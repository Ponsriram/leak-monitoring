# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""Phase 4: whole cycles. Scheduling, joining, due recrawls, finalization, source runs, status.

Everything runs the real manager, queue, workers and fetch handler against the scratch
Postgres; only the network (a scripted `World` standing in for Tor) and extraction storage (a
recording double) are fakes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from test_crawl_discovery import BASE_PATH, EMPTY, Site, new_source, page_html
from test_crawl_fetch import FakeIngestStorage, fail
from test_crawl_queue import pages, pool, queue  # noqa: F401 - fixtures
from test_pipeline_concurrency import make_settings

from intel.collectors import FetchResult
from intel.crawl.manager import CycleManager
from intel.crawl.queue import NewUrl
from intel.crawl.report import CycleSummary, build_summary, judge_source
from intel.crawl.seeding import seed_source
from intel.storage import SourceRow, Storage


def cfg(**overrides: object):  # type: ignore[no-untyped-def]
    base = {
        "CRAWL_WORKERS": 4,
        "CRAWL_PER_SOURCE_INFLIGHT": 3,
        "CRAWL_RETRY_BACKOFF": 0,
        "CRAWL_MAX_ATTEMPTS": 3,
        "EXPOSURE_DETECTION": False,
    }
    base.update(overrides)
    return make_settings(**base)


class World:
    """Several scripted sites behind one collector: what the manager sees of Tor."""

    name = "http"

    def __init__(self, *sites: Site, latency: float = 0.0) -> None:
        self.sites = list(sites)
        self.latency = latency
        self.in_flight = 0
        self.peak = 0
        self.calls: list[str] = []

    def add(self, site: Site) -> None:
        self.sites.append(site)

    async def fetch_detailed(self, url: str) -> FetchResult:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls.append(url)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            for site in self.sites:
                if f"{site.source.slug}.onion" in url:
                    return await site.fetch_detailed(url)
            return fail("permanent", 404, "HTTP 404")
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        return None

    def count(self, url: str) -> int:
        return self.calls.count(url)


def blog(source: SourceRow, *links: str, extra: dict | None = None) -> Site:  # type: ignore[type-arg]
    """A source with one listing page that links to `links`, each a plain page."""
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", *links)}
    for link in links:
        site.content[site.url(link)] = page_html(link)
    site.content.update(extra or {})
    return site


def manager(pool, world, *, ingest=None, serving=None, **settings):  # type: ignore[no-untyped-def]
    ingest = ingest or FakeIngestStorage()
    mgr = CycleManager(
        Storage(pool),
        cfg(**settings),
        ingest_storage=ingest,
        collectors={"http": world},
        poll_seconds=0.05,
        serving=set() if serving is None else serving,
    )
    return mgr, ingest


async def cycles(pool):  # type: ignore[no-untyped-def]
    return await pool.fetch("select *, status::text as st from crawl_cycles order by id")


async def runs(pool, source: SourceRow):  # type: ignore[no-untyped-def]
    return await pool.fetch(
        "select *, status::text as st from crawl_runs where source_id = $1 order by id",
        source.id,
    )


async def source_row(pool, source: SourceRow):  # type: ignore[no-untyped-def]
    return await pool.fetchrow("select * from sources where id = $1", source.id)


# =============================================================================== pure verdicts


def r(**kw):  # type: ignore[no-untyped-def]
    base = {
        "source_id": 1,
        "slug": "a",
        "kind": "listing",
        "page_no": 1,
        "status": "succeeded",
        "result": "new",
        "http_status": 200,
        "last_error": None,
        "attempt": 1,
        "leaks_found": 0,
        "leaks_updated": 0,
        "discovered_at": datetime.now(UTC),
    }
    base.update(kw)
    return base


def test_a_source_fails_exactly_when_page_one_fails() -> None:
    ok_rows = [
        r(),
        r(kind="link", page_no=None, status="failed", http_status=404, last_error="HTTP 404"),
    ]
    assert (
        judge_source(1, "a", ok_rows).status == "succeeded"
    )  # a dead detail page is not a dead site

    down = [
        r(status="failed", http_status=503, last_error="HTTP 503"),
        r(page_no=2, status="skipped"),
    ]
    verdict = judge_source(1, "a", down)
    assert (verdict.status, verdict.error, verdict.update_health) == ("failed", "HTTP 503", True)


def test_a_run_that_never_reached_page_one_does_not_touch_health() -> None:
    link_only = [r(kind="link", page_no=None)]
    verdict = judge_source(1, "a", link_only)
    assert (verdict.status, verdict.update_health) == ("succeeded", False)

    disabled = [r(status="skipped", result="source_disabled")]
    verdict = judge_source(1, "a", disabled)
    assert (verdict.status, verdict.update_health) == ("failed", False)


def sources(*verdicts) -> CycleSummary:  # type: ignore[no-untyped-def]
    return CycleSummary(sources=list(verdicts), total=5, succeeded=3, failed=2)


def test_the_overall_status_matrix() -> None:
    good, bad = judge_source(1, "a", [r()]), judge_source(2, "b", [r(source_id=2, status="failed")])

    # everything fine
    assert build_summary([r()], [good], cycle_started_at=datetime.now(UTC)).status == "completed"
    # a source down while others succeed: completed, and the failure is named
    mixed = CycleSummary(sources=[good, bad], total=2, succeeded=1, failed=1)
    assert mixed.status == "completed" and mixed.error == "1 of 2 sources failed: b"
    # every source failed
    assert CycleSummary(sources=[bad], total=1, failed=1).status == "failed"
    # some URLs failed, but sources are fine
    assert CycleSummary(sources=[good], total=5, succeeded=4, failed=1).status == "completed"
    # pagination ended: skipped pages are not failures
    assert CycleSummary(sources=[good], total=5, succeeded=3, skipped=2).status == "completed"
    # nothing eligible at all
    assert CycleSummary().status == "completed" and CycleSummary().error is None
    # URLs attempted and none succeeded
    assert CycleSummary(total=3, failed=3).status == "failed"


def test_the_report_counts_and_never_carries_content() -> None:
    rows = [
        r(result="new", leaks_found=2),
        r(page_no=2, result="changed", leaks_updated=3, attempt=3),
        r(page_no=3, result="unchanged"),
        r(page_no=4, result="empty"),
        r(kind="link", page_no=None, status="failed", http_status=404, last_error="HTTP 404"),
        r(
            kind="link",
            page_no=None,
            status="failed",
            http_status=None,
            last_error="timed out after 60s",
        ),
        r(page_no=5, status="skipped", result="past_end"),
    ]
    now = datetime.now(UTC) + timedelta(seconds=5)
    summary = build_summary(rows, [judge_source(1, "a", rows)], cycle_started_at=now)

    assert (summary.new, summary.changed, summary.unchanged, summary.empty) == (1, 1, 1, 1)
    assert (summary.failed, summary.skipped, summary.retried_attempts) == (2, 1, 2)
    assert (summary.leaks_found, summary.leaks_updated) == (2, 3)
    assert summary.links_recrawled == 2 and summary.links_discovered == 0
    assert summary.http == {"200": 4, "404": 1, "none": 1}
    assert dict(summary.errors) == {"HTTP 404": 1, "timed out after 60s": 1}
    parsed = json.loads(summary.to_json())
    assert parsed["urls"]["total"] == 7 and parsed["per_source"]["a"]["status"] == "succeeded"


# =============================================================================== scheduled cycles


async def test_a_scheduled_cycle_runs_start_to_finish(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a, "/v/1"), blog(b, "/v/2", "/v/3"))
    mgr, ingest = manager(pool, world)

    out = await mgr.run("schedule")

    assert out is not None and out.status == "completed"
    assert (out.sources_attempted, out.failed_sources, out.urls) == (2, [], 2 + 3)
    (cycle,) = await cycles(pool)
    assert cycle["st"] == "completed" and cycle["trigger"] == "schedule"
    assert (cycle["total_urls"], cycle["successful_urls"], cycle["failed_urls"]) == (5, 5, 0)
    assert cycle["new_pages"] == 5 and cycle["leaks_found"] == 4  # listings extract, details don't
    assert cycle["completed_at"] is not None and cycle["duration_ms"] >= 0
    summary = json.loads(cycle["summary"])
    assert summary["sources"] == {"attempted": 2, "failed": []}
    assert summary["links"]["discovered"] == 3 and summary["http"] == {"200": 5}

    # The Sources page's data: every run closed, health stamped, nothing left "running".
    for src in (a, b):
        (run,) = await runs(pool, src)
        assert run["st"] == "succeeded" and run["finished_at"] is not None
        assert run["cycle_id"] == cycle["id"]
        row = await source_row(pool, src)
        assert row["consecutive_failures"] == 0 and row["last_success_at"] is not None
    assert await pool.fetchval("select count(*) from crawl_runs where status = 'running'") == 0
    assert len(ingest.saves) == 5


async def test_nothing_due_leaves_no_trace(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a, "/v/1"))
    mgr, _ = manager(pool, world)
    await mgr.run("schedule")
    before = len(world.calls)

    again = await mgr.run("schedule")

    assert again is not None and again.discarded
    assert len(world.calls) == before  # nothing was fetched
    assert len(await cycles(pool)) == 1  # and no empty cycle was recorded


async def test_each_url_keeps_its_own_interval(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool, style="query", max_pages=3)
    site = Site(a, {})
    for p in (1, 2, 3):
        site.content[site.listing(p)] = page_html(site.listing(p))
    world = World(site)
    mgr, _ = manager(pool, world, CRAWL_FOLLOW_LINKS=False)
    await mgr.run("schedule")
    assert len(world.calls) == 3

    # Page 1's probe interval has elapsed; the deep pages' has not.
    await pool.execute(
        "update crawl_urls set next_crawl_at = now() - interval '1 s' where page_no = 1"
    )
    await mgr.run("schedule")

    assert world.count(site.listing(1)) == 2
    assert world.count(site.listing(2)) == 1 and world.count(site.listing(3)) == 1


# =============================================================================== manual Sync


async def test_a_manual_sync_recrawls_what_is_not_yet_due(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a), blog(b))
    mgr, _ = manager(pool, world, CRAWL_FOLLOW_LINKS=False)
    await mgr.run("schedule")

    out = await mgr.run("manual", slugs=[a.slug], force=True)

    assert out is not None and out.sources_attempted == 1 and out.status == "completed"
    assert world.count(f"http://{a.slug}.onion{BASE_PATH}") == 2  # forced, though not due
    assert world.count(f"http://{b.slug}.onion{BASE_PATH}") == 1  # not asked for
    last = (await cycles(pool))[-1]
    assert last["trigger"] == "manual"


async def test_a_sync_for_nothing_is_an_empty_completed_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    mgr, _ = manager(pool, World())
    out = await mgr.run("manual", slugs=["no-such-source"], force=True)

    assert out is not None and (out.sources_attempted, out.urls, out.status) == (0, 0, "completed")
    (cycle,) = await cycles(pool)
    assert cycle["st"] == "completed" and cycle["total_urls"] == 0


async def test_a_sync_during_a_cycle_joins_it_instead_of_starting_another(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a), blog(b), latency=0.4)
    scheduled, _ = manager(pool, world, serving=set(), CRAWL_FOLLOW_LINKS=False)
    sync, _ = manager(pool, world, serving=set(), CRAWL_FOLLOW_LINKS=False)

    first = asyncio.create_task(scheduled.run("schedule"))
    await asyncio.sleep(0.05)  # the scheduled cycle is running
    second = await sync.run("manual", slugs=[b.slug], force=True, wait=True)
    done = await first

    assert len(await cycles(pool)) == 1  # one cycle, not two
    assert second is not None and second.cycle_id == done.cycle_id
    assert second.status == "completed"
    # Each page was fetched once: the sync did not duplicate work already queued.
    assert world.count(f"http://{b.slug}.onion{BASE_PATH}") == 1


# ============================================================================ overlap and processes


async def test_only_one_cycle_exists_when_many_ticks_fire_at_once(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    world = World(blog(a, "/v/1"), blog(b, "/v/2"), latency=0.05)
    mgrs = [manager(pool, world, serving=set())[0] for _ in range(4)]  # four "processes"

    outs = await asyncio.gather(*(m.run("schedule") for m in mgrs))

    assert len(await cycles(pool)) == 1
    assert {o.cycle_id for o in outs if o is not None and not o.discarded} <= {
        (await cycles(pool))[0]["id"]
    }
    # Every URL fetched exactly once, however many processes were serving.
    assert all(world.count(u) == 1 for u in set(world.calls))


async def test_several_processes_share_one_concurrency_limit(pool, queue) -> None:  # type: ignore[no-untyped-def]
    sites = []
    for _ in range(4):
        src = await new_source(pool)
        sites.append(blog(src, *[f"/v/{n}" for n in range(5)]))
    world = World(*sites, latency=0.1)
    ingest = FakeIngestStorage()

    def process() -> CycleManager:
        return manager(
            pool,
            world,
            ingest=ingest,
            serving=set(),
            CRAWL_WORKERS=3,
            CRAWL_PER_SOURCE_INFLIGHT=3,
            CRAWL_LINKS_PER_PAGE=5,
        )[0]

    first = asyncio.create_task(process().run("schedule"))
    await asyncio.sleep(0.15)  # the cycle is under way; two more processes now join it
    await asyncio.gather(process().run("schedule"), process().run("schedule"))
    await first

    assert world.peak <= 3  # CRAWL_WORKERS=3 across three processes, not 9
    assert len(world.calls) == 4 * 6 and len(set(world.calls)) == 24  # each URL exactly once
    (cycle,) = await cycles(pool)
    assert cycle["st"] == "completed" and cycle["total_urls"] == 24


async def test_the_legacy_crawl_lock_keeps_a_cycle_from_starting(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a))
    mgr, _ = manager(pool, world)
    other = Storage(pool)

    async with other.crawl_lock() as held:  # `intel run` is mid-crawl
        assert held
        assert await mgr.run("schedule") is None

    assert world.calls == [] and await cycles(pool) == []


# =============================================================================== due recrawls


async def test_a_discovered_page_is_recrawled_when_due_without_its_parent_changing(
    pool, queue
) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a, "/v/1"))
    mgr, _ = manager(pool, world)
    await mgr.run("schedule")
    victim = f"http://{a.slug}.onion/v/1"
    listing = f"http://{a.slug}.onion{BASE_PATH}"
    assert (world.count(victim), world.count(listing)) == (1, 1)

    # The followed page's week is up. The listing's is not.
    await pool.execute(
        "update crawl_urls set next_crawl_at = now() - interval '1 s' where kind = 'link'"
    )
    out = await mgr.run("schedule")

    assert out is not None and not out.discarded
    assert world.count(victim) == 2  # recrawled without being rediscovered
    assert world.count(listing) == 1  # the listing was not due, so not fetched
    link = await pool.fetchrow("select * from crawl_urls where kind = 'link'")
    assert link["result"] == "unchanged" and link["next_crawl_at"] > datetime.now(UTC) + timedelta(
        days=6
    )
    summary = json.loads((await cycles(pool))[-1]["summary"])
    assert summary["links"]["recrawled"] == 1 and summary["links"]["discovered"] == 0


async def test_a_failed_followed_page_is_retried_when_it_comes_due(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    site = blog(a, "/v/1")
    site.content[site.url("/v/1")] = fail("permanent", 404, "HTTP 404")
    world = World(site)
    mgr, _ = manager(pool, world)
    await mgr.run("schedule")
    assert (
        await pool.fetchval("select status::text from crawl_urls where kind = 'link'") == "failed"
    )

    site.content[site.url("/v/1")] = page_html("back")  # it has reappeared
    await pool.execute(
        "update crawl_urls set next_crawl_at = now() - interval '1 s' where kind = 'link'"
    )
    await mgr.run("schedule")

    assert (
        await pool.fetchval("select status::text from crawl_urls where kind = 'link'")
        == "succeeded"
    )


async def test_due_links_are_not_requeued_if_already_scheduled(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, await Storage(pool).get_source(a.slug))
    parent = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )
    urls = [f"http://{a.slug}.onion/v/{n}" for n in range(3)]
    await queue.enqueue_links(
        parent,
        [NewUrl(url=u, url_normalized=u, kind="link") for u in urls],
        max_depth=3,
        per_page_limit=5,
        source_cycle_limit=25,
    )
    statuses = ["queued", "retry", "running"]
    for u, st in zip(urls, statuses, strict=True):
        await pool.execute(
            "update crawl_urls set status = $2::crawl_url_status, "
            "next_crawl_at = now() - interval '1 h' where url = $1",
            u,
            st,
        )

    kw = dict(
        base_url=a.base_url, active_url=None, max_depth=3, max_pages=25, recheck_after_seconds=60
    )
    assert await queue.requeue_due_links(cycle, a.id, **kw) == (0, 0)
    assert await pool.fetchval("select count(*) from crawl_urls where kind = 'link'") == 3


async def test_the_due_link_seeder_respects_the_allowlist_depth_and_cap(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    # The two bad ones are the oldest-due, so they are inspected first and cannot hide
    # behind the cap.
    bad = [
        ("http://moved-away.onion/x", 1, "2 hours"),
        (f"http://{a.slug}.onion/deep", 4, "2 hours"),
    ]
    good = [(f"http://{a.slug}.onion/ok{n}", 1, "1 hour") for n in range(4)]
    for url, depth, age in bad + good:
        await pool.execute(
            "insert into crawl_urls (source_id, url, url_normalized, kind, depth, status, "
            "next_crawl_at, discovered_at) values ($1, $2, $2, 'link', $3, 'succeeded', "
            f"now() - interval '{age}', now() - interval '3 days')",
            a.id,
            url,
            depth,
        )
    kw = dict(
        base_url=a.base_url, active_url=None, max_depth=3, max_pages=3, recheck_after_seconds=3600
    )

    # Cap of 3 per source per cycle: the first look takes bad, bad, ok0.
    assert await queue.requeue_due_links(cycle, a.id, **kw) == (1, 2)
    assert {
        r["url"]
        for r in await pool.fetch("select url from crawl_urls where result = 'out_of_scope'")
    } == {u for u, _, _ in bad}
    # Out-of-scope pages are parked, not retried every cycle; the next look takes ok1, ok2.
    assert await queue.requeue_due_links(cycle, a.id, **kw) == (2, 0)
    # The cap is spent: ok3 waits for a later cycle.
    assert await queue.requeue_due_links(cycle, a.id, **kw) == (0, 0)
    queued = await pool.fetch("select url from crawl_urls where status = 'queued' order by url")
    assert [r["url"].rsplit("/", 1)[1] for r in queued] == ["ok0", "ok1", "ok2"]


# =============================================================================== sources


async def test_a_disabled_source_is_not_crawled(pool, queue) -> None:  # type: ignore[no-untyped-def]
    on, off = await new_source(pool), await new_source(pool)
    await pool.execute("update sources set enabled = false where id = $1", off.id)
    world = World(blog(on), blog(off))
    mgr, _ = manager(pool, world)

    out = await mgr.run("schedule")

    assert out is not None and out.sources_attempted == 1
    assert not [u for u in world.calls if off.slug in u]
    assert await pool.fetchval("select count(*) from crawl_urls where source_id = $1", off.id) == 0
    assert await runs(pool, off) == []


async def test_a_source_disabled_mid_cycle_stops_without_wedging_the_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool, style="query", max_pages=4)
    storage = Storage(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, await storage.get_source(a.slug))
    await queue.ensure_run(cycle, a.id)
    await pool.execute("update sources set enabled = false where id = $1", a.id)

    assert (
        await queue.claim(
            "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
        )
        is None
    )  # nothing is handed out
    assert await queue.skip_disabled(cycle) == 4
    report = await queue.finalize_cycle(cycle)

    assert report is not None and report.status == "failed"  # nothing worked
    row = await source_row(pool, a)
    assert row["consecutive_failures"] == 0  # a disabled source's health is not touched
    (run,) = await runs(pool, a)
    assert run["st"] == "failed" and "disabled" in run["error"]


async def test_browser_sources_do_not_hang_a_cycle_that_has_no_browser_workers(pool, queue) -> None:  # type: ignore[no-untyped-def]
    web, heavy = await new_source(pool), await new_source(pool, collector="browser")
    world = World(blog(web), blog(heavy))
    mgr, _ = manager(pool, world, CRAWL_BROWSER_WORKERS=0)

    out = await asyncio.wait_for(mgr.run("schedule"), timeout=20)

    assert out is not None and out.sources_attempted == 1
    assert not [u for u in world.calls if heavy.slug in u]


# ============================================================================ failure and recovery


async def test_a_source_that_fails_does_not_stop_the_others(pool, queue) -> None:  # type: ignore[no-untyped-def]
    good, bad = await new_source(pool), await new_source(pool)
    dead = blog(bad)
    dead.content[dead.listing()] = fail("transient", 503, "HTTP 503")
    await pool.execute("update sources set consecutive_failures = 2 where id = $1", good.id)
    await pool.execute("update sources set consecutive_failures = 1 where id = $1", bad.id)
    world = World(blog(good, "/v/1"), dead)
    mgr, _ = manager(pool, world)

    out = await mgr.run("schedule")

    assert out is not None and out.status == "completed"  # partial failure is not a failed cycle
    assert out.failed_sources == [bad.slug] and out.sources_attempted == 2
    assert bad.slug in (out.error or "")
    (cycle,) = await cycles(pool)
    assert cycle["st"] == "completed" and bad.slug in cycle["error"]

    g, b = await source_row(pool, good), await source_row(pool, bad)
    assert g["consecutive_failures"] == 0 and g["last_success_at"] is not None  # reset by success
    assert b["consecutive_failures"] == 2 and b["last_success_at"] is None  # one more failure
    (good_run,), (bad_run,) = await runs(pool, good), await runs(pool, bad)
    assert (good_run["st"], bad_run["st"]) == ("succeeded", "failed")
    assert bad_run["error"] == "HTTP 503" and bad_run["finished_at"] is not None
    assert world.count(f"http://{bad.slug}.onion{BASE_PATH}") == 3  # three attempts, then given up


async def test_when_every_source_fails_the_cycle_fails(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a, b = await new_source(pool), await new_source(pool)
    sites = []
    for src in (a, b):
        site = blog(src)
        site.content[site.listing()] = fail("permanent", 403, "HTTP 403")
        sites.append(site)
    mgr, _ = manager(pool, World(*sites))

    out = await mgr.run("schedule")

    assert out is not None and out.status == "failed" and len(out.failed_sources) == 2
    assert (await cycles(pool))[0]["st"] == "failed"
    assert await pool.fetchval("select count(*) from crawl_runs where status = 'running'") == 0


async def test_failed_urls_that_are_not_page_one_leave_the_source_healthy(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    site = blog(a, "/v/1", "/v/2")
    site.content[site.url("/v/2")] = fail("permanent", 404, "HTTP 404")
    mgr, _ = manager(pool, World(site))

    out = await mgr.run("schedule")

    assert out is not None and out.status == "completed" and out.failed_sources == []
    (cycle,) = await cycles(pool)
    assert (cycle["failed_urls"], cycle["successful_urls"]) == (1, 2)
    assert json.loads(cycle["summary"])["http"] == {"200": 2, "404": 1}
    assert (await source_row(pool, a))["consecutive_failures"] == 0


async def test_pagination_that_ends_early_is_a_normal_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool, style="query", max_pages=5)
    site = Site(a, {})
    for p in (1, 2):
        site.content[site.listing(p)] = page_html(site.listing(p))
    site.content[site.listing(3)] = EMPTY
    mgr, _ = manager(pool, World(site), CRAWL_FOLLOW_LINKS=False)

    out = await mgr.run("schedule")

    assert out is not None and out.status == "completed"
    (cycle,) = await cycles(pool)
    assert (cycle["successful_urls"], cycle["skipped_urls"], cycle["failed_urls"]) == (3, 2, 0)


async def test_a_retried_url_is_counted_and_the_cycle_completes(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    site = blog(a)
    html = page_html("list")
    site.content[site.listing()] = lambda n: (
        fail("transient", 503, "HTTP 503")
        if n == 1
        else FetchResult("u", "ok", text=html, http_status=200, response_ms=5, size_bytes=len(html))
    )
    mgr, _ = manager(pool, World(site))

    out = await mgr.run("schedule")

    assert out is not None and out.status == "completed"
    assert json.loads((await cycles(pool))[0]["summary"])["urls"]["retried_attempts"] == 1


async def test_a_worker_that_died_mid_fetch_does_not_stop_the_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a))
    cycle = await queue.start_cycle("schedule")
    await queue.enqueue(
        cycle,
        a.id,
        [
            NewUrl(
                url=f"http://{a.slug}.onion{BASE_PATH}",
                url_normalized=f"http://{a.slug}.onion{BASE_PATH}",
                kind="listing",
                page_no=1,
            )
        ],
    )
    dead = await queue.claim(
        "dead-worker",
        browser=False,
        lane_limit=5,
        per_source_limit=5,
        lease_seconds=60,
        cycle_id=cycle,
    )
    assert dead is not None
    await pool.execute("update crawl_urls set leased_until = now() - interval '1 s'")  # it died

    mgr, _ = manager(pool, world, CRAWL_FOLLOW_LINKS=False)
    out = await mgr.run("schedule")  # a later tick joins the running cycle

    assert out is not None and out.cycle_id == cycle and out.status == "completed"
    row = await pool.fetchrow("select *, status::text as st from crawl_urls")
    assert row["st"] == "succeeded" and row["attempt"] == 2
    assert len(await cycles(pool)) == 1


async def test_a_cycle_nobody_is_working_is_abandoned_and_unblocks_new_ones(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    await pool.execute("update sources set consecutive_failures = 1 where id = $1", a.id)
    cycle = await queue.start_cycle("schedule")
    await queue.enqueue(cycle, a.id, pages(a.id, 3))
    await queue.ensure_run(cycle, a.id)
    await pool.execute("update crawl_cycles set heartbeat_at = now() - interval '2 hours'")

    reports = await queue.abandon_stale_cycles(3600)

    assert [x.status for x in reports] == ["abandoned"]
    assert (await cycles(pool))[0]["st"] == "abandoned"
    assert await pool.fetchval("select count(*) from crawl_urls where status = 'failed'") == 3
    assert (await runs(pool, a))[0]["st"] == "failed"
    assert (await source_row(pool, a))["consecutive_failures"] == 1  # the site was not at fault
    assert await queue.start_cycle("schedule") is not None  # no longer blocked


# =============================================================================== finalization


async def test_a_cycle_is_not_finalized_while_work_remains(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    cycle = await queue.start_cycle("schedule")
    await queue.enqueue(cycle, a.id, pages(a.id, 2))
    kw = dict(browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle)

    assert await queue.finalize_cycle(cycle) is None  # queued
    claimed = await queue.claim("w", **kw)
    assert await queue.finalize_cycle(cycle) is None  # running
    await queue.fail(
        claimed.id,
        "w",
        error="HTTP 503",
        http_status=503,
        response_ms=1,
        transient=True,
        max_attempts=3,
        retry_delay_seconds=60,
        recrawl_after_seconds=900,
    )
    assert await queue.finalize_cycle(cycle) is None  # waiting to retry
    assert (await cycles(pool))[0]["st"] == "running"

    # An expired lease is still "running" until it is recovered: not finished.
    await pool.execute("update crawl_urls set status = 'succeeded'")
    await pool.execute(
        "update crawl_urls set status = 'running', leased_by = 'dead', "
        "leased_until = now() - interval '1 s' where id = (select min(id) from crawl_urls)"
    )
    assert await queue.finalize_cycle(cycle) is None

    # Recovered and finished: now it closes.
    await pool.execute("update crawl_urls set status = 'succeeded', leased_by = null")
    report = await queue.finalize_cycle(cycle)
    assert report is not None and report.status == "completed"


async def test_concurrent_finalizers_close_a_cycle_exactly_once(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    await pool.execute("update sources set consecutive_failures = 0 where id = $1", a.id)
    cycle = await queue.start_cycle("schedule")
    await queue.enqueue(cycle, a.id, pages(a.id, 1))
    await queue.ensure_run(cycle, a.id)
    claimed = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )
    await queue.fail(
        claimed.id,
        "w",
        error="HTTP 403",
        http_status=403,
        response_ms=1,
        transient=False,
        max_attempts=3,
        retry_delay_seconds=0,
        recrawl_after_seconds=900,
    )  # page 1 failed: the source failed

    reports = await asyncio.gather(*(queue.finalize_cycle(cycle) for _ in range(12)))

    assert sum(x is not None for x in reports) == 1
    assert len(await cycles(pool)) == 1
    # Done once, not twelve times: the source failed once.
    assert (await source_row(pool, a))["consecutive_failures"] == 1
    assert len(await runs(pool, a)) == 1


async def test_pages_stored_in_a_cycle_are_filed_under_its_source_run(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    world = World(blog(a, "/v/1"))
    mgr, ingest = manager(pool, world)
    await mgr.run("schedule")

    (run,) = await runs(pool, a)
    assert set(ingest.run_ids) == {run["id"]}
    assert (run["pages_fetched"], run["pages_changed"], run["st"]) == (2, 2, "succeeded")
    assert run["depth"] == "deep"  # it followed a link


async def test_the_report_is_stored_and_carries_no_page_content(pool, queue) -> None:  # type: ignore[no-untyped-def]
    a = await new_source(pool)
    mgr, _ = manager(pool, World(blog(a, "/v/1")))
    out = await mgr.run("schedule")

    text = out.report.text()
    assert "Crawl Cycle" in text and "Total URLs:" in text and "Duration:" in text
    assert "Northwind" not in text and "northwind.example" not in text
    stored = (await cycles(pool))[0]["summary"]
    assert "Northwind" not in stored and json.loads(stored)["urls"]["total"] == 2
