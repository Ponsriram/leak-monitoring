"""Database access. The only module in the pipeline that speaks SQL.

The schema is owned by Drizzle (`packages/db`), not by this service — migrations live there
and this reads/writes the tables they create. Raw SQL rather than an ORM keeps that boundary
obvious and keeps the upsert semantics explicit, because the upsert is the whole point.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

import asyncpg
import structlog

from .models import ExtractedLeak

log = structlog.get_logger(__name__)

# Arbitrary but fixed: advisory lock keys are a single global namespace, so this constant is
# what makes "the crawl lock" mean the same thing in the worker and in the CLI.
_CRAWL_LOCK_KEY = 0x1EA4_C0DE


@dataclass(slots=True)
class SourceRow:
    id: int
    slug: str
    name: str
    base_url: str
    collector: str
    pagination_style: str
    max_pages: int
    crawl_interval_seconds: int
    request_delay_seconds: int
    enabled: bool
    last_crawl_at: datetime | None
    consecutive_failures: int
    active_url: str | None = None

    @property
    def crawl_url(self) -> str:
        """The address to crawl: a failover address if one is in effect, else the configured one."""
        return self.active_url or self.base_url


@dataclass(slots=True)
class MirrorRow:
    id: int
    source_id: int
    url: str
    onion_host: str
    status: str
    times_seen: int
    last_ok_at: datetime | None


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    # Ids of rows that were genuinely new. Alert matching runs against exactly these, so a
    # re-crawl of an unchanged listing cannot re-notify anyone.
    new_leak_ids: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.skipped


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class Storage:
    """Thin repository over the Drizzle-owned schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> Self:
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        if pool is None:  # pragma: no cover - asyncpg only returns None on misuse
            raise RuntimeError("could not create a connection pool")
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # ---------- run lock ----------

    @asynccontextmanager
    async def crawl_lock(self, key: int = _CRAWL_LOCK_KEY) -> AsyncIterator[bool]:
        """Hold a database-wide advisory lock for the duration of a crawl.

        Two crawls of the same 32 sources through one Tor daemon do not go twice as fast;
        they compete for circuits and both get slower. That is not hypothetical — the
        scheduled run and a manual `intel run` overlapped on 2026-08-16 and the sources that
        failed with "TTL expired" were the ones fetched while both were in flight.

        A Postgres advisory lock rather than a Redis key because the CLI already has a
        database connection and does not have a Redis one, so this is the only lock both the
        worker and the CLI can take. It is held by the connection, so a crashed process
        releases it automatically — no stale lock to clear by hand.

        Yields True if the lock was taken, False if another crawl already holds it.
        """
        async with self._pool.acquire() as conn:
            acquired = await conn.fetchval("select pg_try_advisory_lock($1)", key)
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    await conn.execute("select pg_advisory_unlock($1)", key)

    # ---------- sources ----------

    async def list_sources(self, *, only_enabled: bool = True) -> list[SourceRow]:
        rows = await self._pool.fetch(
            """
            select id, slug, name, base_url, collector, pagination_style, max_pages,
                   crawl_interval_seconds, request_delay_seconds, enabled,
                   last_crawl_at, consecutive_failures, active_url
            from sources
            where (not $1::boolean) or enabled
            order by slug
            """,
            only_enabled,
        )
        return [SourceRow(**dict(row)) for row in rows]

    async def get_source(self, slug: str) -> SourceRow | None:
        row = await self._pool.fetchrow(
            """
            select id, slug, name, base_url, collector, pagination_style, max_pages,
                   crawl_interval_seconds, request_delay_seconds, enabled,
                   last_crawl_at, consecutive_failures, active_url
            from sources where slug = $1
            """,
            slug,
        )
        return SourceRow(**dict(row)) if row else None

    async def prune_sources(self, keep_slugs: list[str]) -> list[str]:
        """Delete sources not present in the given list. Returns the slugs removed.

        Opt-in rather than automatic, because deleting a source cascades: its `crawl_runs`
        and `raw_pages` go with it. Extracted `leaks` survive — their `source_id` is
        ON DELETE SET NULL — so pruning a dead site never destroys collected intelligence,
        only the crawl bookkeeping that led to it.
        """
        rows = await self._pool.fetch(
            "delete from sources where slug <> all($1::text[]) returning slug",
            keep_slugs,
        )
        return [row["slug"] for row in rows]

    async def sync_sources(self, definitions: list[dict[str, Any]]) -> tuple[int, int]:
        """Reconcile sources.yaml into the database. Returns (inserted, updated).

        Config fields are overwritten from the file — it is the source of truth for them —
        but health columns (`last_crawl_at`, `consecutive_failures`) are left alone, since
        those are runtime state the file knows nothing about.

        `enabled` is in the second group, not the first. Every entry in the file ships
        `enabled: false` by design, and turning a source on is done with
        `intel sources enable`; when sync also wrote that column, editing any unrelated
        field and re-syncing silently switched collection off for all 32 sources. The
        file's value is applied on INSERT, so a new source still arrives disabled — which
        is the property that comment above the source list is actually protecting.
        `intel sources disable` remains the way to turn one off.
        """
        inserted = updated = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for item in definitions:
                result = await conn.fetchrow(
                    """
                    insert into sources (
                        slug, name, base_url, collector, pagination_style, max_pages,
                        crawl_interval_seconds, request_delay_seconds, enabled, notes
                    )
                    values ($1,$2,$3,$4::collector_kind,$5,$6,$7,$8,$9,$10)
                    on conflict (slug) do update set
                        name = excluded.name,
                        base_url = excluded.base_url,
                        collector = excluded.collector,
                        pagination_style = excluded.pagination_style,
                        max_pages = excluded.max_pages,
                        crawl_interval_seconds = excluded.crawl_interval_seconds,
                        request_delay_seconds = excluded.request_delay_seconds,
                        -- enabled is deliberately absent: see the docstring.
                        notes = excluded.notes,
                        updated_at = now()
                    returning (xmax = 0) as was_inserted
                    """,
                    item["slug"],
                    item.get("name", item["slug"]),
                    item["base_url"],
                    item.get("collector", "http"),
                    item.get("pagination_style", "none"),
                    int(item.get("max_pages", 10)),
                    int(item.get("crawl_interval_seconds", 3600)),
                    int(item.get("request_delay_seconds", 10)),
                    bool(item.get("enabled", True)),
                    item.get("notes"),
                )
                if result and result["was_inserted"]:
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated

    # ---------- crawl bookkeeping ----------

    async def start_crawl(self, source_id: int) -> int:
        return await self._pool.fetchval(
            "insert into crawl_runs (source_id, status) values ($1, 'running') returning id",
            source_id,
        )

    async def finish_crawl(
        self,
        run_id: int,
        source_id: int,
        *,
        status: str,
        pages_fetched: int = 0,
        pages_changed: int = 0,
        bytes_fetched: int = 0,
        error: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                update crawl_runs
                   set status = $2::crawl_status, finished_at = now(),
                       pages_fetched = $3, pages_changed = $4,
                       bytes_fetched = $5, error = $6
                 where id = $1
                """,
                run_id,
                status,
                pages_fetched,
                pages_changed,
                bytes_fetched,
                error,
            )
            # Health lives on the source so the dashboard can show it without a join.
            if status == "succeeded":
                await conn.execute(
                    """
                    update sources
                       set last_crawl_at = now(), last_success_at = now(),
                           consecutive_failures = 0, updated_at = now()
                     where id = $1
                    """,
                    source_id,
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
                    source_id,
                )

    async def save_page(
        self,
        *,
        source_id: int,
        crawl_run_id: int | None,
        url: str,
        page_no: int,
        text: str,
    ) -> tuple[int, bool]:
        """Store a fetched page. Returns (raw_page_id, changed).

        `changed` is False when this exact content has been seen for this source before —
        the caller then skips extraction entirely. This is what turns "reprocess the whole
        corpus every run" into "only handle what actually changed".
        """
        digest = content_hash(text)

        existing = await self._pool.fetchval(
            "select id from raw_pages where source_id = $1 and content_sha256 = $2 limit 1",
            source_id,
            digest,
        )
        if existing is not None:
            return existing, False

        page_id = await self._pool.fetchval(
            """
            insert into raw_pages
                (source_id, crawl_run_id, url, page_no, content_sha256, text, byte_size)
            values ($1,$2,$3,$4,$5,$6,$7)
            returning id
            """,
            source_id,
            crawl_run_id,
            url,
            page_no,
            digest,
            text,
            len(text.encode("utf-8")),
        )
        return page_id, True

    async def mark_extracted(self, raw_page_id: int) -> None:
        await self._pool.execute(
            "update raw_pages set extracted_at = now() where id = $1", raw_page_id
        )

    # ---------- leaks ----------

    async def upsert_leaks(
        self, leaks: list[ExtractedLeak], *, source_id: int | None
    ) -> UpsertResult:
        """Insert new leaks, refresh ones we have already seen.

        The critical detail is what the DO UPDATE clause does NOT touch: `first_seen_at`.
        That column is written once, on insert, and is what makes "new since yesterday"
        answerable. `last_seen_at` advances on every sighting, which is what makes "is this
        listing still up?" answerable.
        """
        result = UpsertResult()
        if not leaks:
            return result

        now = datetime.now(UTC)

        async with self._pool.acquire() as conn, conn.transaction():
            for leak in leaks:
                if not leak.is_usable:
                    result.skipped += 1
                    continue

                row = await conn.fetchrow(
                    """
                    insert into leaks (
                        dedupe_hash, victim_name, victim_domain, victim_country,
                        victim_sector, actor_group, source_id, source_url,
                        published_at, published_at_raw, first_seen_at, last_seen_at,
                        status, leak_type, leak_size_bytes, extraction
                    )
                    values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,
                            $12::leak_status,$13,$14,$15::jsonb)
                    on conflict (dedupe_hash) do update set
                        -- first_seen_at is deliberately absent.
                        last_seen_at = excluded.last_seen_at,
                        victim_name = coalesce(excluded.victim_name, leaks.victim_name),
                        victim_domain = coalesce(excluded.victim_domain, leaks.victim_domain),
                        published_at = coalesce(excluded.published_at, leaks.published_at),
                        published_at_raw = coalesce(
                            excluded.published_at_raw, leaks.published_at_raw
                        ),
                        -- Never downgrade a known status to 'unknown'.
                        --
                        -- This was an unconditional `= excluded.status` while every other
                        -- mutable column used coalesce. Extraction only reruns when a page's
                        -- content hash changes, so an edit anywhere on the page re-derived
                        -- every listing on it — and any listing whose status wording moved
                        -- out of the extractor's reach silently reverted from 'published'
                        -- to 'unknown'. A real state change still writes through, because
                        -- that arrives as a status other than 'unknown'.
                        status = case
                            when excluded.status = 'unknown'::leak_status then leaks.status
                            else excluded.status
                        end,
                        leak_size_bytes = coalesce(
                            excluded.leak_size_bytes, leaks.leak_size_bytes
                        ),
                        source_url = coalesce(excluded.source_url, leaks.source_url),
                        extraction = excluded.extraction,
                        updated_at = now()
                    returning (xmax = 0) as was_inserted, id
                    """,
                    leak.dedupe_hash,
                    leak.victim_name,
                    leak.victim_domain,
                    leak.victim_country,
                    leak.victim_sector,
                    leak.actor_group,
                    source_id,
                    leak.source_url,
                    leak.published_at,
                    leak.published_at_raw,
                    now,
                    leak.status.value,
                    leak.leak_type,
                    leak.leak_size_bytes,
                    leak.extraction.model_dump_json(),
                )

                if row is not None and row["was_inserted"]:
                    result.inserted += 1
                    result.new_leak_ids.append(row["id"])
                else:
                    result.updated += 1

        return result

    async def count_leaks(self) -> int:
        return await self._pool.fetchval("select count(*) from leaks")

    async def clear_fragment_victim_names(self, fragments: list[str]) -> int:
        """Blank victim names that are only name fragments, where a domain identifies the row.

        A re-crawl cannot fix these on its own: the upsert keeps the existing name with
        `coalesce(excluded.victim_name, leaks.victim_name)`, so a corrected extraction that
        now yields no name leaves the old wrong one in place. That coalesce is right — it
        stops a bad parse erasing a good name — which is why this is a separate, explicit
        repair rather than a change to the write path.
        """
        return await self._pool.fetchval(
            """
            with u as (
                update leaks set victim_name = null, updated_at = now()
                 where victim_name is not null
                   and victim_domain is not null
                   -- every word of the name is a fragment
                   and not exists (
                       select 1 from unnest(string_to_array(lower(victim_name), ' ')) as w
                        where btrim(w, '.,') <> all($1::text[])
                   )
                returning 1
            )
            select count(*) from u
            """,
            fragments,
        )

    async def latest_pages(self) -> list[tuple[int, str, str]]:
        """The most recently stored page per source, as (source_id, slug, text).

        `raw_pages` keeps the text of everything fetched, which is what makes it possible to
        re-run a corrected extractor over history instead of waiting to re-crawl.
        """
        rows = await self._pool.fetch(
            """
            select distinct on (r.source_id) r.source_id, s.slug, r.text
              from raw_pages r
              join sources s on s.id = r.source_id
             order by r.source_id, r.id desc
            """
        )
        return [(row["source_id"], row["slug"], row["text"]) for row in rows]

    async def repair_victim_domain(
        self, *, actor_group: str, victim_name: str, victim_domain: str, new_hash: str
    ) -> str:
        """Correct one row's domain and identity. Returns what happened.

        The domain is half of `dedupe_hash`, so fixing it means re-keying the row. Doing
        that in place — rather than deleting and re-collecting — is what preserves
        `first_seen_at`, and `first_seen_at` is the column the entire "what is new" premise
        of the product rests on.

        Three outcomes, all of them normal:
          `repaired`  the row was re-keyed.
          `merged`    a row already existed under the correct identity, so the misfiled one
                      is folded into it, keeping the earlier `first_seen_at` of the two.
          `missing`   nothing matched; the listing is simply not in the database.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                select id, dedupe_hash, first_seen_at from leaks
                 where actor_group = $1 and lower(victim_name) = lower($2)
                 order by first_seen_at
                 limit 1
                """,
                actor_group, victim_name,
            )
            if row is None:
                return "missing"
            if row["dedupe_hash"] == new_hash:
                return "missing"  # already correct, nothing to do

            clash = await conn.fetchrow(
                "select id, first_seen_at from leaks where dedupe_hash = $1", new_hash
            )
            if clash is not None and clash["id"] != row["id"]:
                # The correct identity is already present. Keep the earlier sighting and
                # drop the misfiled duplicate rather than failing on the unique index.
                earliest = min(row["first_seen_at"], clash["first_seen_at"])
                await conn.execute(
                    "update leaks set first_seen_at = $2, updated_at = now() where id = $1",
                    clash["id"], earliest,
                )
                await conn.execute("delete from leaks where id = $1", row["id"])
                return "merged"

            await conn.execute(
                """
                update leaks
                   set victim_domain = $2, dedupe_hash = $3, updated_at = now()
                 where id = $1
                """,
                row["id"], victim_domain, new_hash,
            )
            return "repaired"

    # ---------- alerts ----------

    async def match_alerts(self, leak_ids: list[int]) -> int:
        """Match the given leaks against every enabled alert. Returns events created.

        Lives here rather than in `tasks.py` so the CLI and the scheduled worker run exactly
        the same matcher — previously this SQL was only reachable from an arq task that
        nothing ever enqueued, so no alert had ever fired.

        Matching is expressed as SQL over typed matchers, never a regex built from user
        input: an alert's `match_kind` is one of four fixed behaviours, so there is no
        pattern for a user to make pathological.

        Writing to `alert_events` is idempotent by construction — UNIQUE (alert_id, leak_id)
        means a retry or a duplicate message cannot produce a second notification.
        """
        if not leak_ids:
            return 0

        matched = await self._pool.fetchval(
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
                         when 'exact' then
                             lower(coalesce(l.victim_name, '')) = a.match_value
                         when 'domain' then
                             lower(coalesce(l.victim_domain, '')) = a.match_value
                             or lower(coalesce(l.victim_domain, '')) like '%.' || a.match_value
                         when 'substring' then
                             position(a.match_value in
                                      lower(coalesce(l.victim_name, '') || ' ' ||
                                            coalesce(l.victim_domain, ''))) > 0
                         when 'actor_group' then
                             l.actor_group = a.match_value
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
        return int(matched or 0)

    # ---------- mirrors ----------

    async def known_onion_hosts(self) -> set[str]:
        """Every onion host already accounted for, across base, active and mirror addresses.

        Used to keep a source's own address — and the addresses of sources already
        monitored — out of its candidate list, so what remains is genuinely new.
        """
        rows = await self._pool.fetch(
            """
            select base_url as url from sources
            union all
            select active_url from sources where active_url is not null
            union all
            select url from source_mirrors
            """
        )
        hosts: set[str] = set()
        for row in rows:
            host = _onion_host(row["url"])
            if host:
                hosts.add(host)
        return hosts

    async def record_mirrors(
        self,
        source_id: int,
        mirrors: dict[str, str],
        *,
        discovered_from: str | None,
        status: str = "candidate",
    ) -> int:
        """Record onion addresses seen on a source's page. Returns how many were new.

        Recorded, not followed. `status` stays at `candidate` unless the caller has a reason
        to say otherwise, and only `approved` and `self_declared` are ever used for failover
        — see the note on `mirror_status` in the schema for why that distinction exists.
        """
        if not mirrors:
            return 0

        new = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for host, url in mirrors.items():
                was_inserted = await conn.fetchval(
                    """
                    insert into source_mirrors
                        (source_id, url, onion_host, discovered_from_url, status)
                    values ($1,$2,$3,$4,$5::mirror_status)
                    on conflict (source_id, onion_host) do update set
                        times_seen = source_mirrors.times_seen + 1,
                        last_seen_at = now(),
                        -- A rejected address stays rejected however often it reappears;
                        -- an operator said no, and seeing it again is not new information.
                        status = case
                            when source_mirrors.status = 'rejected'::mirror_status
                                then source_mirrors.status
                            when source_mirrors.status = 'candidate'::mirror_status
                                then excluded.status
                            else source_mirrors.status
                        end
                    returning (xmax = 0) as was_inserted
                    """,
                    source_id, url, host, discovered_from, status,
                )
                if was_inserted:
                    new += 1
        return new

    async def failover_mirrors(self, source_id: int) -> list[MirrorRow]:
        """Addresses worth trying when a source's primary address is dead.

        Ordered by how much they have been vouched for: an operator-approved address first,
        then one the site published about itself, then by how many crawls have seen it.
        Plain `candidate` rows — addresses that merely appeared somewhere on a page — are
        excluded entirely.
        """
        rows = await self._pool.fetch(
            """
            select id, source_id, url, onion_host, status::text, times_seen, last_ok_at
              from source_mirrors
             where source_id = $1
               and status in ('approved'::mirror_status, 'self_declared'::mirror_status)
             order by (status = 'approved'::mirror_status) desc,
                      last_ok_at desc nulls last,
                      times_seen desc
             limit 5
            """,
            source_id,
        )
        return [MirrorRow(**dict(row)) for row in rows]

    async def list_mirrors(self, slug: str | None = None) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            select s.slug, m.url, m.onion_host, m.status::text as status, m.times_seen,
                   m.first_seen_at, m.last_ok_at, m.discovered_from_url
              from source_mirrors m
              join sources s on s.id = m.source_id
             where $1::text is null or s.slug = $1
             order by s.slug, m.times_seen desc
            """,
            slug,
        )
        return [dict(row) for row in rows]

    async def set_mirror_status(self, slug: str, onion_host: str, status: str) -> int:
        return await self._pool.fetchval(
            """
            with u as (
                update source_mirrors m
                   set status = $3::mirror_status
                  from sources s
                 where s.id = m.source_id and s.slug = $1 and m.onion_host = $2
                returning 1
            )
            select count(*) from u
            """,
            slug, onion_host, status,
        )

    async def promote_mirror(self, source_id: int, url: str) -> None:
        """Make `url` the address this source is crawled at from now on.

        Written to `sources.active_url`, not `base_url`: `sources.yaml` owns `base_url` and
        `intel sources sync` rewrites it, which would silently undo the failover.
        """
        host = _onion_host(url)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "update sources set active_url = $2, updated_at = now() where id = $1",
                source_id, url,
            )
            await conn.execute(
                """
                update source_mirrors set last_ok_at = now()
                 where source_id = $1 and onion_host = $2
                """,
                source_id, host,
            )

    async def clear_active_url(self, slug: str) -> int:
        return await self._pool.fetchval(
            "with u as (update sources set active_url = null where slug = $1 returning 1) "
            "select count(*) from u",
            slug,
        )


_ONION_HOST_RE = re.compile(r"\b([a-z2-7]{56}\.onion)\b", re.I)


def _onion_host(url: str | None) -> str | None:
    """Local copy of the host parser, so storage does not import the collector package."""
    if not url:
        return None
    match = _ONION_HOST_RE.search(url)
    return match.group(1).lower() if match else None
