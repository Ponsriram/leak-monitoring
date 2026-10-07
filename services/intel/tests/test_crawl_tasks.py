# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""The arq jobs that drive cycles: scheduled sweep, Sync requests, engine switch, cron timing.

The jobs are called the way arq calls them — a function taking a context dict — against the
scratch Postgres, with a scripted network. What the Sources page and Sync button read (the
`sources`, `crawl_runs` and `crawl_requests` columns) is asserted at the end of the relevant
flows, since that is the data contract that must not change.
"""

from __future__ import annotations

import asyncio

import pytest
from test_crawl_cycles import World, blog, cfg
from test_crawl_discovery import new_source
from test_crawl_fetch import FakeIngestStorage
from test_crawl_queue import pool, queue  # noqa: F401 - fixtures
from test_pipeline_concurrency import make_settings

from intel import tasks
from intel.crawl.manager import RunOutcome
from intel.pipeline import RunResult
from intel.storage import Storage


@pytest.fixture(autouse=True)
async def clean_requests(pool):  # type: ignore[no-untyped-def]
    await pool.execute("delete from crawl_requests")
    yield
    await pool.execute("delete from crawl_requests")


def make_ctx(pool, world, **settings):  # type: ignore[no-untyped-def]
    return {
        "storage": Storage(pool),
        "settings": cfg(**settings),
        "crawl_collectors": {"http": world},
        "crawl_ingest_storage": FakeIngestStorage(),
    }


async def request(pool, slug: str | None = None) -> int:  # type: ignore[no-untyped-def]
    return await pool.fetchval(
        "insert into crawl_requests (source_slug, requested_by) values ($1, 'tester') returning id",
        slug,
    )


async def request_row(pool, request_id: int):  # type: ignore[no-untyped-def]
    return await pool.fetchrow(
        "select *, status::text as st from crawl_requests where id = $1", request_id
    )


# ---------------------------------------------------------------- the sweep


async def test_the_scheduled_sweep_starts_a_cycle_and_then_finds_nothing_due(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    ctx = make_ctx(pool, World(blog(a, "/v/1")))

    first = await tasks.crawl_due(ctx)
    second = await tasks.crawl_due(ctx)

    assert first["status"] == "completed" and first["sources"] == 1 and first["urls"] == 2
    assert second == {"skipped": "nothing is due"}
    assert await pool.fetchval("select count(*) from crawl_cycles") == 1


async def test_the_legacy_engine_still_runs_the_old_crawler(pool, queue, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called: dict[str, object] = {}

    async def fake_locked(**kwargs: object) -> RunResult:
        called.update(kwargs)
        return RunResult()

    monkeypatch.setattr(tasks, "run_pipeline_locked", fake_locked)
    a = await new_source(pool)
    ctx = make_ctx(pool, World(blog(a)), CRAWL_ENGINE="legacy")

    result = await tasks.crawl_due(ctx)

    assert called["only_due"] is True  # the old source-at-a-time sweep ran
    assert result["sources"] == 0
    assert await pool.fetchval("select count(*) from crawl_cycles") == 0  # and no cycle exists


async def test_crawl_all_forces_a_manual_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a))
    ctx = make_ctx(pool, world)
    await tasks.crawl_due(ctx)

    result = await tasks.crawl_all(ctx)  # not due, but "sync everything" means now

    assert result["status"] == "completed" and result["sources"] == 1
    assert world.count(f"http://{a.slug}.onion/list") == 2
    assert (await pool.fetch("select trigger from crawl_cycles order by id"))[-1][
        "trigger"
    ] == "manual"


async def test_crawl_one(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a), blog(b))
    ctx = make_ctx(pool, world)

    assert (await tasks.crawl_one(ctx, "no-such"))["error"].startswith("no source")
    result = await tasks.crawl_one(ctx, a.slug)

    assert result["sources"] == 1
    assert world.count(f"http://{b.slug}.onion/list") == 0


# ---------------------------------------------------------------- Sync requests


async def test_a_sync_request_is_answered_in_the_vocabulary_the_ui_reads(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    ctx = make_ctx(pool, World(blog(a, "/v/1"), blog(b)))
    rid = await request(pool)

    result = await tasks.drain_crawl_requests(ctx)

    row = await request_row(pool, rid)
    assert result["handled"] == 1
    assert row["st"] == "succeeded"
    assert (row["sources_crawled"], row["failed_sources"]) == (2, 0)
    assert row["new_leaks"] == 4 and row["updated_leaks"] == 0  # "4 new, 0 seen again"
    assert row["started_at"] is not None and row["finished_at"] is not None and row["error"] is None
    cycle = await pool.fetchrow("select * from crawl_cycles")
    assert cycle["request_id"] == rid and cycle["trigger"] == "manual"


async def test_a_sync_reports_failed_sources_the_way_it_always_has(pool, queue) -> None:  # type: ignore[no-untyped-def]
    from test_crawl_fetch import fail

    good, bad = await new_source(pool), await new_source(pool)
    dead = blog(bad)
    dead.content[dead.listing()] = fail("permanent", 403, "HTTP 403")
    ctx = make_ctx(pool, World(blog(good), dead))
    rid = await request(pool)

    await tasks.drain_crawl_requests(ctx)

    row = await request_row(pool, rid)
    assert (row["st"], row["sources_crawled"], row["failed_sources"]) == ("succeeded", 2, 1)


async def test_a_sync_where_every_source_failed_is_failed(pool, queue) -> None:  # type: ignore[no-untyped-def]
    from test_crawl_fetch import fail

    bad = await new_source(pool)
    dead = blog(bad)
    dead.content[dead.listing()] = fail("permanent", 403, "HTTP 403")
    rid = await request(pool)

    await tasks.drain_crawl_requests(make_ctx(pool, World(dead)))

    row = await request_row(pool, rid)
    assert (
        row["st"] == "failed" and bad.slug in row["error"] and "every source failed" in row["error"]
    )


async def test_a_sync_matching_nothing_is_skipped(pool, queue) -> None:  # type: ignore[no-untyped-def]
    rid = await request(pool, "no-such-source")
    await tasks.drain_crawl_requests(make_ctx(pool, World()))

    row = await request_row(pool, rid)
    assert (row["st"], row["error"]) == ("skipped", "no enabled sources matched this request")


async def test_a_sync_for_a_disabled_source_is_skipped(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    await pool.execute("update sources set enabled = false where id = $1", a.id)
    rid = await request(pool, a.slug)

    await tasks.drain_crawl_requests(make_ctx(pool, World(blog(a))))

    assert (await request_row(pool, rid))["st"] == "skipped"


async def test_a_sync_during_a_running_cycle_attaches_instead_of_duplicating(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a), blog(b), latency=0.4)
    scheduled = asyncio.create_task(tasks.crawl_due(make_ctx(pool, world)))
    await asyncio.sleep(0.05)  # the scheduled cycle is under way
    rid = await request(pool, b.slug)

    await tasks.drain_crawl_requests(make_ctx(pool, world))
    await scheduled

    assert await pool.fetchval("select count(*) from crawl_cycles") == 1
    row = await request_row(pool, rid)
    assert row["st"] == "succeeded" and row["sources_crawled"] >= 1
    assert world.count(f"http://{b.slug}.onion/list") == 1  # not fetched twice


async def test_two_requests_in_one_tick_share_one_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    ctx = make_ctx(pool, World(blog(a)))
    first, second = await request(pool), await request(pool)

    result = await tasks.drain_crawl_requests(ctx)

    assert result["handled"] == 2
    assert {(await request_row(pool, r))["st"] for r in (first, second)} == {"succeeded"}
    # The second found the first's cycle finished and ran its own forced one; never concurrently.
    assert await pool.fetchval("select count(*) from crawl_cycles where status = 'running'") == 0


async def test_a_request_waits_its_turn_if_the_legacy_crawler_holds_the_lock(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a))
    ctx = make_ctx(pool, world)
    rid = await request(pool)

    async with Storage(pool).crawl_lock() as held:  # `intel run` is mid-crawl
        assert held
        result = await tasks.drain_crawl_requests(ctx)

    row = await request_row(pool, rid)
    assert result["skipped"] == "another crawl is already running"
    assert row["st"] == "queued" and row["started_at"] is None  # untouched, not failed
    assert world.calls == []

    await tasks.drain_crawl_requests(ctx)  # the lock is free: it is picked up
    assert (await request_row(pool, rid))["st"] == "succeeded"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            RunOutcome(1, "completed", "manual", sources_attempted=0),
            ("skipped", "no enabled sources matched this request"),
        ),
        (RunOutcome(1, "completed", "manual", sources_attempted=3), ("succeeded", None)),
        (
            RunOutcome(1, "completed", "manual", sources_attempted=3, failed_sources=["a"]),
            ("succeeded", None),
        ),
        (
            RunOutcome(1, "failed", "manual", sources_attempted=2, failed_sources=["a", "b"]),
            ("failed", "every source failed: a, b"),
        ),
        (
            RunOutcome(1, "abandoned", "manual", sources_attempted=2),
            ("failed", "the crawl cycle ended as abandoned"),
        ),
        (
            RunOutcome(1, "running", "manual", sources_attempted=2),
            ("failed", "the crawl cycle ended as running"),
        ),
    ],
)
def test_request_status_mapping(outcome: RunOutcome, expected: tuple) -> None:  # type: ignore[type-arg]
    assert tasks._request_result(outcome) == expected


# ---------------------------------------------------------------- the data the UI reads


async def test_the_sources_page_data_contract_is_unchanged(pool, queue) -> None:  # type: ignore[no-untyped-def]
    ok_src, bad_src = await new_source(pool), await new_source(pool)
    from test_crawl_fetch import fail

    dead = blog(bad_src)
    dead.content[dead.listing()] = fail("transient", 503, "HTTP 503")
    await tasks.crawl_due(make_ctx(pool, World(blog(ok_src, "/v/1"), dead)))

    # Exactly the columns GET /api/sources selects, and the health rule it derives.
    rows = {
        r["slug"]: r
        for r in await pool.fetch(
            "select id, slug, name, base_url, collector::text, enabled, crawl_interval_seconds, "
            "last_crawl_at, last_success_at, consecutive_failures from sources "
            "where slug = any($1)",
            [ok_src.slug, bad_src.slug],
        )
    }

    def health(row) -> str:  # type: ignore[no-untyped-def]
        n = row["consecutive_failures"]
        return "healthy" if n == 0 else "degraded" if n < 3 else "failing"

    assert health(rows[ok_src.slug]) == "healthy" and rows[ok_src.slug]["last_success_at"]
    assert (
        health(rows[bad_src.slug]) == "degraded" and rows[bad_src.slug]["last_success_at"] is None
    )
    assert all(r["last_crawl_at"] is not None for r in rows.values())

    # What GET /api/crawl/status reads: nothing may be left "running" once a cycle is done.
    assert (
        await pool.fetchval(
            "select count(*) from crawl_runs where status = 'running' "
            "and started_at > now() - interval '1 hour'"
        )
        == 0
    )
    # crawl_runs keeps using only the statuses the schema already had.
    assert {r["s"] for r in await pool.fetch("select distinct status::text s from crawl_runs")} <= {
        "running",
        "succeeded",
        "failed",
        "partial",
    }


async def test_a_running_cycle_reads_as_collection_in_progress(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a), latency=0.4)
    task = asyncio.create_task(tasks.crawl_due(make_ctx(pool, world)))

    # The Sync button's "running" flag is: any crawl_runs row at 'running' recently. Wait for the
    # cycle to open its run rather than guessing how long seeding takes.
    running = 0
    for _ in range(100):
        running = await pool.fetchval(
            "select count(*) from crawl_runs where status = 'running' "
            "and started_at > now() - interval '1 hour'"
        )
        if running:
            break
        await asyncio.sleep(0.05)
    assert running == 1
    await task
    assert await pool.fetchval("select count(*) from crawl_runs where status = 'running'") == 0


# ---------------------------------------------------------------- cron wiring


def test_the_sweep_interval_is_configurable() -> None:
    assert tasks.sweep_minutes(5) == {2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57}
    assert tasks.sweep_minutes(15) == {2, 17, 32, 47}
    assert len(tasks.sweep_minutes(1)) == 60
    assert cfg().sweep_interval_minutes == 5


@pytest.mark.parametrize("bad", [0, 61, -1])
def test_a_nonsense_sweep_interval_is_refused(bad: int) -> None:
    with pytest.raises(ValueError):
        cfg(CRAWL_SWEEP_INTERVAL_MINUTES=bad)


def test_the_crawl_crons_are_the_ones_that_drive_cycles() -> None:
    by_name = {job.name: job for job in tasks.WorkerSettings.cron_jobs}
    assert by_name["cron:crawl_due"].minute == tasks.sweep_minutes(
        tasks._settings.sweep_interval_minutes
    )
    assert by_name["cron:drain_crawl_requests"].second == {0, 10, 20, 30, 40, 50}
    # Both keep the long job timeout: a cycle that outlives it is resumed by the next tick.
    assert by_name["cron:crawl_due"].timeout_s == tasks._settings.job_timeout_seconds


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("CRAWL_WORKERS", 0),
        ("CRAWL_PER_SOURCE_INFLIGHT", 0),
        ("CRAWL_MAX_ATTEMPTS", 0),
        ("CRAWL_BROWSER_WORKERS", -1),
    ],
)
def test_concurrency_settings_refuse_values_that_would_wedge_a_cycle(name: str, bad: int) -> None:
    with pytest.raises(ValueError):
        cfg(**{name: bad})


def test_the_documented_defaults() -> None:
    s = make_settings()  # no overrides: the defaults as shipped
    assert (s.workers, s.browser_workers, s.per_source_inflight) == (6, 1, 3)
    assert s.request_timeout_seconds == 60 and s.max_bytes == 5 * 1024 * 1024
    assert (s.max_attempts, s.retry_backoff_seconds, s.retry_backoff_cap_seconds) == (3, 15, 120)
    assert s.lease_seconds == 120 and s.page1_first is True and s.engine == "queue"
    assert (s.follow_links, s.link_depth, s.links_per_page, s.link_max_pages) == (True, 3, 5, 25)
    assert s.link_recrawl_seconds == 7 * 24 * 3600 and s.link_recrawl_max_pages == 25
    assert s.job_timeout_seconds == 3600


def test_the_cron_follows_the_configured_sweep_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set CRAWL_SWEEP_INTERVAL_MINUTES and the worker's cron really fires on that rhythm."""
    import importlib

    from intel.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    monkeypatch.setenv("CRAWL_SWEEP_INTERVAL_MINUTES", "15")
    get_settings.cache_clear()
    try:
        reloaded = importlib.reload(tasks)
        job = {j.name: j for j in reloaded.WorkerSettings.cron_jobs}["cron:crawl_due"]
        assert job.minute == {2, 17, 32, 47}
    finally:
        monkeypatch.delenv("CRAWL_SWEEP_INTERVAL_MINUTES")
        get_settings.cache_clear()
        importlib.reload(tasks)  # back to the defaults for every other test
