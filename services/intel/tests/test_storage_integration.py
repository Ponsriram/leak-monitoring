"""Storage tests against a real Postgres.

Skipped automatically when the database is unreachable, so `pytest` still passes on a
machine with nothing running. Bring it up with `npm run infra:up` from the repo root.

These verify the upsert contract, which is the single most important behaviour in the
pipeline: re-running must refresh rows, never duplicate them, and must never move
`first_seen_at`.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from intel.config import get_settings
from intel.models import ExtractedLeak, ExtractionMeta, ExtractionMethod, LeakStatus
from intel.storage import Storage, content_hash

# No module-level `pytest.mark.asyncio`: `asyncio_mode = "auto"` in pyproject.toml already
# runs async tests, and the blanket mark would also be applied to the sync test below.


async def _connect() -> Storage | None:
    try:
        return await Storage.connect(get_settings().asyncpg_dsn, max_size=2)
    except (OSError, asyncpg.PostgresError):
        return None


@pytest.fixture
async def storage():  # type: ignore[no-untyped-def]
    store = await _connect()
    if store is None:
        pytest.skip("Postgres not reachable — run `npm run infra:up` from the repo root")
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def group(storage: Storage):  # type: ignore[no-untyped-def]
    """A unique actor group per test, cleaned up afterwards.

    These run against the developer's real database, so they must leave it exactly as they
    found it. An earlier version only generated the unique group and never deleted the rows,
    which quietly accumulated junk leaks in the dev dataset — visible in the dashboard's
    "groups tracked" tile.
    """
    slug = f"test-{uuid.uuid4().hex[:10]}"
    try:
        yield slug
    finally:
        await storage._pool.execute(  # noqa: SLF001 - test teardown
            "delete from leaks where actor_group = $1", slug
        )


def make_leak(group: str, victim: str = "Northwind Logistics", **kwargs: object) -> ExtractedLeak:
    base: dict[str, object] = {
        "victim_name": victim,
        "victim_domain": f"{victim.split()[0].lower()}.example",
        "actor_group": group,
        "extraction": ExtractionMeta(method=ExtractionMethod.RULES),
    }
    base.update(kwargs)
    return ExtractedLeak(**base)  # type: ignore[arg-type]


async def test_insert_then_reinsert_does_not_duplicate(storage: Storage, group: str) -> None:
    """The defect that made the old pipeline double the dataset on every run."""
    leak = make_leak(group)

    first = await storage.upsert_leaks([leak], source_id=None)
    assert first.inserted == 1
    assert first.updated == 0

    second = await storage.upsert_leaks([leak], source_id=None)
    assert second.inserted == 0
    assert second.updated == 1

    third = await storage.upsert_leaks([leak, leak, leak], source_id=None)
    assert third.inserted == 0


async def test_first_seen_at_survives_upsert(storage: Storage, group: str) -> None:
    """`first_seen_at` is what makes "new since yesterday" answerable. It must not move."""
    leak = make_leak(group)
    await storage.upsert_leaks([leak], source_id=None)

    before = await storage._pool.fetchrow(  # noqa: SLF001
        "select first_seen_at, last_seen_at from leaks where dedupe_hash = $1",
        leak.dedupe_hash,
    )

    # Second sighting, with changed mutable fields.
    await storage.upsert_leaks(
        [make_leak(group, status=LeakStatus.PUBLISHED, leak_size_bytes=999)],
        source_id=None,
    )

    after = await storage._pool.fetchrow(  # noqa: SLF001
        "select first_seen_at, last_seen_at, status, leak_size_bytes "
        "from leaks where dedupe_hash = $1",
        leak.dedupe_hash,
    )

    assert after["first_seen_at"] == before["first_seen_at"], "first_seen_at must not move"
    assert after["last_seen_at"] >= before["last_seen_at"], "last_seen_at must advance"
    assert after["status"] == "published", "mutable fields should update"
    assert after["leak_size_bytes"] == 999


async def test_upsert_does_not_null_out_known_fields(storage: Storage, group: str) -> None:
    """A later, sparser extraction must not erase data an earlier one captured."""
    await storage.upsert_leaks(
        [make_leak(group, published_at_raw="2026-02-10", leak_size_bytes=1024)],
        source_id=None,
    )

    sparse = make_leak(group)  # no size, no raw date
    await storage.upsert_leaks([sparse], source_id=None)

    row = await storage._pool.fetchrow(  # noqa: SLF001
        "select leak_size_bytes, published_at_raw from leaks where dedupe_hash = $1",
        sparse.dedupe_hash,
    )
    assert row["leak_size_bytes"] == 1024
    assert row["published_at_raw"] == "2026-02-10"


async def test_unusable_leaks_are_skipped_not_inserted(storage: Storage, group: str) -> None:
    nameless = ExtractedLeak(
        actor_group=group,
        extraction=ExtractionMeta(method=ExtractionMethod.RULES),
    )
    result = await storage.upsert_leaks([nameless], source_id=None)
    assert result.inserted == 0
    assert result.skipped == 1


async def test_page_content_hash_short_circuits(storage: Storage) -> None:
    """Unchanged pages must report changed=False so extraction is skipped entirely."""
    sources = await storage.list_sources(only_enabled=False)
    if not sources:
        pytest.skip("no sources in the database — run `intel sources sync`")

    source = sources[0]
    text = f"unique page content {uuid.uuid4().hex}"
    page_id = None

    try:
        page_id, changed = await storage.save_page(
            source_id=source.id, crawl_run_id=None, url="http://x.onion/1", page_no=1, text=text
        )
        assert changed is True

        same_id, changed_again = await storage.save_page(
            source_id=source.id, crawl_run_id=None, url="http://x.onion/1", page_no=1, text=text
        )
        assert changed_again is False, "identical content must not be reprocessed"
        assert same_id == page_id
    finally:
        if page_id is not None:
            await storage._pool.execute(  # noqa: SLF001 - test teardown
                "delete from raw_pages where id = $1", page_id
            )


def test_content_hash_is_stable() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
