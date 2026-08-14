"""Database access. The only module in the pipeline that speaks SQL.

The schema is owned by Drizzle (`packages/db`), not by this service — migrations live there
and this reads/writes the tables they create. Raw SQL rather than an ORM keeps that boundary
obvious and keeps the upsert semantics explicit, because the upsert is the whole point.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

import asyncpg
import structlog

from .models import ExtractedLeak

log = structlog.get_logger(__name__)


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


@dataclass(slots=True)
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0

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

    # ---------- sources ----------

    async def list_sources(self, *, only_enabled: bool = True) -> list[SourceRow]:
        rows = await self._pool.fetch(
            """
            select id, slug, name, base_url, collector, pagination_style, max_pages,
                   crawl_interval_seconds, request_delay_seconds, enabled,
                   last_crawl_at, consecutive_failures
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
                   last_crawl_at, consecutive_failures
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
                        enabled = excluded.enabled,
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
                        status = excluded.status,
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
                else:
                    result.updated += 1

        return result

    async def count_leaks(self) -> int:
        return await self._pool.fetchval("select count(*) from leaks")
