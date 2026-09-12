"""arq worker: scheduled crawls, on-demand crawls, and event-driven alert matching.

Three things this replaces:

* The manual notebook run — crawls are now scheduled.
* The API's `monitorCollection()` loop, which re-scanned the entire collection every five
  seconds looking for alert matches. Matching here is driven by *new leaks only*, so cost is
  proportional to what actually arrived rather than to how long the process has been up.
* "Wait for the top of the hour" as the only way to refresh anything. `drain_crawl_requests`
  picks up what the UI's Sync button queued, within seconds.

Run with:  arq intel.tasks.WorkerSettings
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from .config import get_settings
from .enrich import enrich_client
from .enrich_sweep import sweep_domains
from .feeds import fetch_threatfox, fetch_urlhaus
from .hunt import run_hunt
from .logging import configure_logging
from .pipeline import crawl_source, run_pipeline, run_pipeline_locked
from .storage import Storage

log = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=True)
    ctx["settings"] = settings
    ctx["storage"] = await Storage.connect(settings.asyncpg_dsn, max_size=10)
    log.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    storage: Storage = ctx["storage"]
    await storage.close()
    log.info("worker stopped")


async def crawl_all(ctx: dict[str, Any]) -> dict[str, int | str]:
    """Crawl every enabled source, ignoring their intervals. Manual "sync everything".

    Takes the crawl lock, so a run that overlaps a manual `intel run` steps aside instead of
    competing with it for Tor circuits.
    """
    result = await run_pipeline_locked(storage=ctx["storage"], settings=ctx["settings"])
    if result is None:
        return {"skipped": "another crawl is already running"}
    return {
        "sources": len(result.sources),
        "new": result.inserted,
        "seen_again": result.updated,
        "failed": len(result.failed),
    }


async def crawl_due(ctx: dict[str, Any]) -> dict[str, int | str]:
    """The scheduled sweep: crawl only the sources whose own interval has elapsed.

    This is the job the cron below runs, and the fix for the thing that made "auto-fetching"
    look broken. The schedule used to be one hourly `crawl_all`, which meant two wrong
    behaviours at once: `sources.crawl_interval_seconds` was written by every operator and
    read by nobody, so a source configured to refresh every 15 minutes refreshed hourly; and
    every other source was refetched on that same hour whether or not its interval had
    elapsed, so an already-long job spent most of its time re-reading pages whose content
    hash was about to match.

    Sweeping every few minutes and crawling only what is due inverts that: the tick is cheap
    when nothing is due, and a source's configured cadence is finally the thing that decides
    when it runs.
    """
    result = await run_pipeline_locked(
        storage=ctx["storage"], settings=ctx["settings"], only_due=True
    )
    if result is None:
        return {"skipped": "another crawl is already running"}
    return {
        "sources": len(result.sources),
        "new": result.inserted,
        "seen_again": result.updated,
        "failed": len(result.failed),
    }


async def drain_crawl_requests(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run whatever the UI's Sync button queued.

    The API cannot enqueue an arq job — arq pickles its payloads and the API is TypeScript —
    so a request arrives as a row in `crawl_requests` and this tick is what notices it. The
    whole tick is one indexed query when nothing is waiting, which is what makes running it
    every few seconds reasonable.

    The crawl lock is taken here and held across claim-and-run rather than inside
    `run_pipeline_locked`, for two reasons. A request must not be claimed and then discover
    it cannot run — that would mark it running and immediately abandon it. And a tick that
    cannot get the lock must leave queued requests exactly where they are, so the crawl
    already in flight finishes and the next tick picks them up.
    """
    storage: Storage = ctx["storage"]
    settings = ctx["settings"]

    # A worker killed mid-crawl leaves rows at 'running' forever — `finish_crawl` is
    # shielded against cancellation but not against the process vanishing. Both tables are
    # read as "collection is happening now", so stranded rows show the UI a sync with no
    # end and disable the button whose whole job is to recover from that. Swept here rather
    # than at startup, so a crash that took the whole host down is still recovered by
    # whichever worker comes back.
    expired_requests = await storage.expire_stale_crawl_requests(settings.job_timeout_seconds)
    expired_runs = await storage.expire_stale_crawl_runs(settings.job_timeout_seconds)
    if expired_requests or expired_runs:
        log.warning(
            "expired abandoned crawl records",
            requests=expired_requests,
            runs=expired_runs,
        )

    handled: list[dict[str, Any]] = []

    async with storage.crawl_lock() as acquired:
        if not acquired:
            # Not an error: a crawl is running and these requests are next in line.
            return {"skipped": "another crawl is already running"}

        # Drain the whole queue while we hold the lock. Several people clicking Sync inside
        # one crawl's runtime is the normal case, and making each of them wait for a
        # separate tick would serialise them minutes apart for no reason.
        while (request := await storage.claim_crawl_request()) is not None:
            log.info(
                "crawl requested",
                request=request.id,
                source=request.source_slug or "all enabled",
                by=request.requested_by,
            )
            try:
                result = await run_pipeline(
                    storage=storage,
                    settings=settings,
                    slugs=[request.source_slug] if request.source_slug else None,
                )
            except Exception as exc:  # noqa: BLE001 - one bad request must not kill the tick
                log.exception("requested crawl failed", request=request.id)
                await storage.finish_crawl_request(
                    request.id, status="failed", error=str(exc)
                )
                handled.append({"id": request.id, "status": "failed"})
                continue

            # A crawl where some sources failed is still a crawl: the count goes in
            # `failed_sources` and the UI reports it beside the new-leak numbers. Only two
            # outcomes are not "succeeded" — nothing ran at all, and nothing that ran
            # worked. Anything else would tell someone their sync failed when it had just
            # collected several hundred listings from the sources that were up.
            if not result.sources:
                outcome, error = "skipped", "no enabled sources matched this request"
            elif len(result.failed) == len(result.sources):
                outcome, error = "failed", f"every source failed: {', '.join(result.failed[:5])}"
            else:
                outcome, error = "succeeded", None

            await storage.finish_crawl_request(
                request.id,
                status=outcome,
                sources_crawled=len(result.sources),
                new_leaks=result.inserted,
                updated_leaks=result.updated,
                failed_sources=len(result.failed),
                error=error,
            )
            handled.append({"id": request.id, "status": outcome, "new": result.inserted})

    return {"handled": len(handled), "requests": handled}


async def drain_hunt_jobs(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run whatever company searches the UI queued.

    The same handoff as `drain_crawl_requests` and for the same reason — the API is
    TypeScript and cannot enqueue an arq job — but deliberately *not* behind the crawl lock.
    A hunt makes a handful of ordinary HTTPS requests and touches neither Tor nor the crawl
    pipeline, so making it wait for a fifteen-minute crawl to finish would turn an
    interactive search into something slower than the thing it was meant to replace.

    Ticks every few seconds. An empty tick is one indexed lookup on the partial
    `hunt_jobs_status_idx`, which is what makes running it that often reasonable.
    """
    storage: Storage = ctx["storage"]
    settings = ctx["settings"]

    # A worker killed mid-hunt leaves rows at 'running' forever, and the results page polls
    # those into an endless spinner. Swept here rather than at startup so a crash that took
    # the host down is still recovered by whichever worker comes back.
    expired = await storage.expire_stale_hunt_jobs(settings.hunt_timeout_seconds)
    if expired:
        log.warning("expired abandoned hunts", hunts=expired)

    handled: list[dict[str, Any]] = []

    # Drain the whole queue in one tick. Several analysts searching at once is the normal
    # case, and hunts do not contend for anything, so making each wait for its own tick
    # would add seconds of latency to buy nothing.
    while (job := await storage.claim_hunt_job()) is not None:
        try:
            outcome = await run_hunt(storage, job)
        except Exception as exc:  # noqa: BLE001 - one bad hunt must not kill the tick
            log.exception("hunt failed", hunt=job.id)
            await storage.finish_hunt_job(
                job.id, status="failed", errors={"hunt": str(exc)[:200]}
            )
            handled.append({"id": job.id, "status": "failed"})
            continue

        await storage.finish_hunt_job(
            job.id,
            status=outcome.status,
            target_domain=outcome.target_domain,
            errors=outcome.errors or None,
        )
        handled.append(
            {"id": job.id, "status": outcome.status, "findings": outcome.findings}
        )

    return {"handled": len(handled), "hunts": handled}


async def enrich_domains(ctx: dict[str, Any]) -> dict[str, int]:
    """Fill in WHOIS, technologies and site status for victim domains, a batch at a time.

    Without this the enrichment columns only ever hold the domains someone has personally
    searched for, and the Ransomware and Dark Web tables show empty columns for every other
    row — which reads as broken rather than as not-yet-collected.

    Runs on its own cron rather than inside the crawl, because it touches none of the same
    things: no Tor, no crawl lock, no leak-site traffic. Bolting it onto a crawl would make
    a fifteen-minute Tor job the gate on data that has nothing to do with Tor.
    """
    result = await sweep_domains(storage=ctx["storage"], settings=ctx["settings"])
    return {
        "attempted": result.attempted,
        "enriched": result.enriched,
        "failed": result.failed,
    }


async def fetch_feeds(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull the public indicator feeds and upsert what they hold.

    Hourly, not continuous. These are rolling dumps that change on the order of minutes and
    are several megabytes each — fetching them more often would cost abuse.ch real bandwidth
    to learn almost nothing, and they publish them for free.

    Each feed is isolated: one being down is recorded and the other still lands. The same
    reasoning as the hunt enrichers — a feed outage must not discard a feed that answered.
    """
    settings = ctx["settings"]
    if not settings.feeds_enabled:
        return {"skipped": "FEEDS_ENABLED is off"}

    storage: Storage = ctx["storage"]
    collectors = {"urlhaus": fetch_urlhaus, "threatfox": fetch_threatfox}
    summary: dict[str, Any] = {}

    async with enrich_client() as client:
        for name, collector in collectors.items():
            try:
                indicators = await collector(client, limit=settings.feeds_max_entries)
            except Exception as exc:  # noqa: BLE001 - one feed must not stop the others
                log.warning("feed fetch failed", feed=name, error=str(exc))
                summary[name] = {"error": str(exc)[:200]}
                continue

            try:
                new, updated = await storage.upsert_iocs(
                    [
                        {
                            "value": item.value,
                            "ioc_type": item.ioc_type,
                            "host": item.host,
                            "tags": item.tags,
                            "threat": item.threat,
                            "note": item.note,
                            "confidence": item.confidence,
                            "feed": item.feed,
                            "feed_ref": item.feed_ref,
                            "reporter": item.reporter,
                            "reported_at": item.reported_at,
                        }
                        for item in indicators
                    ]
                )
                summary[name] = {"new": new, "seen_again": updated}
            except Exception as exc:  # noqa: BLE001
                log.exception("feed store failed", feed=name)
                summary[name] = {"error": str(exc)[:200]}

    log.info("feeds fetched", **{k: str(v) for k, v in summary.items()})
    return summary


async def crawl_one(ctx: dict[str, Any], slug: str) -> dict[str, Any]:
    """Crawl a single source. Enqueued ad hoc, or by a per-source schedule."""
    storage: Storage = ctx["storage"]
    source = await storage.get_source(slug)
    if source is None:
        return {"error": f"no source {slug!r}"}

    result = await crawl_source(source, storage=storage, settings=ctx["settings"])

    # One source is still a source of new leaks, so it evaluates alerts like a full run.
    events = await storage.match_alerts(result.leaks.new_leak_ids)

    return {
        "slug": result.slug,
        "status": result.status,
        "new": result.leaks.inserted,
        "seen_again": result.leaks.updated,
        "alert_events": events,
    }


async def match_alerts(ctx: dict[str, Any], leak_ids: list[int]) -> dict[str, int]:
    """Match new leaks against alert rules and record deliveries.

    Only the leaks passed in are considered — this is the inversion of the old five-second
    full-collection scan. The matching itself lives in `Storage.match_alerts` so the CLI and
    the crawl pipeline run the same matcher rather than a second copy of the SQL.
    """
    storage: Storage = ctx["storage"]
    matched = await storage.match_alerts(leak_ids)
    log.info("alert matching complete", leaks=len(leak_ids), new_events=matched)
    return {"matched": matched}


_settings = get_settings()

# Seconds within each minute on which the request drain fires. Every 10 seconds is fast
# enough that a Sync click feels immediate and cheap enough to be free — an empty tick is a
# single index lookup on `crawl_requests_pending_idx`.
_DRAIN_SECONDS = {0, 10, 20, 30, 40, 50}

# Minutes on which the due-source sweep fires. Every 5 minutes, offset off the hour so it
# does not collide with every other cron on the box. The sweep itself is cheap when nothing
# is due; what it costs is decided by the sources' own intervals, not by this number.
_SWEEP_MINUTES = {2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57}


class WorkerSettings:
    """arq entry point.

    arq has cron built in, so there is no separate beat process to run — one of the reasons
    it was chosen over Celery for this workload.
    """

    functions = [  # noqa: RUF012
        crawl_all,
        crawl_due,
        crawl_one,
        drain_crawl_requests,
        drain_hunt_jobs,
        enrich_domains,
        fetch_feeds,
        match_alerts,
    ]
    on_startup = startup
    on_shutdown = shutdown

    # arq's default is 300 seconds. A full crawl of 32 sources takes ~15 minutes, so every
    # scheduled run was cancelled at exactly 299.99s and recorded as a failure — the reason
    # collection only ever worked when someone ran the CLI by hand.
    job_timeout = _settings.job_timeout_seconds

    # A crawl that timed out will time out again on a retry, an hour of Tor traffic per
    # attempt. The next scheduled run is the retry.
    max_tries = 1

    cron_jobs = [  # noqa: RUF012
        # `timeout` is set on each cron job as well as on the worker: arq applies the job's
        # own timeout when it has one, and leaving it unset here would silently reinstate
        # the 300s default for exactly the jobs that need an hour.
        cron(
            crawl_due,
            minute=_SWEEP_MINUTES,
            timeout=_settings.job_timeout_seconds,
            max_tries=1,
        ),
        cron(
            drain_crawl_requests,
            second=_DRAIN_SECONDS,
            timeout=_settings.job_timeout_seconds,
            max_tries=1,
        ),
        # Hunts get their own timeout, not the crawl's hour. A search someone is watching
        # must fail fast and say so; inheriting `job_timeout` would leave a dead hunt
        # holding the page for an hour.
        cron(
            drain_hunt_jobs,
            second=_DRAIN_SECONDS,
            timeout=_settings.hunt_timeout_seconds,
            max_tries=1,
        ),
        # Once a minute, a dozen domains. Slow by design — see `enrich_sweep`.
        cron(
            enrich_domains,
            second={5},
            timeout=_settings.hunt_timeout_seconds,
            max_tries=1,
        ),
        # Hourly, at :34 so it does not land on the same minute as the crawl sweep. The
        # dumps are several megabytes; fetching them more often costs abuse.ch bandwidth to
        # learn almost nothing.
        cron(
            fetch_feeds,
            minute={34},
            timeout=_settings.job_timeout_seconds,
            max_tries=1,
        ),
    ]

    # A plain class attribute, not a method: arq reads `redis_settings` directly and expects
    # a RedisSettings instance. As a @staticmethod it handed arq the function object, which
    # failed with "'staticmethod' object has no attribute 'host'" at worker startup.
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
