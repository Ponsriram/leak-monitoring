"""The crawl queue: every SQL statement that moves a URL between states.

    queued ──claim──▶ running ──ok──▶ succeeded
       ▲                 │
       │                 ├─transient failure──▶ retry ──claim──▶ running …
       │                 ├─permanent failure / attempts spent──▶ failed
       │                 └─lease expires (worker died)──▶ reaper ──▶ retry / failed

A `retry` row is just a queued row with a later `next_crawl_at`. That is what lets a failed
URL give its worker slot back immediately instead of sleeping through the backoff.

Concurrency limits are enforced here, in the database, and not in any one process's memory.
`claim` takes a transaction-scoped advisory lock, counts the live leases, and only then picks
a row, so the system-wide and per-source limits hold exactly however many worker processes
are claiming. The lock is held for one short transaction; claiming is not where the time goes
(a fetch over Tor is tens of seconds).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import structlog

from .frontier import admit_link, host_of
from .report import CycleReport, CycleSummary, SourceVerdict, build_summary, judge_source

log = structlog.get_logger(__name__)

# Held for the duration of one claim transaction. Distinct from the legacy crawl lock key.
_CLAIM_LOCK_KEY = 0x1EA4_C1A1

# Advisory-lock classes for the two-key form, distinct from the claim lock.
_LINK_LOCK_CLASS = 0x4C4E4B53
_RUN_LOCK_CLASS = 0x52554E53

TERMINAL = ("succeeded", "failed", "skipped")


@dataclass(slots=True)
class NewUrl:
    """A URL offered to the queue. `url_normalized` is its identity."""

    url: str
    url_normalized: str
    kind: str  # 'listing' | 'link'
    page_no: int | None = None
    depth: int = 0
    parent_id: int | None = None


@dataclass(slots=True)
class ClaimedUrl:
    """A URL a worker now holds a lease on."""

    id: int
    source_id: int
    source_slug: str
    collector: str
    cycle_id: int | None
    url: str
    kind: str
    page_no: int | None
    depth: int
    attempt: int
    parent_id: int | None
    content_sha256: str | None
    failure_count: int
    # The source's own cadence, carried so the worker can decide when this URL is next due
    # without another query.
    interval_seconds: int
    deep_interval_seconds: int
    # The addresses this source may be crawled at. A discovered link must be on one of these
    # hosts; carried on the claim so discovery needs no extra query to know its boundary.
    base_url: str = ""
    active_url: str | None = None
    # The source's CSS selector for one listing block, if it has one (see `to_text`).
    item_selector: str | None = None


@dataclass(slots=True)
class EnqueueResult:
    inserted: int = 0
    requeued: int = 0


@dataclass(slots=True)
class LinkResult:
    """What happened to the links offered from one page."""

    inserted: int = 0
    requeued: int = 0
    rejected: int = 0  # failed the allowlist / file / account-page checks
    known: int = 0  # already queued, or finished and not yet due again
    over_limit: int = 0  # eligible, but past the per-page or per-source-per-cycle limit

    @property
    def accepted(self) -> int:
        return self.inserted + self.requeued


_CLAIM_SQL = """
with live as (
    select u.source_id, (s.collector = 'browser') as is_browser
      from crawl_urls u
      join sources s on s.id = u.source_id
     where u.status = 'running' and u.leased_until > now()
),
lane_live as (
    select count(*) as n from live where is_browser = $2
),
picked as (
    select c.id
      from crawl_urls c
      join sources s on s.id = c.source_id
     where c.status in ('queued', 'retry')
       and c.next_crawl_at <= now()
       and s.enabled
       and (s.collector = 'browser') = $2
       and ($1::bigint is null or c.cycle_id = $1)
       and (select n from lane_live) < $3
       and (select count(*) from live l where l.source_id = c.source_id) < $4
       -- Page 1 first. Pages 2..N are all queued up front, but none is claimed until page 1
       -- of the same cycle has been resolved: if the source is down, gated or moved, that is
       -- one wasted attempt instead of N, and `prune_listing` then skips the rest.
       and (not $7::boolean
            or c.kind <> 'listing'
            or coalesce(c.page_no, 1) <= 1
            or not exists (
                select 1 from crawl_urls p
                 where p.source_id = c.source_id and p.cycle_id = c.cycle_id
                   and p.kind = 'listing' and coalesce(p.page_no, 1) = 1
                   and p.status in ('queued', 'running', 'retry')))
     order by c.depth, c.page_no nulls last, c.next_crawl_at, c.id
     limit 1
       for update of c skip locked
)
update crawl_urls u
   set status = 'running',
       attempt = u.attempt + 1,
       leased_by = $5,
       leased_until = now() + make_interval(secs => $6::double precision),
       updated_at = now()
  from picked, sources s
 where u.id = picked.id and s.id = u.source_id
returning u.id, u.source_id, s.slug as source_slug, s.collector, u.cycle_id, u.url, u.kind,
          u.page_no, u.depth, u.attempt, u.parent_id, u.content_sha256, u.failure_count,
          s.crawl_interval_seconds as interval_seconds,
          s.deep_crawl_interval_seconds as deep_interval_seconds,
          s.base_url, s.active_url, s.item_selector
"""

_ENQUEUE_SQL = """
with incoming as (
    select * from unnest(
        $3::text[], $4::text[], $5::text[], $6::int[], $7::int[], $8::bigint[]
    ) as t(url, url_normalized, kind, page_no, depth, parent_id)
),
up as (
    insert into crawl_urls
        (source_id, cycle_id, url, url_normalized, kind, page_no, depth, parent_id)
    select $1, $2, url, url_normalized, kind::crawl_url_kind, page_no, depth, parent_id
      from incoming
    on conflict (source_id, url_normalized) do update
       set cycle_id = excluded.cycle_id,
           -- A finished URL that has come due is scheduled afresh. One that is already
           -- queued, running or waiting to retry keeps its state and is only adopted by this
           -- cycle; a finished one that is not yet due is left exactly as it is.
           status = case when crawl_urls.status in ('succeeded','failed','skipped')
                         then 'queued'::crawl_url_status else crawl_urls.status end,
           attempt = case when crawl_urls.status in ('succeeded','failed','skipped')
                          then 0 else crawl_urls.attempt end,
           result = case when crawl_urls.status in ('succeeded','failed','skipped')
                         then null else crawl_urls.result end,
           next_crawl_at = case when crawl_urls.status in ('succeeded','failed','skipped')
                                then now() else crawl_urls.next_crawl_at end,
           updated_at = now()
     where (crawl_urls.status in ('succeeded','failed','skipped')
            and (crawl_urls.next_crawl_at <= now() or $9::boolean))
        or crawl_urls.status in ('queued','retry')
    returning (xmax = 0) as inserted
)
select count(*) filter (where inserted) as inserted,
       count(*) filter (where not inserted) as requeued
  from up
"""


class CrawlQueue:
    """Postgres-backed frontier and job queue."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    # ---------------------------------------------------------------- cycles

    async def start_cycle(self, trigger: str, *, request_id: int | None = None) -> int | None:
        """Open a cycle. None means one is already running — the overlap guard.

        The guard is the partial unique index `crawl_cycles_one_running`, so it holds across
        processes and a race between two starters has exactly one winner.
        """
        try:
            return await self._pool.fetchval(
                "insert into crawl_cycles (trigger, request_id) values ($1, $2) returning id",
                trigger,
                request_id,
            )
        except asyncpg.UniqueViolationError:
            return None

    async def running_cycle(self) -> int | None:
        return await self._pool.fetchval(
            "select id from crawl_cycles where status = 'running' limit 1"
        )

    async def heartbeat(self, cycle_id: int) -> None:
        await self._pool.execute(
            "update crawl_cycles set heartbeat_at = now() where id = $1 and status = 'running'",
            cycle_id,
        )

    async def active_count(self, cycle_id: int) -> int:
        """URLs of this cycle that are not yet terminal: queued, running or waiting to retry."""
        return await self._pool.fetchval(
            """
            select count(*) from crawl_urls
             where cycle_id = $1 and status in ('queued', 'running', 'retry')
            """,
            cycle_id,
        )

    # ---------------------------------------------------------------- enqueue

    async def enqueue(
        self, cycle_id: int, source_id: int, urls: list[NewUrl], *, force: bool = False
    ) -> EnqueueResult:
        """Offer URLs to the queue. Duplicates — within the batch and against what exists —
        collapse on `(source_id, url_normalized)`; see `_ENQUEUE_SQL` for what is requeued."""
        return await self._enqueue(self._pool, cycle_id, source_id, urls, force)

    @staticmethod
    async def _enqueue(
        conn: Any, cycle_id: int, source_id: int, urls: list[NewUrl], force: bool = False
    ) -> EnqueueResult:
        unique: dict[str, NewUrl] = {}
        for item in urls:
            unique.setdefault(item.url_normalized, item)
        if not unique:
            return EnqueueResult()

        items = list(unique.values())
        row = await conn.fetchrow(
            _ENQUEUE_SQL,
            source_id,
            cycle_id,
            [i.url for i in items],
            [i.url_normalized for i in items],
            [i.kind for i in items],
            [i.page_no for i in items],
            [i.depth for i in items],
            [i.parent_id for i in items],
            force,
        )
        return EnqueueResult(inserted=row["inserted"], requeued=row["requeued"])

    # ---------------------------------------------------------------- claim

    async def claim(
        self,
        worker_id: str,
        *,
        browser: bool,
        lane_limit: int,
        per_source_limit: int,
        lease_seconds: float,
        cycle_id: int | None = None,
        page1_first: bool = False,
    ) -> ClaimedUrl | None:
        """Take one eligible URL, or None if nothing is claimable right now.

        `lane_limit` is the system-wide cap on live leases in this lane (http or browser) and
        `per_source_limit` the cap for any one source. Both are checked against the live
        leases under the claim lock, so they are exact rather than best effort.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("select pg_advisory_xact_lock($1)", _CLAIM_LOCK_KEY)
            row = await conn.fetchrow(
                _CLAIM_SQL,
                cycle_id,
                browser,
                lane_limit,
                per_source_limit,
                worker_id,
                float(lease_seconds),
                page1_first,
            )
        return ClaimedUrl(**dict(row)) if row else None

    # ---------------------------------------------------------------- outcomes
    #
    # Every outcome is guarded by `leased_by`. A worker whose lease expired and whose URL was
    # re-claimed elsewhere must not overwrite the new holder's result when it finally returns.

    async def complete(
        self,
        url_id: int,
        worker_id: str,
        *,
        result: str,
        http_status: int | None,
        response_ms: int | None,
        recrawl_after_seconds: float,
        content_sha256: str | None = None,
        leaks_found: int = 0,
        leaks_updated: int = 0,
        status: str = "succeeded",
    ) -> bool:
        """Record a finished URL. `content_sha256` is only ever passed by a caller whose
        ingest succeeded; None leaves the stored hash alone, so a page that failed to ingest
        is never later read as unchanged."""
        tag = await self._pool.execute(
            """
            update crawl_urls
               set status = $3::crawl_url_status,
                   result = $4,
                   http_status = $5,
                   response_ms = $6,
                   content_sha256 = coalesce($7, content_sha256),
                   leaks_found = $8,
                   leaks_updated = $10,
                   failure_count = 0,
                   last_error = null,
                   last_crawled_at = now(),
                   next_crawl_at = now() + make_interval(secs => $9::double precision),
                   leased_by = null, leased_until = null,
                   updated_at = now()
             where id = $1 and leased_by = $2 and status = 'running'
            """,
            url_id,
            worker_id,
            status,
            result,
            http_status,
            response_ms,
            content_sha256,
            leaks_found,
            float(recrawl_after_seconds),
            leaks_updated,
        )
        return tag.endswith(" 1")

    async def fail(
        self,
        url_id: int,
        worker_id: str,
        *,
        error: str,
        http_status: int | None,
        response_ms: int | None,
        transient: bool,
        max_attempts: int,
        retry_delay_seconds: float,
        recrawl_after_seconds: float,
        result: str | None = None,
    ) -> str | None:
        """Record a failed attempt. Returns the new status, or None if the lease was lost.

        A transient failure with attempts left becomes `retry`, due after the delay, and the
        worker is free the moment this returns. Anything else is `failed`, and the URL is not
        looked at again until its normal recrawl time.
        """
        return await self._pool.fetchval(
            """
            update crawl_urls
               set status = case when $4 and attempt < $5
                                 then 'retry'::crawl_url_status
                                 else 'failed'::crawl_url_status end,
                   failure_count = failure_count + 1,
                   last_error = left($3, 500),
                   -- A failure that produced no status or timing (a timeout) must not wipe
                   -- the last known ones; last_error says why this attempt failed.
                   http_status = coalesce($6, http_status),
                   response_ms = coalesce($7, response_ms),
                   result = coalesce($10, result),
                   last_crawled_at = now(),
                   next_crawl_at = case when $4 and attempt < $5
                                        then now() + make_interval(secs => $8::double precision)
                                        else now() + make_interval(secs => $9::double precision)
                                   end,
                   leased_by = null, leased_until = null,
                   updated_at = now()
             where id = $1 and leased_by = $2 and status = 'running'
            returning status::text
            """,
            url_id,
            worker_id,
            error,
            transient,
            max_attempts,
            http_status,
            response_ms,
            float(retry_delay_seconds),
            float(recrawl_after_seconds),
            result,
        )

    # ---------------------------------------------------------------- listing pagination

    async def observed_depth(self, source_id: int) -> int | None:
        """The deepest listing page of this source that has ever returned real content.

        None means none has — a source never crawled, or one whose page 1 has never worked —
        which is what makes the next seeding "a first crawl". Empty and failed pages do not
        count: a page 4 that came back empty is where the listing ended, not part of it.
        """
        return await self._pool.fetchval(
            """
            select max(page_no) from crawl_urls
             where source_id = $1 and kind = 'listing' and status = 'succeeded'
               and result in ('new', 'changed', 'unchanged')
            """,
            source_id,
        )

    async def prune_listing(self, source_id: int, cycle_id: int, after_page: int) -> int:
        """Skip this cycle's not-yet-started listing pages beyond `after_page`.

        Only `queued` and `retry` rows are touched. A page already running keeps running, and
        a page already finished stays as it is: pruning saves Tor requests that have not been
        made, it never cancels one that has. The row lock the UPDATE takes makes this safe
        against a worker claiming the page at the same instant — whoever locks the row first
        wins, and the loser re-reads its status.

        A skipped page is due again after the source's deep interval, like any listing page.
        """
        rows = await self._pool.fetch(
            """
            update crawl_urls u
               set status = 'skipped',
                   result = 'past_end',
                   next_crawl_at = now()
                       + make_interval(secs => s.deep_crawl_interval_seconds::double precision),
                   leased_by = null, leased_until = null,
                   updated_at = now()
              from sources s
             where s.id = u.source_id
               and u.source_id = $1 and u.cycle_id = $2
               and u.kind = 'listing' and u.page_no > $3
               and u.status in ('queued', 'retry')
            returning u.id
            """,
            source_id,
            cycle_id,
            after_page,
        )
        return len(rows)

    # ---------------------------------------------------------------- link discovery

    async def eligible_links(self, source_id: int, normalized: list[str]) -> set[str]:
        """Which of these links are worth queueing: unseen, or finished and due again.

        A link already queued, running or waiting to retry is on its way; one finished and
        not yet due has been looked at recently. Neither is offered again, which is what
        lets a page's limit of N links go to N *new* ones instead of the same top N each time.
        """
        if not normalized:
            return set()
        rows = await self._pool.fetch(
            """
            select u.n from unnest($2::text[]) as u(n)
             where not exists (
                select 1 from crawl_urls c
                 where c.source_id = $1 and c.url_normalized = u.n
                   and not (c.status in ('succeeded', 'failed', 'skipped')
                            and c.next_crawl_at <= now()))
            """,
            source_id,
            normalized,
        )
        return {r[0] for r in rows}

    async def enqueue_links(
        self,
        parent: ClaimedUrl,
        candidates: list[NewUrl],
        *,
        max_depth: int,
        per_page_limit: int,
        source_cycle_limit: int,
    ) -> LinkResult:
        """Queue links found on `parent`, within every limit. The only way links enter.

        Whatever the caller already checked, each candidate is validated again here against
        the *claimed URL's own source* — the discovered URL is filed under that source and no
        other, and must be on its host. Then, in this order:

        * depth: a link is at `parent.depth + 1`, never beyond `max_depth`;
        * eligibility: only unseen links, or finished ones that are due again;
        * `per_page_limit`: at most this many of what is left, in page order;
        * `source_cycle_limit`: at most this many link URLs per source per cycle in total,
          counted under a lock so concurrent workers cannot both spend the same budget.

        The unique index on (source_id, url_normalized) remains the final authority: if two
        workers still find the same URL, one row results, whatever happens above.
        """
        result = LinkResult()
        if parent.cycle_id is None or not candidates:
            return result
        depth = parent.depth + 1
        if depth > max_depth:
            return result

        source = await self._pool.fetchrow(
            "select base_url, active_url from sources where id = $1", parent.source_id
        )
        if source is None:
            return result
        hosts = {h for h in (host_of(source["base_url"]), host_of(source["active_url"] or "")) if h}

        valid = [c for c in candidates if admit_link(c.url, allowed_hosts=hosts)]
        result.rejected = len(candidates) - len(valid)

        eligible = await self.eligible_links(
            parent.source_id, list({c.url_normalized for c in valid})
        )
        fresh: list[NewUrl] = []
        seen: set[str] = set()
        for c in valid:
            if c.url_normalized in eligible and c.url_normalized not in seen:
                seen.add(c.url_normalized)
                fresh.append(c)
        result.known = len(valid) - len(fresh)

        taken = fresh[:per_page_limit]
        result.over_limit = len(fresh) - len(taken)
        if not taken:
            return result

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "select pg_advisory_xact_lock($1::int, $2::int)",
                _LINK_LOCK_CLASS,
                parent.source_id % (2**31),
            )
            used = await conn.fetchval(
                """
                select count(*) from crawl_urls
                 where source_id = $1 and cycle_id = $2 and kind = 'link'
                   and discovered_at >= (select started_at from crawl_cycles where id = $2)
                """,
                parent.source_id,
                parent.cycle_id,
            )
            room = max(0, source_cycle_limit - used)
            kept = taken[:room]
            result.over_limit += len(taken) - len(kept)
            if kept:
                queued = await self._enqueue(
                    conn,
                    parent.cycle_id,
                    parent.source_id,
                    [
                        NewUrl(
                            url=c.url,
                            url_normalized=c.url_normalized,
                            kind="link",
                            page_no=None,
                            depth=depth,
                            parent_id=parent.id,
                        )
                        for c in kept
                    ],
                )
                result.inserted, result.requeued = queued.inserted, queued.requeued
        return result

    # ---------------------------------------------------------------- crawl_runs

    async def ensure_run(self, cycle_id: int, source_id: int) -> int:
        """The `crawl_runs` row for this source in this cycle, created on first use.

        `raw_pages` rows carry the run they were fetched in, and the Sources page reads
        `crawl_runs`; both want exactly one row per source per cycle however many workers
        (or processes) reach the source first, so creation is done under a lock.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "select pg_advisory_xact_lock($1::int, $2::int)",
                _RUN_LOCK_CLASS,
                (cycle_id * 1_000_003 + source_id) % (2**31),
            )
            found = await conn.fetchval(
                "select id from crawl_runs where cycle_id = $1 and source_id = $2 limit 1",
                cycle_id,
                source_id,
            )
            if found is not None:
                return found
            return await conn.fetchval(
                "insert into crawl_runs (source_id, cycle_id) values ($1, $2) returning id",
                source_id,
                cycle_id,
            )

    # ---------------------------------------------------------------- due URLs and cycle scope

    @asynccontextmanager
    async def hold_cycle_open(self, cycle_id: int) -> AsyncIterator[bool]:
        """Keep a running cycle from being finalized while URLs are being added to it.

        Seeding is several statements; a finalizer that looked in between would see "nothing
        left" and close the cycle under the seeder. Both sides take the cycle row's lock —
        this a shared one, the finalizer an exclusive one — so whichever comes second waits.
        Yields False if the cycle is no longer running, and nothing should be added.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            found = await conn.fetchval(
                "select id from crawl_cycles where id = $1 and status = 'running' for share",
                cycle_id,
            )
            yield found is not None

    async def requeue_due_links(
        self,
        cycle_id: int,
        source_id: int,
        *,
        base_url: str,
        active_url: str | None,
        max_depth: int,
        max_pages: int,
        recheck_after_seconds: float,
    ) -> tuple[int, int]:
        """Queue followed pages whose recrawl time has come. Returns (requeued, rejected).

        This is what makes a followed page recrawlable without its parent listing having to
        change and re-announce it. Only finished pages (`succeeded`, `failed`, `skipped`) that
        are due are touched; one that is queued, running or waiting to retry is already
        scheduled and is never selected, and the unique key makes a second row impossible.

        Each URL is re-checked against the source's *current* host and the depth limit: a
        source that has moved, or a depth limit that was lowered, must not keep old URLs
        alive. Those are skipped and looked at again only after `recheck_after_seconds`.
        At most `max_pages` are taken per source per cycle, oldest-due first, so a large
        backlog is worked through over several cycles instead of arriving all at once.
        """
        hosts = {h for h in (host_of(base_url), host_of(active_url or "")) if h}
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "select pg_advisory_xact_lock($1::int, $2::int)",
                _LINK_LOCK_CLASS,
                source_id % (2**31),
            )
            adopted = await conn.fetchval(
                """
                select count(*) from crawl_urls
                 where source_id = $1 and cycle_id = $2 and kind = 'link'
                   and discovered_at < (select started_at from crawl_cycles where id = $2)
                """,
                source_id,
                cycle_id,
            )
            room = max(0, max_pages - adopted)
            if room == 0:
                return 0, 0
            due = await conn.fetch(
                """
                select id, url, depth from crawl_urls
                 where source_id = $1 and kind = 'link'
                   and status in ('succeeded', 'failed', 'skipped')
                   and next_crawl_at <= now()
                 order by next_crawl_at, id
                 limit $2
                   for update skip locked
                """,
                source_id,
                room,
            )
            ok_ids = [
                r["id"]
                for r in due
                if r["depth"] <= max_depth and admit_link(r["url"], allowed_hosts=hosts)
            ]
            bad_ids = [r["id"] for r in due if r["id"] not in set(ok_ids)]
            if bad_ids:
                await conn.execute(
                    """
                    update crawl_urls
                       set status = 'skipped', result = 'out_of_scope',
                           next_crawl_at = now() + make_interval(secs => $2::double precision),
                           updated_at = now()
                     where id = any($1::bigint[])
                    """,
                    bad_ids,
                    float(recheck_after_seconds),
                )
            if ok_ids:
                await conn.execute(
                    """
                    update crawl_urls
                       set status = 'queued', cycle_id = $2, attempt = 0, result = null,
                           next_crawl_at = now(), leased_by = null, leased_until = null,
                           updated_at = now()
                     where id = any($1::bigint[])
                    """,
                    ok_ids,
                    cycle_id,
                )
            return len(ok_ids), len(bad_ids)

    async def skip_disabled(self, cycle_id: int) -> int:
        """Skip waiting URLs of sources that were disabled after the cycle was seeded.

        A source switched off mid-cycle must stop being fetched (the claim already refuses
        its URLs), and its waiting rows must not hold the cycle open forever.
        """
        rows = await self._pool.fetch(
            """
            update crawl_urls u
               set status = 'skipped', result = 'source_disabled',
                   next_crawl_at = now()
                       + make_interval(secs => s.crawl_interval_seconds::double precision),
                   updated_at = now()
              from sources s
             where s.id = u.source_id and not s.enabled
               and u.cycle_id = $1 and u.status in ('queued', 'retry')
            returning u.id
            """,
            cycle_id,
        )
        return len(rows)

    async def cycle_source_ids(self, cycle_id: int) -> list[int]:
        rows = await self._pool.fetch(
            "select distinct source_id from crawl_urls where cycle_id = $1", cycle_id
        )
        return [r[0] for r in rows]

    async def cycle_status(self, cycle_id: int) -> str | None:
        return await self._pool.fetchval(
            "select status::text from crawl_cycles where id = $1", cycle_id
        )

    # ---------------------------------------------------------------- finalization

    async def finalize_cycle(self, cycle_id: int, *, abandon: bool = False) -> CycleReport | None:
        """Close a cycle and every source run in it, atomically. None if it was not closed.

        One transaction that takes the cycle row's lock first. Two workers finishing at the
        same moment both get here; one holds the lock and finalizes, the other waits and then
        finds the cycle no longer `running` and returns None. Neither can finalize twice.

        A cycle is complete only when none of its URLs is queued, running or waiting to retry.
        Callers reap expired leases first (a dead worker's URL is `running` until its lease
        runs out, and that is correctly "not finished"). `abandon=True` is the exception: it
        fails whatever is left and closes the cycle as `abandoned`.

        An empty *scheduled* cycle — nothing was due — is deleted rather than recorded, or the
        cron would write a row every few minutes saying nothing happened.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            cycle = await conn.fetchrow(
                """
                select id, trigger, started_at, status::text as status
                  from crawl_cycles where id = $1 for update
                """,
                cycle_id,
            )
            if cycle is None or cycle["status"] != "running":
                return None

            if abandon:
                await conn.execute(
                    """
                    update crawl_urls
                       set status = 'failed', last_error = 'cycle abandoned',
                           leased_by = null, leased_until = null,
                           next_crawl_at = now(), updated_at = now()
                     where cycle_id = $1 and status in ('queued', 'running', 'retry')
                    """,
                    cycle_id,
                )
            elif await conn.fetchval(
                """
                select count(*) from crawl_urls
                 where cycle_id = $1 and status in ('queued', 'running', 'retry')
                """,
                cycle_id,
            ):
                return None

            rows = await conn.fetch(
                """
                select u.source_id, s.slug, u.kind::text as kind, u.page_no,
                       u.status::text as status, u.result, u.http_status, u.last_error,
                       u.attempt, u.leaks_found, u.leaks_updated, u.discovered_at
                  from crawl_urls u join sources s on s.id = u.source_id
                 where u.cycle_id = $1
                """,
                cycle_id,
            )
            started = cycle["started_at"]
            now = await conn.fetchval("select now()")

            if not rows and cycle["trigger"] == "schedule" and not abandon:
                await conn.execute("delete from crawl_cycles where id = $1", cycle_id)
                empty = CycleSummary()
                return CycleReport(cycle_id, "schedule", "completed", started, now, 0, empty, True)

            by_source: dict[int, list[Any]] = defaultdict(list)
            slugs: dict[int, str] = {}
            for r in rows:
                by_source[r["source_id"]].append(r)
                slugs[r["source_id"]] = r["slug"]
            verdicts = [judge_source(sid, slugs[sid], rs) for sid, rs in by_source.items()]
            if abandon:
                # Nobody was working: that says nothing about the sites, so their health is
                # left exactly as it was.
                for v in verdicts:
                    v.update_health = False
            summary = build_summary(rows, verdicts, cycle_started_at=started)

            await self._close_runs(conn, cycle_id, verdicts)

            status = "abandoned" if abandon else summary.status
            error = summary.error
            if abandon:
                error = "abandoned: no worker made progress before the cycle timed out"
            duration_ms = int((now - started).total_seconds() * 1000)
            await conn.execute(
                """
                update crawl_cycles
                   set status = $2::crawl_cycle_status, completed_at = $3,
                       total_urls = $4, successful_urls = $5, failed_urls = $6,
                       skipped_urls = $7, new_pages = $8, changed_pages = $9,
                       unchanged_pages = $10, leaks_found = $11, leaks_updated = $12,
                       duration_ms = $13, summary = $14::jsonb, error = $15
                 where id = $1
                """,
                cycle_id,
                status,
                now,
                summary.total,
                summary.succeeded,
                summary.failed,
                summary.skipped,
                summary.new,
                summary.changed,
                summary.unchanged,
                summary.leaks_found,
                summary.leaks_updated,
                duration_ms,
                summary.to_json(),
                error,
            )
            return CycleReport(
                cycle_id, cycle["trigger"], status, started, now, duration_ms, summary
            )

    async def _close_runs(self, conn: Any, cycle_id: int, verdicts: list[SourceVerdict]) -> None:
        """Finish each source's `crawl_runs` row and, where it counts, the source's health.

        The `sources` update is the same one `Storage.finish_crawl` makes, so the Sources page
        reads exactly what it always has: a success clears `consecutive_failures` and stamps
        `last_success_at`, a failure adds one.
        """
        runs = {
            r["source_id"]: r["id"]
            for r in await conn.fetch(
                "select id, source_id from crawl_runs where cycle_id = $1", cycle_id
            )
        }
        bytes_by_run = {
            r[0]: r[1]
            for r in await conn.fetch(
                """
                select crawl_run_id, coalesce(sum(byte_size), 0) from raw_pages
                 where crawl_run_id = any($1::bigint[]) group by 1
                """,
                list(runs.values()),
            )
        }
        for v in verdicts:
            run_id = runs.get(v.source_id)
            if run_id is None:  # a source whose pages never reached ingestion (page 1 failed)
                run_id = await conn.fetchval(
                    "insert into crawl_runs (source_id, cycle_id) values ($1, $2) returning id",
                    v.source_id,
                    cycle_id,
                )
            await conn.execute(
                """
                update crawl_runs
                   set status = $2::crawl_status, depth = $3::crawl_depth, finished_at = now(),
                       pages_fetched = $4, pages_changed = $5, bytes_fetched = $6, error = $7
                 where id = $1
                """,
                run_id,
                v.status,
                "deep" if v.deep else "shallow",
                v.pages_fetched,
                v.pages_changed,
                int(bytes_by_run.get(run_id, 0)),
                v.error,
            )
            if not v.update_health:
                continue
            if v.status == "succeeded":
                await conn.execute(
                    """
                    update sources
                       set last_crawl_at = now(), last_success_at = now(),
                           last_deep_crawl_at = case when $2 then now()
                                                     else last_deep_crawl_at end,
                           consecutive_failures = 0, updated_at = now()
                     where id = $1
                    """,
                    v.source_id,
                    v.deep and v.failed == 0,
                )
            else:
                await conn.execute(
                    """
                    update sources
                       set last_crawl_at = now(),
                           consecutive_failures = consecutive_failures + 1,
                           updated_at = now()
                     where id = $1
                    """,
                    v.source_id,
                )

    async def abandon_stale_cycles(self, stale_seconds: float) -> list[CycleReport]:
        """Close cycles that have stopped making progress, as `abandoned`.

        A running cycle's heartbeat is bumped by every worker that is serving it. One that has
        not been touched for `stale_seconds` has no worker at all — the host went down and
        nothing has come back — and would otherwise block every new cycle indefinitely.
        """
        ids = await self._pool.fetch(
            """
            select id from crawl_cycles
             where status = 'running' and heartbeat_at < now() - make_interval(secs => $1)
            """,
            float(stale_seconds),
        )
        reports = []
        for (cycle_id,) in ids:
            report = await self.finalize_cycle(cycle_id, abandon=True)
            if report is not None:
                reports.append(report)
        return reports

    # ---------------------------------------------------------------- recovery

    async def reap_expired(
        self, *, max_attempts: int, backoff_base: float, backoff_cap: float
    ) -> tuple[int, int]:
        """Recover URLs whose lease ran out because the worker died. Returns (retried, failed).

        This is the only recovery for an individual URL. It needs no lock and no knowledge of
        which worker died: an expired lease is the whole signal. Attempts used by the dead
        worker count, so a URL that kills its worker every time is eventually failed rather
        than retried forever.
        """
        rows = await self._pool.fetch(
            """
            update crawl_urls
               set status = case when attempt < $1 then 'retry'::crawl_url_status
                                 else 'failed'::crawl_url_status end,
                   failure_count = failure_count + 1,
                   last_error = 'lease expired: worker stopped before finishing',
                   next_crawl_at = now() + make_interval(
                       secs => least($3::double precision,
                                     $2::double precision * power(2, greatest(attempt - 1, 0)))),
                   leased_by = null, leased_until = null,
                   updated_at = now()
             where status = 'running' and leased_until < now()
            returning status::text
            """,
            max_attempts,
            float(backoff_base),
            float(backoff_cap),
        )
        retried = sum(1 for r in rows if r[0] == "retry")
        return retried, len(rows) - retried

    async def stats(self, cycle_id: int) -> dict[str, Any]:
        """Counts by status for one cycle. Used by tests and, later, the finalizer."""
        rows = await self._pool.fetch(
            "select status::text, count(*) from crawl_urls where cycle_id = $1 group by 1",
            cycle_id,
        )
        return {r[0]: r[1] for r in rows}
