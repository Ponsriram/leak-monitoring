"""arq worker: scheduled crawls and event-driven alert matching.

Two things this replaces:

* The manual notebook run — crawls are now on a cron schedule per source group.
* The API's `monitorCollection()` loop, which re-scanned the entire collection every five
  seconds looking for alert matches. Matching here is driven by *new leaks only*, so cost is
  proportional to what actually arrived rather than to how long the process has been up.

Run with:  arq intel.tasks.WorkerSettings
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from .config import get_settings
from .logging import configure_logging
from .pipeline import crawl_source, run_pipeline_locked
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
    """Crawl every enabled source. Bounded by settings.concurrency.

    Takes the crawl lock, so a scheduled run that overlaps a manual `intel run` steps aside
    instead of competing with it for Tor circuits.
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


class WorkerSettings:
    """arq entry point.

    arq has cron built in, so there is no separate beat process to run — one of the reasons
    it was chosen over Celery for this workload.
    """

    functions = [crawl_all, crawl_one, match_alerts]  # noqa: RUF012
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
        # Hourly, at a minute that isn't :00 — nothing else on the box is doing anything
        # interesting at :17, and it avoids the thundering herd of every cron on the hour.
        #
        # `timeout` is set on the cron job as well as on the worker: arq applies the job's
        # own timeout when it has one, and leaving it unset here would silently reinstate
        # the 300s default for exactly the job that needs an hour.
        cron(
            crawl_all,
            minute=17,
            timeout=_settings.job_timeout_seconds,
            max_tries=1,
        ),
    ]

    # A plain class attribute, not a method: arq reads `redis_settings` directly and expects
    # a RedisSettings instance. As a @staticmethod it handed arq the function object, which
    # failed with "'staticmethod' object has no attribute 'host'" at worker startup.
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
