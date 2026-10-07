"""Claim loops: the workers that take URLs off the queue and run a handler on each.

The worker knows nothing about fetching or extraction. It claims a URL, hands it to a
`Handler`, and turns what comes back into a queue transition. Phase 2 plugs the real fetcher
in as the handler; this module is the part that has to be right regardless of what it is.

Two properties matter and both are tested:

* **A retry does not hold a slot.** A transient failure writes the URL back as `retry` with a
  future due time and the loop goes straight on to claim something else. Nothing here sleeps
  through a backoff.
* **A worker holds a URL, never a source.** Any loop may take any eligible URL; the only
  thing that stops a source being worked on by many loops at once is the per-source limit the
  queue enforces.

Concurrency is system-wide, not per process: `CrawlWorkers.run` starts as many loops as the
lane limit allows, and the queue's claim refuses once the *database-wide* count of live leases
reaches that limit. A second worker process therefore shares the budget instead of adding to
it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import structlog

from ..config import Settings
from .frontier import listing_boundary
from .queue import ClaimedUrl, CrawlQueue, NewUrl

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Outcome:
    """What a handler reports about one URL.

    `ok` — fetched and handled. `transient` — failed in a way a later attempt may fix
    (timeout, 5xx, 429, a Tor circuit that would not build). `permanent` — will not improve
    by retrying (404, 403, a gate page, a non-page content type).
    """

    kind: Literal["ok", "transient", "permanent"]
    result: str = ""
    http_status: int | None = None
    response_ms: int | None = None
    # Set only by a handler whose ingest succeeded.
    content_sha256: str | None = None
    leaks_found: int = 0
    leaks_updated: int = 0
    error: str = ""
    # Links found on a page whose content is new or changed. Offered to the queue by the
    # worker only after this URL's own result has been recorded.
    links: list[NewUrl] = field(default_factory=list)


Handler = Callable[[ClaimedUrl], Awaitable[Outcome]]


@dataclass(slots=True)
class WorkerStats:
    ok: int = 0
    retried: int = 0
    failed: int = 0
    lost_lease: int = 0
    reaped: int = 0
    pruned: int = 0
    links_queued: int = 0
    by_worker: dict[str, int] = field(default_factory=dict)


def recrawl_after(url: ClaimedUrl, settings: Settings) -> float:
    """Seconds until this URL is next due, from the cadence its kind has.

    Page 1 of a listing follows the source's own probe interval, deeper listing pages its
    deep-walk interval, and pages reached by a link `CRAWL_LINK_INTERVAL`. All three are
    configuration; nothing here knows what 15 minutes or 6 hours is.
    """
    if url.kind == "link":
        return float(settings.link_recrawl_seconds)
    if (url.page_no or 1) <= 1:
        return float(url.interval_seconds)
    return float(url.deep_interval_seconds)


def retry_delay(
    attempt: int, *, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Exponential backoff with jitter: base, 2·base, 4·base … up to cap, ±20%.

    Jitter so a burst of simultaneous failures (Tor losing a circuit) does not come back as
    a burst of simultaneous retries.
    """
    delay = min(cap, base * (2 ** max(attempt - 1, 0)))
    return delay * (rng or random).uniform(0.8, 1.2)


class CrawlWorkers:
    def __init__(
        self,
        queue: CrawlQueue,
        handler: Handler,
        settings: Settings,
        *,
        poll_seconds: float = 1.0,
        process_id: str | None = None,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._settings = settings
        self._poll = poll_seconds
        self._process_id = process_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run(
        self,
        cycle_id: int,
        *,
        browser: bool = False,
        stop: asyncio.Event | None = None,
    ) -> WorkerStats:
        """Work one lane of a cycle until nothing in it is left to do.

        Returns when the cycle has no queued, running or retrying URLs, or when `stop` is
        set. Loops that find nothing claimable but see work still pending (a retry waiting
        out its backoff, or URLs held by other workers) poll; they do not exit early.
        """
        s = self._settings
        lane_limit = s.browser_workers if browser else s.workers
        stats = WorkerStats()
        stop = stop or asyncio.Event()

        if lane_limit < 1:
            return stats

        reaper = asyncio.create_task(self._reap_loop(cycle_id, stats, stop))
        try:
            await asyncio.gather(
                *(
                    self._loop(
                        f"{self._process_id}:{'b' if browser else 'h'}{n}",
                        cycle_id,
                        browser,
                        lane_limit,
                        stats,
                        stop,
                    )
                    for n in range(lane_limit)
                )
            )
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        return stats

    async def _loop(
        self,
        worker_id: str,
        cycle_id: int,
        browser: bool,
        lane_limit: int,
        stats: WorkerStats,
        stop: asyncio.Event,
    ) -> None:
        s = self._settings
        while not stop.is_set():
            claimed = await self._queue.claim(
                worker_id,
                browser=browser,
                lane_limit=lane_limit,
                per_source_limit=s.per_source_inflight,
                lease_seconds=s.lease_seconds,
                cycle_id=cycle_id,
                page1_first=s.page1_first,
            )
            if claimed is None:
                if await self._queue.active_count(cycle_id) == 0:
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
                continue

            await self._process(worker_id, claimed, stats)
            stats.by_worker[worker_id] = stats.by_worker.get(worker_id, 0) + 1

    async def _process(self, worker_id: str, url: ClaimedUrl, stats: WorkerStats) -> None:
        s = self._settings
        try:
            # Bounded by the lease, with a margin: a handler that hangs must not outlive the
            # claim it runs under, or the reaper would hand the URL to a second worker
            # mid-flight. The cutoff has to fire *before* the lease does, or the two race and
            # this worker's own result is discarded as a lost lease.
            outcome = await asyncio.wait_for(self._handler(url), timeout=s.lease_seconds * 0.9)
        except TimeoutError:
            outcome = Outcome("transient", error=f"handler exceeded its {s.lease_seconds}s lease")
        except Exception as exc:  # noqa: BLE001 - one bad URL must not stop the worker
            log.exception("handler raised", url_id=url.id, source=url.source_slug)
            outcome = Outcome("transient", error=f"{type(exc).__name__}: {exc}")

        recrawl = recrawl_after(url, s)

        if outcome.kind == "ok":
            done = await self._queue.complete(
                url.id,
                worker_id,
                result=outcome.result or "ok",
                http_status=outcome.http_status,
                response_ms=outcome.response_ms,
                recrawl_after_seconds=recrawl,
                content_sha256=outcome.content_sha256,
                leaks_found=outcome.leaks_found,
                leaks_updated=outcome.leaks_updated,
            )
            if done:
                stats.ok += 1
                await self._after_outcome(url, outcome, stats, final_failure=False)
            else:
                stats.lost_lease += 1
            return

        status = await self._queue.fail(
            url.id,
            worker_id,
            error=outcome.error or outcome.kind,
            http_status=outcome.http_status,
            response_ms=outcome.response_ms,
            transient=outcome.kind == "transient",
            max_attempts=s.max_attempts,
            retry_delay_seconds=retry_delay(
                url.attempt,
                base=s.retry_backoff_seconds,
                cap=s.retry_backoff_cap_seconds,
            ),
            recrawl_after_seconds=recrawl,
            result=outcome.result or None,
        )
        if status is None:
            stats.lost_lease += 1
        elif status == "retry":
            stats.retried += 1
        else:
            stats.failed += 1
            await self._after_outcome(url, outcome, stats, final_failure=True)

    async def _after_outcome(
        self, url: ClaimedUrl, outcome: Outcome, stats: WorkerStats, *, final_failure: bool
    ) -> None:
        """What this URL's result means for the rest of the frontier.

        Runs only after the result was recorded under a valid lease, so a worker that lost its
        lease neither prunes nor discovers anything — the URL's new holder will. Never raises:
        frontier bookkeeping must not take a worker down.
        """
        if url.cycle_id is None:
            return
        s = self._settings
        try:
            after = listing_boundary(
                kind=url.kind,
                page_no=url.page_no,
                result=outcome.result,
                http_status=outcome.http_status,
                ok=outcome.kind == "ok",
                final_failure=final_failure,
            )
            if after is not None:
                pruned = await self._queue.prune_listing(url.source_id, url.cycle_id, after)
                if pruned:
                    stats.pruned += pruned
                    log.info(
                        "listing ended, pages skipped",
                        source=url.source_slug,
                        after_page=after,
                        skipped=pruned,
                    )

            if outcome.links and outcome.kind == "ok" and s.follow_links:
                found = await self._queue.enqueue_links(
                    url,
                    outcome.links,
                    max_depth=s.link_depth,
                    per_page_limit=s.links_per_page,
                    source_cycle_limit=s.link_max_pages,
                )
                stats.links_queued += found.accepted
                log.info(
                    "links discovered",
                    source=url.source_slug,
                    url_id=url.id,
                    queued=found.accepted,
                    known=found.known,
                    rejected=found.rejected,
                    over_limit=found.over_limit,
                )
        except Exception:  # noqa: BLE001 - bookkeeping must not kill a worker
            log.exception("frontier update failed", url_id=url.id, source=url.source_slug)

    async def _reap_loop(self, cycle_id: int, stats: WorkerStats, stop: asyncio.Event) -> None:
        """Recover leases whose workers died, and keep the cycle's heartbeat alive."""
        s = self._settings
        interval = max(self._poll * 5, 1.0)
        while not stop.is_set():
            retried, failed = await self._queue.reap_expired(
                max_attempts=s.max_attempts,
                backoff_base=s.retry_backoff_seconds,
                backoff_cap=s.retry_backoff_cap_seconds,
            )
            if retried or failed:
                stats.reaped += retried + failed
                log.warning("reaped expired leases", retried=retried, failed=failed)
            skipped = await self._queue.skip_disabled(cycle_id)
            if skipped:
                stats.pruned += skipped
                log.warning("skipped urls of disabled sources", skipped=skipped)
            await self._queue.heartbeat(cycle_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
