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
from .pipeline import crawl_source, run_pipeline
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


async def crawl_all(ctx: dict[str, Any]) -> dict[str, int]:
    """Crawl every enabled source. Bounded by settings.concurrency."""
    result = await run_pipeline(storage=ctx["storage"], settings=ctx["settings"])
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
    return {
        "slug": result.slug,
        "status": result.status,
        "new": result.leaks.inserted,
        "seen_again": result.leaks.updated,
    }


async def match_alerts(ctx: dict[str, Any], leak_ids: list[int]) -> dict[str, int]:
    """Match new leaks against alert rules and record deliveries.

    Only the leaks passed in are considered — this is the inversion of the old five-second
    full-collection scan.

    Matching is expressed as SQL over typed matchers, never a regex built from user input:
    an alert's `match_kind` is one of four fixed behaviours, so there is no pattern for a
    user to make pathological.

    Writing to `alert_events` is idempotent by construction — UNIQUE (alert_id, leak_id)
    means a retry or a duplicate message cannot produce a second notification.
    """
    if not leak_ids:
        return {"matched": 0}

    storage: Storage = ctx["storage"]
    matched = await storage._pool.fetchval(  # noqa: SLF001 - repository method pending
        """
        with candidates as (
            select a.id as alert_id, l.id as leak_id, a.channel, a.target,
                   case
                       when a.match_kind = 'actor_group' then 'actor_group'
                       when a.match_kind = 'domain' then 'victim_domain'
                       else 'victim_name'
                   end as matched_on
              from alerts a
              join leaks l on l.id = any($1::bigint[])
             where a.enabled
               and case a.match_kind
                     when 'exact'       then lower(coalesce(l.victim_name, '')) = a.match_value
                     when 'domain'      then lower(coalesce(l.victim_domain, '')) = a.match_value
                                             or lower(coalesce(l.victim_domain, ''))
                                                like '%.' || a.match_value
                     when 'substring'   then position(a.match_value in
                                                lower(coalesce(l.victim_name, '') || ' ' ||
                                                      coalesce(l.victim_domain, ''))) > 0
                     when 'actor_group' then l.actor_group = a.match_value
                   end
        ),
        inserted as (
            insert into alert_events (alert_id, leak_id, matched_on, channel, target, status)
            select alert_id, leak_id, matched_on, channel, target, 'pending'
              from candidates
            on conflict (alert_id, leak_id) do nothing
            returning 1
        )
        select count(*) from inserted
        """,
        leak_ids,
    )

    log.info("alert matching complete", leaks=len(leak_ids), new_events=matched)
    return {"matched": int(matched or 0)}


class WorkerSettings:
    """arq entry point.

    arq has cron built in, so there is no separate beat process to run — one of the reasons
    it was chosen over Celery for this workload.
    """

    functions = [crawl_all, crawl_one, match_alerts]  # noqa: RUF012
    on_startup = startup
    on_shutdown = shutdown

    cron_jobs = [  # noqa: RUF012
        # Hourly, at a minute that isn't :00 — nothing else on the box is doing anything
        # interesting at :17, and it avoids the thundering herd of every cron on the hour.
        cron(crawl_all, minute=17),
    ]

    # A plain class attribute, not a method: arq reads `redis_settings` directly and expects
    # a RedisSettings instance. As a @staticmethod it handed arq the function object, which
    # failed with "'staticmethod' object has no attribute 'host'" at worker startup.
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
