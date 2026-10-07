"""The crawl queue and its workers, against a real Postgres.

Claiming, leases and the concurrency caps are SQL behaviour: a fake would only test the fake.
So these run against a scratch database named by `CRAWL_TEST_DATABASE_URL`, and are skipped
when it is unset or unreachable.

That variable is deliberately NOT the repo's DATABASE_URL, which in this project points at a
hosted database. Create a scratch one next to the dev stack and migrate it:

    docker exec leakmon-postgres psql -U leak -d postgres -c "create database leakmon_test"
    DATABASE_URL=postgresql://leak:leak_dev_password@localhost:5433/leakmon_test \\
        npm run migrate -w @leak/db
    CRAWL_TEST_DATABASE_URL=postgresql://leak:leak_dev_password@localhost:5433/leakmon_test \\
        pytest tests/test_crawl_queue.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from test_pipeline_concurrency import make_settings

from intel.crawl.frontier import normalize_crawl_url
from intel.crawl.queue import ClaimedUrl, CrawlQueue, NewUrl
from intel.crawl.worker import CrawlWorkers, Outcome, recrawl_after, retry_delay

TEST_DSN = os.environ.get("CRAWL_TEST_DATABASE_URL")


@pytest.fixture
async def pool():  # type: ignore[no-untyped-def]
    if not TEST_DSN:
        pytest.skip("CRAWL_TEST_DATABASE_URL not set")
    try:
        created = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=40, timeout=5)
    except (OSError, asyncpg.PostgresError, TimeoutError):
        pytest.skip("scratch Postgres not reachable")
    assert created is not None
    # A scratch database: nothing here is anyone's data. Clear what a failed run left over.
    await created.execute("update crawl_cycles set status = 'completed' where status = 'running'")
    try:
        yield created
    finally:
        await created.execute("delete from sources where slug like 'cq-%'")
        await created.execute("delete from crawl_cycles")
        await created.close()


@pytest.fixture
def queue(pool: asyncpg.Pool) -> CrawlQueue:
    return CrawlQueue(pool)


async def make_source(pool: asyncpg.Pool, *, collector: str = "http") -> int:
    slug = f"cq-{uuid.uuid4().hex[:8]}"
    return await pool.fetchval(
        """
        insert into sources (slug, name, base_url, collector, crawl_interval_seconds,
                             deep_crawl_interval_seconds)
        values ($1, $1, $2, $3::collector_kind, 900, 21600) returning id
        """,
        slug,
        f"http://{slug}.onion",
        collector,
    )


def pages(source_id: int, n: int, *, start: int = 1) -> list[NewUrl]:
    base = f"http://s{source_id}.onion/list"
    return [
        NewUrl(
            url=f"{base}?page={no}",
            url_normalized=normalize_crawl_url(f"{base}?page={no}"),
            kind="listing",
            page_no=no,
        )
        for no in range(start, start + n)
    ]


async def claim_all(queue: CrawlQueue, n: int, **kw) -> list[ClaimedUrl | None]:  # type: ignore[no-untyped-def]
    defaults = dict(
        browser=False, lane_limit=100, per_source_limit=100, lease_seconds=60.0, cycle_id=None
    )
    defaults.update(kw)
    return await asyncio.gather(*(queue.claim(f"w{i}", **defaults) for i in range(n)))  # type: ignore[arg-type]


# ---------------------------------------------------------------- enqueue


async def test_duplicate_urls_collapse_to_one_job(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    base = f"http://s{src}.onion/victim/1"
    batch = [
        NewUrl(url=u, url_normalized=normalize_crawl_url(u), kind="link", depth=1)
        for u in (base, base + "/", base + "?utm_source=x", base + "#frag")
    ]
    result = await queue.enqueue(cycle, src, batch)
    again = await queue.enqueue(cycle, src, batch)

    assert result.inserted == 1
    assert again.inserted == 0
    assert await pool.fetchval("select count(*) from crawl_urls where source_id = $1", src) == 1


async def test_a_finished_url_is_requeued_only_once_it_is_due(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    (claimed,) = await claim_all(queue, 1)
    assert claimed is not None
    await queue.complete(
        claimed.id, "w0", result="new", http_status=200, response_ms=5, recrawl_after_seconds=3600
    )

    # Not due for an hour: a new cycle's seeding must leave it alone.
    assert (await queue.enqueue(cycle, src, pages(src, 1))).requeued == 0
    assert await pool.fetchval("select status::text from crawl_urls") == "succeeded"

    await pool.execute("update crawl_urls set next_crawl_at = now() - interval '1 second'")
    assert (await queue.enqueue(cycle, src, pages(src, 1))).requeued == 1
    assert await pool.fetchval("select status::text from crawl_urls") == "queued"
    assert await pool.fetchval("select attempt from crawl_urls") == 0


# ---------------------------------------------------------------- claiming and limits


async def test_claims_go_page_one_first_across_sources(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await make_source(pool), await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, a, pages(a, 4))
    await queue.enqueue(cycle, b, pages(b, 4))

    first_two = [
        await queue.claim(
            f"w{i}",
            browser=False,
            lane_limit=100,
            per_source_limit=100,
            lease_seconds=60,
            cycle_id=cycle,
        )
        for i in range(2)
    ]
    assert sorted(c.page_no for c in first_two if c) == [1, 1]


async def test_concurrent_claims_never_exceed_the_lane_limit(pool, queue) -> None:  # type: ignore[no-untyped-def]
    sources = [await make_source(pool) for _ in range(5)]
    cycle = await queue.start_cycle("cli")
    for s in sources:
        await queue.enqueue(cycle, s, pages(s, 6))

    got = await claim_all(queue, 25, lane_limit=6, per_source_limit=100, cycle_id=cycle)

    assert sum(c is not None for c in got) == 6
    assert await pool.fetchval("select count(*) from crawl_urls where status = 'running'") == 6


async def test_the_per_source_cap_holds(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await make_source(pool), await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, a, pages(a, 10))
    await queue.enqueue(cycle, b, pages(b, 10))

    got = await claim_all(queue, 20, lane_limit=100, per_source_limit=3, cycle_id=cycle)

    by_source: dict[int, int] = {}
    for c in got:
        if c:
            by_source[c.source_id] = by_source.get(c.source_id, 0) + 1
    assert by_source == {a: 3, b: 3}


async def test_one_source_cannot_take_every_slot(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """The reason the cap exists: a deep listing must leave room for the other sources."""
    big, small = await make_source(pool), await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, big, pages(big, 20))
    await queue.enqueue(cycle, small, pages(small, 2))

    got = await claim_all(queue, 6, lane_limit=6, per_source_limit=3, cycle_id=cycle)

    assert {c.source_id for c in got if c} == {big, small}


async def test_browser_and_http_lanes_are_separate(pool, queue) -> None:  # type: ignore[no-untyped-def]
    web, browser = await make_source(pool), await make_source(pool, collector="browser")
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, web, pages(web, 5))
    await queue.enqueue(cycle, browser, pages(browser, 5))

    http = await claim_all(queue, 10, browser=False, lane_limit=6, cycle_id=cycle)
    heavy = await claim_all(queue, 10, browser=True, lane_limit=1, cycle_id=cycle)

    assert {c.source_id for c in http if c} == {web}
    assert [c.source_id for c in heavy if c] == [browser]


# ---------------------------------------------------------------- retries free the slot


async def test_a_transient_failure_frees_the_slot_instead_of_waiting(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 2))
    kw = dict(browser=False, lane_limit=1, per_source_limit=100, lease_seconds=60, cycle_id=cycle)

    first = await queue.claim("w", **kw)
    assert first is not None and first.page_no == 1
    assert await queue.claim("w2", **kw) is None  # the single slot is taken

    status = await queue.fail(
        first.id,
        "w",
        error="timeout",
        http_status=None,
        response_ms=None,
        transient=True,
        max_attempts=3,
        retry_delay_seconds=300,
        recrawl_after_seconds=900,
    )
    assert status == "retry"

    # The slot is free at once, and what it gets is page 2 — not the URL backing off.
    second = await queue.claim("w", **kw)
    assert second is not None and second.page_no == 2
    row = await pool.fetchrow(
        "select status::text, next_crawl_at > now() as later from crawl_urls where id = $1",
        first.id,
    )
    assert (row["status"], row["later"]) == ("retry", True)


async def test_a_permanent_failure_is_not_retried(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    (claimed,) = await claim_all(queue, 1, cycle_id=cycle)
    assert claimed is not None

    status = await queue.fail(
        claimed.id,
        "w0",
        error="HTTP 404",
        http_status=404,
        response_ms=10,
        transient=False,
        max_attempts=3,
        retry_delay_seconds=0,
        recrawl_after_seconds=900,
    )
    assert status == "failed"
    assert await queue.active_count(cycle) == 0


async def test_attempts_run_out(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))

    statuses = []
    for _ in range(3):
        claimed = await queue.claim(
            "w", browser=False, lane_limit=1, per_source_limit=1, lease_seconds=60, cycle_id=cycle
        )
        assert claimed is not None
        statuses.append(
            await queue.fail(
                claimed.id,
                "w",
                error="HTTP 503",
                http_status=503,
                response_ms=5,
                transient=True,
                max_attempts=3,
                retry_delay_seconds=0,
                recrawl_after_seconds=900,
            )
        )
        await pool.execute("update crawl_urls set next_crawl_at = now()")

    assert statuses == ["retry", "retry", "failed"]
    assert await pool.fetchval("select failure_count from crawl_urls") == 3


# ---------------------------------------------------------------- leases


async def test_an_expired_lease_is_recovered_and_reclaimed(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    kw = dict(browser=False, lane_limit=1, per_source_limit=1, lease_seconds=60, cycle_id=cycle)

    dead = await queue.claim("dead-worker", **kw)
    assert dead is not None

    # While the lease is live, nothing else can have it.
    assert await queue.claim("other", **kw) is None

    # The worker dies: its lease runs out.
    await pool.execute("update crawl_urls set leased_until = now() - interval '1 second'")
    retried, failed = await queue.reap_expired(max_attempts=3, backoff_base=0, backoff_cap=0)
    assert (retried, failed) == (1, 0)

    taken = await queue.claim("other", **kw)
    assert taken is not None and taken.id == dead.id and taken.attempt == 2

    # The dead worker turning up late must not overwrite the new holder's work.
    assert not await queue.complete(
        dead.id,
        "dead-worker",
        result="new",
        http_status=200,
        response_ms=1,
        recrawl_after_seconds=60,
    )
    assert await pool.fetchval("select leased_by from crawl_urls") == "other"


async def test_a_url_that_keeps_killing_its_worker_is_eventually_failed(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    kw = dict(browser=False, lane_limit=1, per_source_limit=1, lease_seconds=60, cycle_id=cycle)

    outcomes = []
    for _ in range(3):
        assert await queue.claim("w", **kw) is not None
        await pool.execute("update crawl_urls set leased_until = now() - interval '1 second'")
        outcomes.append(await queue.reap_expired(max_attempts=3, backoff_base=0, backoff_cap=0))
        await pool.execute("update crawl_urls set next_crawl_at = now()")

    assert outcomes == [(1, 0), (1, 0), (0, 1)]
    assert await pool.fetchval("select status::text from crawl_urls") == "failed"


async def test_an_expired_lease_does_not_count_against_the_limit(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 2))
    kw = dict(browser=False, lane_limit=1, per_source_limit=1, lease_seconds=60, cycle_id=cycle)

    assert await queue.claim("a", **kw) is not None
    await pool.execute("update crawl_urls set leased_until = now() - interval '1 second'")
    # A dead worker's slot must not stay occupied while waiting for the reaper.
    assert await queue.claim("b", **kw) is not None


# ---------------------------------------------------------------- cycles and hashes


async def test_only_one_cycle_runs_at_a_time(pool, queue) -> None:  # type: ignore[no-untyped-def]
    first = await queue.start_cycle("schedule")
    assert first is not None
    assert await queue.start_cycle("schedule") is None
    assert await queue.start_cycle("manual") is None
    assert await queue.running_cycle() == first

    # Two starters racing: exactly one wins.
    await pool.execute("update crawl_cycles set status = 'completed'")
    racers = await asyncio.gather(*(queue.start_cycle("schedule") for _ in range(8)))
    assert sum(r is not None for r in racers) == 1


async def test_a_stored_hash_survives_a_completion_that_carries_none(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """The hash is written only by a caller whose ingest succeeded."""
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    kw = dict(browser=False, lane_limit=1, per_source_limit=1, lease_seconds=60, cycle_id=cycle)

    first = await queue.claim("w", **kw)
    await queue.complete(
        first.id,
        "w",
        result="new",
        http_status=200,
        response_ms=1,
        recrawl_after_seconds=0,
        content_sha256="a" * 64,
    )
    await queue.enqueue(cycle, src, pages(src, 1))  # due immediately

    second = await queue.claim("w", **kw)
    assert second.content_sha256 == "a" * 64
    await queue.complete(
        second.id,
        "w",
        result="empty",
        http_status=200,
        response_ms=1,
        recrawl_after_seconds=0,
        content_sha256=None,
    )
    assert await pool.fetchval("select content_sha256 from crawl_urls") == "a" * 64


# ---------------------------------------------------------------- the worker loop


def settings(**overrides: object):  # type: ignore[no-untyped-def]
    base = {
        "CRAWL_WORKERS": 6,
        "CRAWL_PER_SOURCE_INFLIGHT": 3,
        "CRAWL_RETRY_BACKOFF": 0,
        "CRAWL_MAX_ATTEMPTS": 3,
        # These tests are about limits, leases and retries with every page claimable at once;
        # the page-1-first gate has its own tests in test_crawl_discovery.py.
        "CRAWL_PAGE1_FIRST": False,
    }
    base.update(overrides)
    return make_settings(**base)


async def test_workers_process_a_cycle_within_the_concurrency_limit(pool, queue) -> None:  # type: ignore[no-untyped-def]
    sources = [await make_source(pool) for _ in range(4)]
    cycle = await queue.start_cycle("cli")
    for s in sources:
        await queue.enqueue(cycle, s, pages(s, 6))

    live = peak = 0

    async def handler(url: ClaimedUrl) -> Outcome:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return Outcome("ok", result="new", http_status=200, response_ms=20)

    stats = await CrawlWorkers(queue, handler, settings(), poll_seconds=0.05).run(cycle)

    assert stats.ok == 24
    assert 1 < peak <= 6
    assert await queue.active_count(cycle) == 0


async def test_two_processes_share_one_budget(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """CRAWL_WORKERS=6 across two worker processes is 6 in flight, not 12."""
    sources = [await make_source(pool) for _ in range(4)]
    cycle = await queue.start_cycle("cli")
    for s in sources:
        await queue.enqueue(cycle, s, pages(s, 8))

    live = peak = 0

    async def handler(url: ClaimedUrl) -> Outcome:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.03)
        live -= 1
        return Outcome("ok", result="new")

    one = CrawlWorkers(queue, handler, settings(), poll_seconds=0.05, process_id="proc-1")
    two = CrawlWorkers(queue, handler, settings(), poll_seconds=0.05, process_id="proc-2")
    a, b = await asyncio.gather(one.run(cycle), two.run(cycle))

    assert a.ok + b.ok == 32
    assert peak <= 6
    assert a.ok > 0 and b.ok > 0  # both really took part


async def test_a_retry_does_not_hold_a_worker(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """One worker, one URL that backs off for a long time: everything else still finishes."""
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 4))

    seen_first = False
    finished_while_waiting: list[int] = []

    async def handler(url: ClaimedUrl) -> Outcome:
        nonlocal seen_first
        if url.page_no == 1 and not seen_first:
            seen_first = True
            return Outcome("transient", error="timeout")
        if url.page_no != 1:
            finished_while_waiting.append(url.page_no or 0)
        return Outcome("ok", result="new")

    # A 1s backoff against ~instant handlers: if the worker slept through it, pages 2-4 could
    # not complete before page 1's retry came due.
    stats = await CrawlWorkers(
        queue, handler, settings(CRAWL_WORKERS=1, CRAWL_RETRY_BACKOFF=1), poll_seconds=0.05
    ).run(cycle)

    assert stats.retried == 1 and stats.ok == 4
    assert sorted(finished_while_waiting) == [2, 3, 4]
    order = [
        r["page_no"]
        for r in await pool.fetch("select page_no from crawl_urls order by last_crawled_at")
    ]
    assert order == [2, 3, 4, 1]


async def test_a_handler_exception_becomes_a_retry_not_a_dead_worker(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 2))
    calls: dict[int, int] = {}

    async def handler(url: ClaimedUrl) -> Outcome:
        calls[url.id] = calls.get(url.id, 0) + 1
        if url.page_no == 1 and calls[url.id] == 1:
            raise RuntimeError("boom")
        return Outcome("ok", result="new")

    stats = await CrawlWorkers(queue, handler, settings(), poll_seconds=0.05).run(cycle)
    assert (stats.ok, stats.retried, stats.failed) == (2, 1, 0)


async def test_a_hung_handler_is_cut_off_at_the_lease(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    first = True

    async def handler(url: ClaimedUrl) -> Outcome:
        nonlocal first
        if first:
            first = False
            await asyncio.sleep(30)
        return Outcome("ok", result="new")

    stats = await CrawlWorkers(
        queue, handler, settings(CRAWL_LEASE_SECONDS=1), poll_seconds=0.05
    ).run(cycle)
    assert (stats.retried, stats.ok) == (1, 1)


async def test_one_failing_url_does_not_stop_the_rest(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 5))

    async def handler(url: ClaimedUrl) -> Outcome:
        if url.page_no == 3:
            # A 403, not a 404: a 404 on a listing page means the listing ended there, which
            # prunes later pages (test_crawl_discovery.py). This test is about isolation.
            return Outcome("permanent", http_status=403, error="HTTP 403")
        return Outcome("ok", result="new", http_status=200)

    stats = await CrawlWorkers(queue, handler, settings(), poll_seconds=0.05).run(cycle)
    assert (stats.ok, stats.failed) == (4, 1)
    assert await queue.stats(cycle) == {"succeeded": 4, "failed": 1}


# ---------------------------------------------------------------- pure helpers


def test_retry_delay_doubles_to_a_cap_with_jitter() -> None:
    import random

    rng = random.Random(1)
    delays = [retry_delay(n, base=15, cap=120, rng=rng) for n in (1, 2, 3, 4, 5)]
    for delay, nominal in zip(delays, (15, 30, 60, 120, 120), strict=True):
        assert nominal * 0.8 <= delay <= nominal * 1.2


def test_recrawl_cadence_follows_the_kind_of_page() -> None:
    s = make_settings(CRAWL_LINK_INTERVAL=1234)

    def url(kind: str, page_no: int | None) -> ClaimedUrl:
        return ClaimedUrl(1, 1, "x", "http", 1, "u", kind, page_no, 0, 1, None, None, 0, 900, 21600)

    assert recrawl_after(url("listing", 1), s) == 900
    assert recrawl_after(url("listing", 4), s) == 21600
    assert recrawl_after(url("link", None), s) == 1234
