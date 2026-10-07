"""arq worker: scheduled crawls, on-demand crawls, enrichment and indicator feeds.

The crawl jobs hand off to the crawl cycle manager (`intel.crawl`) when `CRAWL_ENGINE=queue`
(the default) and to the original source-at-a-time crawler when it is `legacy`. arq is only
the clock here: it decides *when* to look, the manager decides what is due, and Postgres
holds the queue and the coordination.

* Crawls run on a schedule — each source on its own interval.
* `drain_crawl_requests` picks up what the UI's Sync button queued, within seconds.
* The enrichment sweep and the feed fetches run on their own crons, off the crawl lock:
  IOC feeds hourly, ransomware.live every 15 minutes, scam-number reports every 30.

Run with:  arq intel.tasks.WorkerSettings
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from .config import get_settings
from .crawl.manager import CycleManager, RunOutcome
from .enrich import enrich_client
from .enrich_sweep import sweep_domains
from .feeds import (
    ScamReport,
    fetch_mastodon_reports,
    fetch_ransomware_live,
    fetch_reddit_reports,
    fetch_threatfox,
    fetch_tweetfeed,
    fetch_urlhaus,
)
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


def _manager(ctx: dict[str, Any]) -> CycleManager:
    """The cycle manager for this tick.

    `crawl_collectors` and `crawl_ingest_storage` in the context are seams for tests, which
    substitute a scripted network and a recording ingest; a running worker sets neither.
    """
    return CycleManager(
        ctx["storage"],
        ctx["settings"],
        collectors=ctx.get("crawl_collectors"),
        ingest_storage=ctx.get("crawl_ingest_storage"),
    )


def _cycle_result(outcome: RunOutcome | None) -> dict[str, int | str]:
    if outcome is None:
        return {"skipped": "another crawl is already running"}
    if outcome.discarded:
        return {"skipped": "nothing is due"}
    return {
        "cycle": outcome.cycle_id,
        "status": outcome.status,
        "sources": outcome.sources_attempted,
        "urls": outcome.urls,
        "new": outcome.leaks_found,
        "seen_again": outcome.leaks_updated,
        "failed": len(outcome.failed_sources),
    }


async def crawl_all(ctx: dict[str, Any]) -> dict[str, int | str]:
    """Crawl every enabled source now, whether or not it is due. Manual "sync everything"."""
    if ctx["settings"].engine == "legacy":
        return await _legacy_crawl_all(ctx)
    return _cycle_result(await _manager(ctx).run("manual", force=True))


async def crawl_due(ctx: dict[str, Any]) -> dict[str, int | str]:
    """The scheduled sweep: start (or join) a cycle covering whatever has come due.

    The cron fires every `CRAWL_SWEEP_INTERVAL_MINUTES`. Firing is cheap: if nothing is due
    the manager starts nothing, and what *is* due is decided per URL from its own
    `next_crawl_at`, not by this clock. If a cycle is already running this tick joins it as
    extra workers, which adds no concurrency (the limit is enforced in the database) but means
    the cycle keeps moving if the process that started it has died.
    """
    if ctx["settings"].engine == "legacy":
        return await _legacy_crawl_due(ctx)
    return _cycle_result(await _manager(ctx).run("schedule"))


def _request_result(outcome: RunOutcome) -> tuple[str, str | None]:
    """A cycle's outcome as a `crawl_requests` status, in the vocabulary the UI already reads."""
    if outcome.sources_attempted == 0:
        return "skipped", "no enabled sources matched this request"
    if outcome.status == "failed":
        return "failed", f"every source failed: {', '.join(outcome.failed_sources[:5])}"
    if outcome.status != "completed":
        return "failed", f"the crawl cycle ended as {outcome.status}"
    return "succeeded", None


async def drain_crawl_requests(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run whatever the UI's Sync button queued.

    A request never makes a cycle of its own when one is already running: it is attached to
    that cycle (its sources are queued into it) and answered when the cycle finishes. If a
    cycle cannot start at all because `intel run` holds the legacy lock, the request goes back
    to the queue untouched and is picked up on a later tick.
    """
    settings = ctx["settings"]
    if settings.engine == "legacy":
        return await _legacy_drain_crawl_requests(ctx)

    storage: Storage = ctx["storage"]

    # A worker killed mid-crawl leaves rows at 'running' forever; see the legacy drain.
    expired_requests = await storage.expire_stale_crawl_requests(settings.job_timeout_seconds)
    expired_runs = await storage.expire_stale_crawl_runs(settings.job_timeout_seconds)
    if expired_requests or expired_runs:
        log.warning(
            "expired abandoned crawl records", requests=expired_requests, runs=expired_runs
        )

    manager = _manager(ctx)
    handled: list[dict[str, Any]] = []

    while (request := await storage.claim_crawl_request()) is not None:
        log.info(
            "crawl requested",
            request=request.id,
            source=request.source_slug or "all enabled",
            by=request.requested_by,
        )
        try:
            outcome = await manager.run(
                "manual",
                request_id=request.id,
                slugs=[request.source_slug] if request.source_slug else None,
                force=True,
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the tick
            log.exception("requested crawl failed", request=request.id)
            await storage.finish_crawl_request(request.id, status="failed", error=str(exc))
            handled.append({"id": request.id, "status": "failed"})
            continue

        if outcome is None:
            await storage.requeue_crawl_request(request.id)
            return {"skipped": "another crawl is already running", "handled": len(handled)}

        status, error = _request_result(outcome)
        await storage.finish_crawl_request(
            request.id,
            status=status,
            sources_crawled=outcome.sources_attempted,
            new_leaks=outcome.leaks_found,
            updated_leaks=outcome.leaks_updated,
            failed_sources=len(outcome.failed_sources),
            error=error,
        )
        handled.append({"id": request.id, "status": status, "cycle": outcome.cycle_id})

    return {"handled": len(handled), "requests": handled}


async def _legacy_crawl_all(ctx: dict[str, Any]) -> dict[str, int | str]:
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


async def _legacy_crawl_due(ctx: dict[str, Any]) -> dict[str, int | str]:
    """The scheduled sweep: crawl only the sources whose own interval has elapsed.

    This is the job the cron below runs. Each source refreshes on its own
    `crawl_interval_seconds`, and nothing is refetched before its interval has elapsed.

    Sweeping every few minutes and crawling only what is due keeps the tick cheap when
    nothing is due, and a source's configured cadence is what decides when it runs.
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


async def _legacy_drain_crawl_requests(ctx: dict[str, Any]) -> dict[str, Any]:
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


async def match_watchlist(ctx: dict[str, Any]) -> dict[str, int]:
    """Match the watchlist against whatever has arrived since it was last matched.

    Once a minute, off the crawl lock: it is a few indexed lookups per entry and touches no
    Tor, so a fifteen-minute crawl has no business gating it. A new entry is matched against
    all history on the first tick after it is added — that is the whole of "the API never
    matches anything itself".
    """
    new = await ctx["storage"].match_watchlist()
    return {"new_matches": new}


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
        "ioc_hosts": result.ioc_hosts,
        "ioc_lookups": result.ioc_lookups,
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
    collectors = {
        "urlhaus": fetch_urlhaus,
        "threatfox": fetch_threatfox,
        "tweetfeed": fetch_tweetfeed,
    }
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


async def fetch_ransomware_feed(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull ransomware.live's most recent victims into `leaks`.

    Every 15 minutes rather than hourly with the indicator feeds: the endpoint returns the
    newest 100 victims, and a group posting a batch of forty would push earlier victims out
    of that window between two hourly runs. It is one small JSON request.
    """
    settings = ctx["settings"]
    if not settings.feeds_enabled:
        return {"skipped": "FEEDS_ENABLED is off"}

    storage: Storage = ctx["storage"]
    try:
        async with enrich_client() as client:
            leaks = await fetch_ransomware_live(client)
    except Exception as exc:  # noqa: BLE001 - a feed outage is logged, not raised
        log.warning("ransomware.live fetch failed", error=str(exc))
        return {"error": str(exc)[:200]}

    result = await storage.upsert_leaks(leaks, source_id=None)
    log.info("ransomware.live stored", new=result.inserted, seen_again=result.updated)
    return {"new": result.inserted, "seen_again": result.updated}


async def fetch_mobile_reports(ctx: dict[str, Any]) -> dict[str, Any]:
    """Collect scam phone-number reports from public posts into `mobile_numbers`.

    Each platform is isolated, as the indicator feeds are: Reddit rate-limiting us must not
    discard what Mastodon returned.
    """
    settings = ctx["settings"]
    if not (settings.feeds_enabled and settings.mobile_enabled):
        return {"skipped": "FEEDS_ENABLED or MOBILE_ENABLED is off"}

    storage: Storage = ctx["storage"]
    summary: dict[str, Any] = {}

    async with enrich_client() as client:
        collectors = {
            "mastodon": lambda: fetch_mastodon_reports(
                client,
                instance=settings.mobile_mastodon_instance,
                tags=settings.mobile_mastodon_tags,
            ),
            "reddit": lambda: fetch_reddit_reports(client, subreddits=settings.mobile_subreddits),
        }
        for name, collect in collectors.items():
            try:
                reports = await collect()
                new, updated = await storage.upsert_mobile_reports(
                    [_report_row(report) for report in reports]
                )
                summary[name] = {"new": new, "seen_again": updated}
            except Exception as exc:  # noqa: BLE001 - one platform must not stop the other
                log.warning("mobile report fetch failed", source=name, error=str(exc))
                summary[name] = {"error": str(exc)[:200]}

    log.info("mobile reports fetched", **{k: str(v) for k, v in summary.items()})
    return summary


def _report_row(report: ScamReport) -> dict[str, Any]:
    return {
        "number": report.number.e164,
        "number_display": report.number.display,
        "number_raw": report.number.raw,
        "region_code": report.number.region_code,
        "country": report.number.country,
        "line_type": report.number.line_type,
        "threat_types": report.threat_types,
        "target_audience": report.target_audience,
        "details": report.details,
        "source": report.source,
        "source_url": report.source_url,
        "author": report.author,
        "language": report.language,
        "reported_at": report.reported_at,
    }


async def crawl_one(ctx: dict[str, Any], slug: str) -> dict[str, Any]:
    """Crawl a single source now. Enqueued ad hoc."""
    if ctx["settings"].engine == "legacy":
        return await _legacy_crawl_one(ctx, slug)
    source = await ctx["storage"].get_source(slug)
    if source is None:
        return {"error": f"no source {slug!r}"}
    return _cycle_result(await _manager(ctx).run("manual", slugs=[slug], force=True))


async def _legacy_crawl_one(ctx: dict[str, Any], slug: str) -> dict[str, Any]:
    """Crawl a single source. Enqueued ad hoc, or by a per-source schedule."""
    storage: Storage = ctx["storage"]
    source = await storage.get_source(slug)
    if source is None:
        return {"error": f"no source {slug!r}"}

    result = await crawl_source(source, storage=storage, settings=ctx["settings"])

    return {
        "slug": result.slug,
        "status": result.status,
        "new": result.leaks.inserted,
        "seen_again": result.leaks.updated,
    }


_settings = get_settings()

# Seconds within each minute on which the request drain fires. Every 10 seconds is fast
# enough that a Sync click feels immediate and cheap enough to be free — an empty tick is a
# single index lookup on `crawl_requests_pending_idx`.
_DRAIN_SECONDS = {0, 10, 20, 30, 40, 50}

# Minutes on which the due-source sweep fires. Every 5 minutes, offset off the hour so it
# does not collide with every other cron on the box. The sweep itself is cheap when nothing
# is due; what it costs is decided by the sources' own intervals, not by this number.
def sweep_minutes(interval: int) -> set[int]:
    """Minutes of the hour on which the sweep fires, `interval` apart, starting at :02.

    Offset off the hour so it does not collide with the other crons. With the default 5 this
    is the same set as ever: {2, 7, 12, ... 57}.
    """
    return {m % 60 for m in range(2, 62, max(1, interval))}


_SWEEP_MINUTES = sweep_minutes(_settings.sweep_interval_minutes)


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
        fetch_mobile_reports,
        fetch_ransomware_feed,
        match_watchlist,
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
        # A new watch entry should show its history within a minute, and new leaks should
        # reach a watch entry soon after they land. Offset from the other per-minute crons.
        cron(
            match_watchlist,
            second={25},
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
        # ransomware.live every 15 minutes, clear of the crawl sweep's and the feeds' minutes.
        cron(
            fetch_ransomware_feed,
            minute={9, 24, 39, 54},
            timeout=_settings.hunt_timeout_seconds,
            max_tries=1,
        ),
        # Scam-number reports every 30 minutes: one request per hashtag and subreddit, and
        # public timelines move on the order of minutes.
        cron(
            fetch_mobile_reports,
            minute={14, 44},
            timeout=_settings.hunt_timeout_seconds,
            max_tries=1,
        ),
    ]

    # A plain class attribute, not a method: arq reads `redis_settings` directly and expects
    # a RedisSettings instance. As a @staticmethod it handed arq the function object, which
    # failed with "'staticmethod' object has no attribute 'host'" at worker startup.
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
