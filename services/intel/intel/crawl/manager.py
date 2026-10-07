"""The crawl cycle manager: start a cycle, seed it, serve it with workers, finalize it.

    scheduler (arq cron, or a Sync request)
        -> manager.run()                    decides; never fetches
              -> start / join / attach      one running cycle at a time, enforced by Postgres
              -> seed                       due listing pages, due followed pages
              -> workers                    claim URLs from `crawl_urls`, fetch, ingest
              -> finalize                   atomically, once, with a report

The scheduler only calls `run`. What is due is decided here, from per-URL `next_crawl_at`;
what is fetched is decided by the queue; the fetching is the workers'.

**One cycle at a time, across every process.** The `crawl_cycles_one_running` index decides who
starts a cycle. A process that finds one already running *joins* it as additional workers —
which adds no concurrency, because the system-wide limit is enforced in the database at claim
time — and so a cycle keeps moving when the process that began it has died. Nothing here
depends on one process staying alive.

**A manual Sync never makes a second cycle.** If one is running, the request is attached to it:
the requested sources are force-queued into the running cycle, and the request is answered
when that cycle finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from dataclasses import dataclass, field

import structlog

from ..config import Settings
from ..storage import SourceRow, Storage
from .fetch import CrawlFetcher
from .queue import CrawlQueue
from .report import CycleReport
from .seeding import seed_source
from .worker import CrawlWorkers

log = structlog.get_logger(__name__)

# Cycles this process is already serving. A cron tick that fires while this process is still
# working a cycle must not start a second set of loops for it.
_SERVING: set[int] = set()


@dataclass(slots=True)
class RunOutcome:
    """What a `run` call knows about the cycle it took part in."""

    cycle_id: int
    status: str  # completed | failed | abandoned | running (finished by nobody yet)
    trigger: str
    sources_attempted: int = 0
    failed_sources: list[str] = field(default_factory=list)
    urls: int = 0
    leaks_found: int = 0
    leaks_updated: int = 0
    error: str | None = None
    report: CycleReport | None = None
    discarded: bool = False


class CycleManager:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        *,
        queue: CrawlQueue | None = None,
        ingest_storage: object | None = None,
        collectors: dict[str, object] | None = None,
        poll_seconds: float = 1.0,
        process_id: str | None = None,
        serving: set[int] | None = None,
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._queue = queue or CrawlQueue(storage.pool)
        # Where pages are ingested. The real storage unless a test substitutes a double.
        self._ingest_storage = ingest_storage or storage
        self._collectors = collectors
        self._poll = poll_seconds
        self._process_id = process_id or f"{socket.gethostname()}:{os.getpid()}"
        # Cycles this process is already serving; shared by default, injectable for tests that
        # stand in for separate processes.
        self._serving = _SERVING if serving is None else serving

    @property
    def queue(self) -> CrawlQueue:
        return self._queue

    # ---------------------------------------------------------------- entry point

    async def run(
        self,
        trigger: str,
        *,
        request_id: int | None = None,
        slugs: list[str] | None = None,
        force: bool = False,
        wait: bool = False,
    ) -> RunOutcome | None:
        """Take part in a cycle: start one if none is running, otherwise join or attach.

        `trigger` is `schedule`, `manual` or `cli`. `force` queues every page of the selected
        sources whether or not it is due — a Sync means now. `wait` makes the call return
        only once the cycle has been finalized by someone, so a request can be answered with
        its result.

        Returns None when nothing could be done: the legacy crawler's lock is held, or an
        empty scheduled cycle had nothing due and was discarded.
        """
        s = self._settings

        for report in await self._queue.abandon_stale_cycles(s.job_timeout_seconds):
            log.warning("abandoned a stalled cycle", cycle=report.cycle_id)

        sources = await self._select_sources(slugs)

        cycle = await self._queue.running_cycle()
        if cycle is not None:
            return await self._join(cycle, trigger, sources, force=force or bool(slugs), wait=wait)

        # The legacy crawler's lock: two crawls on one Tor daemon slow each other down, so a
        # cycle does not start while `intel run` is in the middle of one. Held by the process
        # that starts the cycle for as long as it serves it; if that process dies the lock is
        # released with it, and the cycle carries on in whichever process joins.
        async with self._storage.crawl_lock() as acquired:
            if not acquired:
                log.warning("another crawl holds the lock, not starting a cycle")
                return None

            cycle = await self._queue.start_cycle(trigger, request_id=request_id)
            if cycle is None:  # another process won the race a moment ago
                cycle = await self._queue.running_cycle()
                if cycle is None:
                    return None
                return await self._join(
                    cycle, trigger, sources, force=force or bool(slugs), wait=wait
                )

            log.info("[CRAWL] cycle started", cycle=cycle, trigger=trigger, forced=force)
            await self._seed(cycle, sources, force=force)
            return await self._serve(cycle, trigger, wait=wait)

    # ---------------------------------------------------------------- joining

    async def _join(
        self, cycle: int, trigger: str, sources: list[SourceRow], *, force: bool, wait: bool
    ) -> RunOutcome | None:
        """Work a cycle someone else started. Adds sources to it only if asked to by force."""
        # A Sync arriving mid-cycle: queue what was asked for into the running cycle instead
        # of starting a second one. The cycle cannot be finalized while this is under way.
        if force and trigger != "schedule" and await self._seed(cycle, sources, force=True):
            log.info("[CRAWL] sync attached to running cycle", cycle=cycle)
        return await self._serve(cycle, trigger, wait=wait)

    # ---------------------------------------------------------------- seeding

    async def _select_sources(self, slugs: list[str] | None) -> list[SourceRow]:
        sources = await self._storage.list_sources(only_enabled=True)  # enabled ones only
        if slugs:
            wanted = set(slugs)
            sources = [src for src in sources if src.slug in wanted]
        return sources

    async def _seed(self, cycle: int, sources: list[SourceRow], *, force: bool) -> bool:
        """Queue what is due for these sources. False if the cycle was no longer open."""
        s = self._settings
        async with self._queue.hold_cycle_open(cycle) as open_:
            if not open_:
                return False

            for source in sources:
                if source.collector == "browser" and s.browser_workers < 1:
                    # Nothing would ever claim these URLs and the cycle could not finish.
                    log.warning(
                        "browser source skipped: CRAWL_BROWSER_WORKERS is 0", source=source.slug
                    )
                    continue
                await seed_source(self._queue, cycle, source, force=force)
                if s.follow_links:
                    requeued, rejected = await self._queue.requeue_due_links(
                        cycle,
                        source.id,
                        base_url=source.base_url,
                        active_url=source.active_url,
                        max_depth=s.link_depth,
                        max_pages=s.link_recrawl_max_pages,
                        recheck_after_seconds=s.link_recrawl_seconds,
                    )
                    if requeued or rejected:
                        log.info(
                            "due links", source=source.slug, requeued=requeued, rejected=rejected
                        )

            # One `crawl_runs` row per source that has work, opened now so the Sources page
            # and the Sync button see collection as running from the start.
            for source_id in await self._queue.cycle_source_ids(cycle):
                await self._queue.ensure_run(cycle, source_id)
        return True

    # ---------------------------------------------------------------- serving and finishing

    async def _serve(self, cycle: int, trigger: str, *, wait: bool) -> RunOutcome | None:
        s = self._settings

        if cycle not in self._serving:
            self._serving.add(cycle)
            fetcher = CrawlFetcher(
                self._ingest_storage,  # type: ignore[arg-type]
                s,
                collectors=self._collectors,
                queue=self._queue,
            )
            workers = CrawlWorkers(
                self._queue, fetcher, s, poll_seconds=self._poll, process_id=self._process_id
            )
            try:
                lanes = [workers.run(cycle)]
                if s.browser_workers > 0:
                    lanes.append(workers.run(cycle, browser=True))
                stats = await asyncio.gather(*lanes)
                log.info(
                    "[CRAWL] workers drained",
                    cycle=cycle,
                    ok=sum(x.ok for x in stats),
                    retried=sum(x.retried for x in stats),
                    failed=sum(x.failed for x in stats),
                    pruned=sum(x.pruned for x in stats),
                    links_queued=sum(x.links_queued for x in stats),
                    lost_lease=sum(x.lost_lease for x in stats),
                )
            finally:
                self._serving.discard(cycle)
                with contextlib.suppress(Exception):
                    await fetcher.aclose()

            # Before deciding the cycle is complete, recover any URL whose worker died: it is
            # `running` until its lease runs out, which is correctly "not finished".
            await self._queue.reap_expired(
                max_attempts=s.max_attempts,
                backoff_base=s.retry_backoff_seconds,
                backoff_cap=s.retry_backoff_cap_seconds,
            )
            report = await self._queue.finalize_cycle(cycle)
            if report is not None:
                return self._outcome_from_report(report)

        if wait:
            await self._wait_finished(cycle)
        return await self._outcome_from_row(cycle, trigger)

    async def _wait_finished(self, cycle: int) -> None:
        """Wait for some process to finalize the cycle, bounded by the job timeout."""
        deadline = asyncio.get_running_loop().time() + self._settings.job_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self._queue.cycle_status(cycle) != "running":
                return
            await asyncio.sleep(self._poll)

    def _outcome_from_report(self, report: CycleReport) -> RunOutcome:
        if report.discarded:
            log.info("[CRAWL] nothing due", cycle=report.cycle_id)
            return RunOutcome(report.cycle_id, "completed", report.trigger, discarded=True)
        summary = report.summary
        for line in report.text().splitlines():
            log.info(f"[CRAWL] {line}")
        return RunOutcome(
            cycle_id=report.cycle_id,
            status=report.status,
            trigger=report.trigger,
            sources_attempted=len(summary.sources),
            failed_sources=summary.failed_sources,
            urls=summary.total,
            leaks_found=summary.leaks_found,
            leaks_updated=summary.leaks_updated,
            error=summary.error,
            report=report,
        )

    async def _outcome_from_row(self, cycle: int, trigger: str) -> RunOutcome | None:
        """The result of a cycle some other process finalized, read back from its row."""
        row = await self._queue.pool.fetchrow(
            """
            select status::text as status, trigger, total_urls, leaks_found, leaks_updated,
                   summary::text as summary, error
              from crawl_cycles where id = $1
            """,
            cycle,
        )
        if row is None:
            return None
        summary = json.loads(row["summary"]) if row["summary"] else {}
        sources = summary.get("sources", {})
        return RunOutcome(
            cycle_id=cycle,
            status=row["status"],
            trigger=row["trigger"],
            sources_attempted=sources.get("attempted", 0),
            failed_sources=sources.get("failed", []),
            urls=row["total_urls"],
            leaks_found=row["leaks_found"],
            leaks_updated=row["leaks_updated"],
            error=row["error"],
        )
