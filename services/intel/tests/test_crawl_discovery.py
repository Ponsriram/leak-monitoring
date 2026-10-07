# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
"""Phase 3: eager pagination, empty-page pruning, and controlled link discovery.

The pure rules (what a page count is, what ends a listing, what a link may be) are tested
without a database. The queue behaviour — pruning, the page-1 gate, link limits, concurrent
discovery — runs against the scratch Postgres, through the real fetch handler and workers.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import pytest
from test_crawl_fetch import FakeIngestStorage, fail, ok  # noqa: F401
from test_crawl_queue import make_source, pages, pool, queue  # noqa: F401 - fixtures
from test_pipeline_concurrency import make_settings

from intel.collectors import FetchResult
from intel.crawl.fetch import CrawlFetcher
from intel.crawl.frontier import (
    admit_link,
    is_allowed,
    is_internal_host,
    listing_boundary,
    normalize_crawl_url,
)
from intel.crawl.queue import ClaimedUrl, NewUrl
from intel.crawl.seeding import listing_urls, pages_to_seed, seed_source
from intel.crawl.worker import CrawlWorkers
from intel.storage import SourceRow, Storage

HOST = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"


def cfg(**overrides: object):  # type: ignore[no-untyped-def]
    base = {
        "CRAWL_WORKERS": 1,  # one worker: what is queued when a result lands is deterministic
        "CRAWL_PER_SOURCE_INFLIGHT": 3,
        "CRAWL_RETRY_BACKOFF": 0,
        "CRAWL_MAX_ATTEMPTS": 3,
        "EXPOSURE_DETECTION": False,
    }
    base.update(overrides)
    return make_settings(**base)


# =============================================================================== pure rules


def test_url_identity_collapses_only_what_is_the_same_page() -> None:
    same = [
        "http://x.onion/page",
        "http://x.onion/page/",
        "http://x.onion/page?utm_source=x",
        "http://x.onion/page?utm_source=y",
    ]
    assert len({normalize_crawl_url(u) for u in same}) == 1

    different = [
        "http://x.onion/page",
        "http://x.onion/page?id=1",
        "http://x.onion/page?id=2",
        "http://x.onion/page?page=2",
    ]
    assert len({normalize_crawl_url(u) for u in different}) == 4


@pytest.mark.parametrize(
    ("max_pages", "observed", "style", "expected"),
    [
        (10, None, "query", 10),  # first crawl: everything configured
        (10, 4, "query", 6),  # later: deepest seen + 2
        (10, 9, "query", 10),  # never past max_pages
        (10, 10, "path", 10),
        (5, 1, "offset", 3),
        (5, None, "none", 1),  # the base URL is the whole listing
        (5, 3, "none", 1),
        (0, None, "query", 0),
    ],
)
def test_how_many_pages_are_seeded(
    max_pages: int, observed: int | None, style: str, expected: int
) -> None:
    assert pages_to_seed(max_pages, observed, style) == expected


def _source(style: str, base: str = f"http://{HOST}/list", **kw) -> SourceRow:  # type: ignore[no-untyped-def]
    return SourceRow(
        id=1,
        slug="s",
        name="s",
        base_url=base,
        collector="http",
        pagination_style=style,
        max_pages=kw.pop("max_pages", 5),
        crawl_interval_seconds=900,
        request_delay_seconds=0,
        enabled=True,
        last_crawl_at=None,
        consecutive_failures=0,
        **kw,
    )


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        (
            "query",
            [f"http://{HOST}/list", f"http://{HOST}/list?page=2", f"http://{HOST}/list?page=3"],
        ),
        (
            "path",
            [f"http://{HOST}/list", f"http://{HOST}/list/page/2", f"http://{HOST}/list/page/3"],
        ),
        (
            "offset",
            [
                f"http://{HOST}/list",
                f"http://{HOST}/list?offset=25",
                f"http://{HOST}/list?offset=50",
            ],
        ),
    ],
)
def test_pagination_is_built_from_the_declared_pattern(style: str, expected: list[str]) -> None:
    built = listing_urls(_source(style), 3)
    assert [u.url for u in built] == expected
    assert [u.page_no for u in built] == [1, 2, 3]
    assert all(u.kind == "listing" and u.depth == 0 for u in built)


def test_an_unpaginated_source_has_one_page_however_many_are_asked_for() -> None:
    assert [u.page_no for u in listing_urls(_source("none"), 5)] == [1]


def test_a_source_configured_at_an_internal_address_is_not_seeded() -> None:
    assert listing_urls(_source("query", base="http://127.0.0.1:5432/list"), 3) == []
    assert listing_urls(_source("query", base="http://169.254.169.254/latest"), 3) == []
    assert listing_urls(_source("query", base="ftp://x.onion/list"), 3) == []


def test_seeded_pages_are_normalized() -> None:
    (first, second) = listing_urls(_source("query", base=f"http://{HOST.upper()}/list/"), 2)
    assert first.url_normalized == f"http://{HOST}/list"
    assert second.url_normalized == f"http://{HOST}/list?page=2"


# ---------------------------------------------------------------- the allowlist


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/victim/1",
        f"https://{HOST}/v?id=3",
    ],
)
def test_same_host_pages_are_admitted(url: str) -> None:
    assert admit_link(url, allowed_hosts={HOST})


@pytest.mark.parametrize(
    "url",
    [
        "http://other.onion/victim/1",  # another host
        f"http://{HOST}.evil.com/",  # lookalike
        f"http://{HOST}@evil.com/",  # userinfo trick
        "http://127.0.0.1/",  # loopback
        "http://localhost/admin",
        "http://[::1]/",
        "http://10.1.2.3/",  # private
        "http://192.168.0.10/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # link-local / cloud metadata
        "http://0.0.0.0/",
        "ftp://" + HOST + "/x",  # not http(s)
        "file:///etc/passwd",
        "javascript:alert(1)",
        f"http://{HOST}/dump.zip",  # static / file resources
        f"http://{HOST}/data.sql",
        f"http://{HOST}/proof.png",
        f"http://{HOST}/report.pdf",
        f"http://{HOST}/login",  # account pages
        f"http://{HOST}/register",
    ],
)
def test_everything_else_is_refused(url: str) -> None:
    assert not admit_link(url, allowed_hosts={HOST})


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "localhost"])
def test_an_internal_host_is_refused_even_if_the_source_lists_it(host: str) -> None:
    assert is_internal_host(host)
    assert not is_allowed(f"http://{host}/", allowed_hosts={host})


def test_an_onion_name_is_not_internal() -> None:
    assert not is_internal_host(HOST)


# ---------------------------------------------------------------- what ends a listing


def boundary(**kw) -> int | None:  # type: ignore[no-untyped-def]
    base = {
        "kind": "listing",
        "page_no": 3,
        "result": "",
        "http_status": None,
        "ok": False,
        "final_failure": False,
    }
    base.update(kw)
    return listing_boundary(**base)


@pytest.mark.parametrize("page", [2, 3, 5])
def test_an_empty_page_ends_the_listing_just_before_it(page: int) -> None:
    assert boundary(page_no=page, ok=True, result="empty") == page - 1


@pytest.mark.parametrize("status", [404, 410])
def test_a_page_that_does_not_exist_ends_the_listing(status: int) -> None:
    assert boundary(page_no=4, http_status=status, final_failure=True) == 3


def test_page_one_failing_for_good_leaves_nothing_worth_fetching() -> None:
    assert boundary(page_no=1, final_failure=True, http_status=403) == 1
    assert boundary(page_no=1, final_failure=True, http_status=None) == 1  # all retries spent
    assert boundary(page_no=1, result="empty", final_failure=True) == 1


@pytest.mark.parametrize(
    "case",
    [
        {"page_no": 3, "final_failure": True, "http_status": 403},  # refused, not absent
        {"page_no": 3, "final_failure": True, "http_status": 503},
        {"page_no": 3, "final_failure": True, "http_status": None},  # a timeout
        {"page_no": 3, "final_failure": True, "http_status": 200},  # oversized / gate
        {"page_no": 3, "ok": True, "result": "changed"},  # a real page
        {"page_no": 3, "ok": True, "result": "unchanged"},
        {"page_no": 1, "ok": True, "result": "new"},
        {"page_no": 1, "ok": False, "final_failure": False},  # still retrying
    ],
)
def test_nothing_else_is_evidence_of_an_end(case: dict) -> None:  # type: ignore[type-arg]
    assert boundary(**case) is None


def test_a_followed_link_never_ends_a_listing() -> None:
    assert boundary(kind="link", page_no=None, ok=True, result="empty") is None
    assert boundary(kind="link", page_no=None, final_failure=True, http_status=404) is None


# =============================================================================== database

BASE_PATH = "/list"


async def new_source(
    pool, *, style: str = "none", max_pages: int = 1, collector: str = "http"
) -> SourceRow:  # type: ignore[no-untyped-def]
    slug = f"cq-{uuid.uuid4().hex[:8]}"
    await pool.execute(
        """
        insert into sources (slug, name, base_url, collector, pagination_style, max_pages,
                             crawl_interval_seconds, deep_crawl_interval_seconds)
        values ($1, $1, $2, $3::collector_kind, $4, $5, 900, 21600)
        """,
        slug,
        f"http://{slug}.onion{BASE_PATH}",
        collector,
        style,
        max_pages,
    )
    source = await Storage(pool).get_source(slug)  # type: ignore[arg-type]
    assert source is not None
    return source


def page_html(url: str, *links: str, text: str = "") -> str:
    """A page long enough to read as a listing, unique per URL so hashes never collide."""
    anchors = "".join(f'<a href="{link}">link</a>' for link in links)
    return (
        f"<html><body><h1>Leaked data</h1><p>Northwind Logistics {uuid.uuid4().hex[:6]} for "
        f"{url} - northwind.example - 2026-02-10 {text}</p><p>This listing has been published "
        f"in full.</p>{anchors}</body></html>"
    )


class Site:
    """A scripted onion site: url -> HTML, or a callable (url, call number) -> FetchResult."""

    name = "http"

    def __init__(self, source: SourceRow, content: dict, *, latency: float = 0.0) -> None:  # type: ignore[type-arg]
        self.source = source
        self.content = content
        self.latency = latency
        self.calls: list[str] = []
        self.timeline: list[tuple[str, float, float]] = []
        self.counts: dict[str, int] = {}

    def url(self, path: str = "") -> str:
        return f"http://{self.source.slug}.onion{path}"

    def listing(self, page_no: int = 1) -> str:
        return self.url(BASE_PATH) if page_no == 1 else self.url(f"{BASE_PATH}?page={page_no}")

    async def fetch_detailed(self, url: str) -> FetchResult:
        started = time.monotonic()
        self.calls.append(url)
        self.counts[url] = self.counts.get(url, 0) + 1
        if self.latency:
            await asyncio.sleep(self.latency)
        entry = self.content.get(url)
        if isinstance(entry, FetchResult):
            result = entry
        elif callable(entry):
            result = entry(self.counts[url])
        elif entry is None:
            result = fail("permanent", 404, "HTTP 404")
        else:
            result = ok(entry)
        self.timeline.append((url, started, time.monotonic()))
        return result

    async def aclose(self) -> None:
        return None


async def run_cycle(pool, queue, source, site, *, config=None, seed: bool = True):  # type: ignore[no-untyped-def]
    config = config or cfg()
    cycle = await queue.start_cycle("cli")
    if seed:
        await seed_source(queue, cycle, source)
    storage = FakeIngestStorage()
    fetcher = CrawlFetcher(storage, config, collectors={"http": site}, queue=queue)
    stats = await CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle)
    return stats, storage, cycle


async def rows(pool, source: SourceRow, **where):  # type: ignore[no-untyped-def]
    return await pool.fetch(
        "select *, status::text as st, kind::text as k from crawl_urls where source_id = $1 "
        "order by id",
        source.id,
    )


async def by_page(pool, source: SourceRow) -> dict[int, dict]:  # type: ignore[no-untyped-def,type-arg]
    return {
        r["page_no"]: dict(r)
        for r in await pool.fetch(
            "select page_no, status::text as st, result, attempt from crawl_urls "
            "where source_id = $1 and kind = 'listing'",
            source.id,
        )
    }


# ---------------------------------------------------------------- eager seeding


async def test_every_page_is_queued_up_front(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=8)
    cycle = await queue.start_cycle("cli")

    result = await seed_source(queue, cycle, source)

    assert result.inserted == 8
    got = await rows(pool, source)
    assert sorted(r["page_no"] for r in got) == list(range(1, 9))
    assert {r["st"] for r in got} == {"queued"}
    assert {r["cycle_id"] for r in got} == {cycle}


async def test_seeding_twice_does_not_duplicate_pages(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=5)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    again = await seed_source(queue, cycle, source)

    assert again.inserted == 0
    assert len(await rows(pool, source)) == 5


async def test_later_cycles_seed_only_what_was_observed_plus_two(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=10)
    first = await queue.start_cycle("cli")
    await seed_source(queue, first, source)  # a first crawl: every configured page
    assert len(await rows(pool, source)) == 10
    assert await queue.observed_depth(source.id) is None

    # Pages 1-3 have content; 4 was empty; the rest were never reached.
    await pool.execute(
        """
        update crawl_urls set status = 'succeeded', result = 'new',
               next_crawl_at = now() - interval '1 second' where page_no <= 3
        """
    )
    await pool.execute(
        """
        update crawl_urls set status = 'skipped', result = 'past_end',
               next_crawl_at = now() - interval '1 second' where page_no > 3
        """
    )
    await pool.execute("update crawl_cycles set status = 'completed'")
    assert await queue.observed_depth(source.id) == 3

    second = await queue.start_cycle("cli")
    await seed_source(queue, second, source)

    queued = sorted(
        r["page_no"]
        for r in await pool.fetch(
            "select page_no from crawl_urls where cycle_id = $1 and status = 'queued'", second
        )
    )
    assert queued == [1, 2, 3, 4, 5]  # min(10, 3 + 2)


async def test_pages_not_yet_due_are_left_alone(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=4)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    await pool.execute(
        "update crawl_urls set status = 'succeeded', result = 'new', "
        "next_crawl_at = now() + interval '1 hour'"
    )
    await pool.execute("update crawl_cycles set status = 'completed'")

    nxt = await queue.start_cycle("cli")
    result = await seed_source(queue, nxt, source)

    assert (result.inserted, result.requeued) == (0, 0)


async def test_page_one_and_deep_pages_follow_their_own_intervals(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=3)
    site = Site(
        source,
        {
            site_url: page_html(site_url)
            for site_url in (
                f"http://{source.slug}.onion{BASE_PATH}",
                f"http://{source.slug}.onion{BASE_PATH}?page=2",
                f"http://{source.slug}.onion{BASE_PATH}?page=3",
            )
        },
    )
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_FOLLOW_LINKS=False))

    due = {
        r["page_no"]: round(r["secs"])
        for r in await pool.fetch(
            "select page_no, extract(epoch from next_crawl_at - now()) as secs from crawl_urls "
            "where source_id = $1",
            source.id,
        )
    }
    assert 800 < due[1] <= 900  # the source's probe interval
    assert 21000 < due[2] <= 21600 and 21000 < due[3] <= 21600  # its deep-walk interval


# ---------------------------------------------------------------- pruning, end to end


def pages_content(source: SourceRow, n: int, *, special: dict | None = None) -> dict:  # type: ignore[type-arg]
    site = Site(source, {})
    content = {site.listing(p): page_html(site.listing(p)) for p in range(1, n + 1)}
    for page_no, entry in (special or {}).items():
        content[site.listing(page_no)] = entry
    return content


EMPTY = "<html><body></body></html>"


@pytest.mark.parametrize("empty_page", [3, 5])
async def test_an_empty_page_ends_the_listing_and_later_pages_are_skipped(
    pool, queue, empty_page
) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=8)
    site = Site(source, pages_content(source, 8, special={empty_page: EMPTY}))
    stats, _, _ = await run_cycle(pool, queue, source, site, config=cfg(CRAWL_FOLLOW_LINKS=False))

    got = await by_page(pool, source)
    assert site.calls == [site.listing(p) for p in range(1, empty_page + 1)]  # nothing past it
    assert got[empty_page]["result"] == "empty"
    for p in range(1, empty_page):
        assert got[p]["st"] == "succeeded"
    for p in range(empty_page + 1, 9):
        assert (got[p]["st"], got[p]["result"], got[p]["attempt"]) == ("skipped", "past_end", 0)
    assert stats.pruned == 8 - empty_page


async def test_an_empty_page_one_prunes_the_whole_listing(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=6)
    site = Site(source, pages_content(source, 6, special={1: EMPTY}))
    stats, _, _ = await run_cycle(pool, queue, source, site, config=cfg(CRAWL_FOLLOW_LINKS=False))

    got = await by_page(pool, source)
    assert site.calls == [site.listing(1)]
    assert got[1]["st"] == "failed" and got[1]["result"] == "empty"
    assert {got[p]["st"] for p in range(2, 7)} == {"skipped"}


@pytest.mark.parametrize("status", [404, 410])
async def test_a_missing_page_is_the_boundary(pool, queue, status) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=7)
    site = Site(
        source, pages_content(source, 7, special={4: fail("permanent", status, f"HTTP {status}")})
    )
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_FOLLOW_LINKS=False))

    got = await by_page(pool, source)
    assert site.calls == [site.listing(p) for p in range(1, 5)]
    assert got[3]["st"] == "succeeded"  # a completed page before the boundary is untouched
    assert got[4]["st"] == "failed"
    assert {got[p]["st"] for p in (5, 6, 7)} == {"skipped"}


@pytest.mark.parametrize(
    "bad",
    [
        fail("permanent", 403, "HTTP 403"),
        fail("permanent", 200, "response exceeds 5242880 bytes"),
        fail("transient", 503, "HTTP 503"),
        fail("transient", None, "timed out after 60s"),
    ],
    ids=["403", "oversized", "503-retried-out", "timeout-retried-out"],
)
async def test_a_failure_that_is_not_an_end_does_not_prune(pool, queue, bad) -> None:  # type: ignore[no-untyped-def]
    """The old crawler read every failed fetch as the end. A timeout on page 3 says nothing
    about page 4."""
    source = await new_source(pool, style="query", max_pages=5)
    site = Site(source, pages_content(source, 5, special={3: bad}))
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_FOLLOW_LINKS=False))

    got = await by_page(pool, source)
    assert got[3]["st"] == "failed"
    assert got[4]["st"] == "succeeded" and got[5]["st"] == "succeeded"
    assert not [r for r in await rows(pool, source) if r["st"] == "skipped"]


async def test_a_page_after_an_empty_one_that_was_already_in_flight_is_kept(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """Pruning saves requests not yet made; it never cancels or discards one that has been."""
    source = await new_source(pool, style="query", max_pages=6)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    kw = dict(browser=False, lane_limit=10, per_source_limit=10, lease_seconds=60, cycle_id=cycle)

    claimed = {}
    for _ in range(4):  # pages 1-4 claimed in page order
        c = await queue.claim("w", **kw)
        claimed[c.page_no] = c
    await queue.complete(
        claimed[1].id, "w", result="new", http_status=200, response_ms=1, recrawl_after_seconds=60
    )
    # Page 5 already finished (some earlier cycle's work): a completed page past the boundary.
    await pool.execute(
        "update crawl_urls set status = 'succeeded', result = 'new' where page_no = 5"
    )
    # Page 6 is waiting to retry.
    await pool.execute("update crawl_urls set status = 'retry' where page_no = 6")

    pruned = await queue.prune_listing(source.id, cycle, after_page=1)

    got = await by_page(pool, source)
    assert pruned == 1  # only page 6
    assert got[1]["st"] == "succeeded"
    assert {got[2]["st"], got[3]["st"], got[4]["st"]} == {"running"}  # in flight: left alone
    assert got[5]["st"] == "succeeded"  # finished: left alone
    assert (got[6]["st"], got[6]["result"]) == ("skipped", "past_end")  # retry pages are pruned

    # And an in-flight page past the boundary still completes and counts as a normal page.
    assert await queue.complete(
        claimed[3].id,
        "w",
        result="changed",
        http_status=200,
        response_ms=1,
        recrawl_after_seconds=60,
    )
    assert (await by_page(pool, source))[3]["st"] == "succeeded"


async def test_pruning_only_touches_this_source_and_this_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    mine = await new_source(pool, style="query", max_pages=5)
    other = await new_source(pool, style="query", max_pages=5)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, mine)
    await seed_source(queue, cycle, other)

    await queue.prune_listing(mine.id, cycle, after_page=2)

    assert {r["st"] for r in await rows(pool, other)} == {"queued"}
    assert sorted(r["page_no"] for r in await rows(pool, mine) if r["st"] == "skipped") == [3, 4, 5]


# ---------------------------------------------------------------- page 1 first


async def test_pages_behind_page_one_wait_for_it(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=4)
    site = Site(source, pages_content(source, 4), latency=0.15)
    await run_cycle(
        pool,
        queue,
        source,
        site,
        config=cfg(CRAWL_WORKERS=4, CRAWL_FOLLOW_LINKS=False, CRAWL_PAGE1_FIRST=True),
    )

    timeline = {u: (s, e) for u, s, e in site.timeline}
    page1_end = timeline[site.listing(1)][1]
    assert all(timeline[site.listing(p)][0] >= page1_end for p in (2, 3, 4))
    # ...and once released, they run together rather than one after another.
    starts = sorted(timeline[site.listing(p)][0] for p in (2, 3, 4))
    assert starts[-1] - starts[0] < 0.1


async def test_without_the_gate_every_page_starts_at_once(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=4)
    site = Site(source, pages_content(source, 4), latency=0.15)
    await run_cycle(
        pool,
        queue,
        source,
        site,
        config=cfg(
            CRAWL_WORKERS=4,
            CRAWL_PER_SOURCE_INFLIGHT=4,
            CRAWL_FOLLOW_LINKS=False,
            CRAWL_PAGE1_FIRST=False,
        ),
    )

    starts = [s for _, s, _ in site.timeline]
    assert max(starts) - min(starts) < 0.1


async def test_a_dead_source_costs_one_page_not_all_of_them(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool, style="query", max_pages=5)
    site = Site(source, pages_content(source, 5, special={1: fail("transient", 503, "HTTP 503")}))
    stats, _, _ = await run_cycle(
        pool, queue, source, site, config=cfg(CRAWL_WORKERS=4, CRAWL_FOLLOW_LINKS=False)
    )

    assert site.calls == [site.listing(1)] * 3  # page 1's three attempts, and nothing else
    got = await by_page(pool, source)
    assert got[1]["st"] == "failed"
    assert {got[p]["st"] for p in range(2, 6)} == {"skipped"}


# ---------------------------------------------------------------- link discovery


async def link_rows(pool, source: SourceRow):  # type: ignore[no-untyped-def]
    return await pool.fetch(
        "select *, status::text as st, kind::text as k from crawl_urls "
        "where source_id = $1 and kind = 'link' "
        "order by id",
        source.id,
    )


def tree(site: Site, depth: int, *, fanout: int = 1, prefix: str = "n") -> dict:  # type: ignore[type-arg]
    """base page -> fanout links -> each of those -> ... `depth` levels of link pages."""
    content: dict = {}  # type: ignore[type-arg]

    def build(path: str, level: int) -> None:
        kids = [f"{path}/{prefix}{i}" for i in range(fanout)] if level < depth else []
        content[site.url(path)] = page_html(site.url(path), *[k for k in kids])
        for kid in kids:
            build(kid, level + 1)

    build(BASE_PATH, 0)
    return content


async def test_a_link_is_queued_as_a_child_of_the_page_it_was_found_on(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {
        site.listing(): page_html("list", "/v/1"),
        site.url("/v/1"): page_html("v1", "/v/1/files"),
        site.url("/v/1/files"): page_html("files"),
    }
    _, _, cycle = await run_cycle(pool, queue, source, site)

    listing_id = await pool.fetchval(
        "select id from crawl_urls where source_id = $1 and kind = 'listing'", source.id
    )
    first, second = await link_rows(pool, source)
    assert (first["url"], first["depth"], first["parent_id"]) == (site.url("/v/1"), 1, listing_id)
    assert (second["url"], second["depth"], second["parent_id"]) == (
        site.url("/v/1/files"),
        2,
        first["id"],
    )
    assert {first["cycle_id"], second["cycle_id"]} == {cycle}
    assert first["page_no"] is None and first["k"] == "link"
    assert {first["st"], second["st"]} == {"succeeded"}


async def test_only_valid_links_are_queued(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    good = "/v/good"
    hostile = [
        "http://other.onion/v/1",
        "http://127.0.0.1/admin",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://169.254.169.254/latest",
        "ftp://x.onion/f",
        "javascript:alert(1)",
        "/dump.zip",
        "/data.sql",
        "/proof.png",
        "/login",
        "/register",
        "mailto:a@b.c",
    ]
    site.content = {
        site.listing(): page_html("list", *hostile, good),
        site.url(good): page_html("g"),
    }
    await run_cycle(pool, queue, source, site)

    assert [r["url"] for r in await link_rows(pool, source)] == [site.url(good)]
    assert set(site.calls) == {site.listing(), site.url(good)}  # nothing hostile was ever fetched


async def test_the_queue_refuses_hostile_links_even_if_a_caller_offers_them(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """The handler filters, but the queue is the gate that matters: it re-checks everything."""
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    parent = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )
    assert parent is not None

    def link(u: str) -> NewUrl:
        return NewUrl(url=u, url_normalized=normalize_crawl_url(u), kind="link")

    hostile = [
        "http://elsewhere.onion/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://169.254.169.254/a",
        "ftp://x.onion/a",
        f"http://{source.slug}.onion/x.zip",
        f"http://{source.slug}.onion/login",
        f"http://{source.slug}.onion@evil.com/a",
    ]
    result = await queue.enqueue_links(
        parent,
        [link(u) for u in hostile] + [link(f"http://{source.slug}.onion/ok")],
        max_depth=3,
        per_page_limit=50,
        source_cycle_limit=50,
    )

    assert (result.inserted, result.rejected) == (1, len(hostile))
    assert [r["url"] for r in await link_rows(pool, source)] == [f"http://{source.slug}.onion/ok"]


async def test_a_link_cannot_be_filed_under_another_source(pool, queue) -> None:  # type: ignore[no-untyped-def]
    mine = await new_source(pool)
    theirs = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, mine)
    parent = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )
    assert parent is not None and parent.source_id == mine.id

    target = f"http://{theirs.slug}.onion/victim"
    result = await queue.enqueue_links(
        parent,
        [NewUrl(url=target, url_normalized=normalize_crawl_url(target), kind="link")],
        max_depth=3,
        per_page_limit=5,
        source_cycle_limit=25,
    )

    assert (result.inserted, result.rejected) == (0, 1)
    assert await link_rows(pool, theirs) == [] and await link_rows(pool, mine) == []


async def test_links_stop_at_the_configured_depth(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = tree(site, depth=5)  # a chain five links deep
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_LINK_DEPTH=3))

    assert [r["depth"] for r in await link_rows(pool, source)] == [1, 2, 3]
    deepest = site.url(f"{BASE_PATH}/n0/n0/n0/n0")
    assert deepest not in site.calls  # depth 4 was never fetched


async def test_the_depth_limit_is_configurable(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = tree(site, depth=5)
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_LINK_DEPTH=1))
    assert [r["depth"] for r in await link_rows(pool, source)] == [1]


async def test_no_more_than_the_per_page_limit_is_taken_from_one_page(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    links = [f"/v/{n}" for n in range(12)]
    site.content = {
        site.listing(): page_html("list", *links),
        **{site.url(p): page_html(p) for p in links},
    }
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_LINKS_PER_PAGE=5))

    # The first five in page order: on a listing that is the newest victims.
    assert [r["url"] for r in await link_rows(pool, source)] == [site.url(p) for p in links[:5]]


async def test_the_per_source_per_cycle_limit_caps_the_whole_tree(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """5 per page over 3 levels could be 155 fetches; the cap is what bounds it."""
    source = await new_source(pool)
    site = Site(source, {})
    site.content = tree(site, depth=3, fanout=5)
    await run_cycle(
        pool, queue, source, site, config=cfg(CRAWL_LINK_MAX_PAGES=8, CRAWL_LINKS_PER_PAGE=5)
    )

    assert len(await link_rows(pool, source)) == 8


async def test_duplicate_links_become_one_job(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    spellings = [
        "/v/1",
        "/v/1/",
        "/v/1#top",
        "/v/1?utm_source=x",
        f"http://{source.slug}.onion/v/1",
    ]
    site.content = {
        site.listing(): page_html("list", *spellings),
        site.url("/v/1"): page_html("v1"),
    }
    await run_cycle(pool, queue, source, site)

    assert len(await link_rows(pool, source)) == 1
    assert site.calls.count(site.url("/v/1")) == 1


async def test_meaningful_query_parameters_are_separate_links(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    links = ["/v?id=1", "/v?id=2", "/v?page=2"]
    site.content = {
        site.listing(): page_html("list", *links),
        **{site.url(p): page_html(p) for p in links},
    }
    await run_cycle(pool, queue, source, site)

    assert len(await link_rows(pool, source)) == 3


async def test_a_link_already_queued_does_not_use_up_the_per_page_limit(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """Five slots go to five NEW links, not to the same top five every time."""
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    parent = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )

    def links(*ns: int) -> list[NewUrl]:
        return [
            NewUrl(
                url=f"http://{source.slug}.onion/v/{n}",
                url_normalized=normalize_crawl_url(f"http://{source.slug}.onion/v/{n}"),
                kind="link",
            )
            for n in ns
        ]

    kw = dict(max_depth=3, per_page_limit=3, source_cycle_limit=100)
    first = await queue.enqueue_links(parent, links(1, 2, 3, 4, 5, 6), **kw)
    second = await queue.enqueue_links(parent, links(1, 2, 3, 4, 5, 6), **kw)

    assert first.inserted == 3
    assert (second.inserted, second.known) == (3, 3)  # 1-3 are known; 4-6 take the slots
    assert len(await link_rows(pool, source)) == 6


async def test_two_workers_discovering_the_same_url_create_one_job(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    await queue.enqueue(
        cycle,
        source.id,
        [
            NewUrl(
                url=f"http://{source.slug}.onion/other{n}",
                url_normalized=f"http://{source.slug}.onion/other{n}",
                kind="listing",
                page_no=n + 2,
            )
            for n in range(8)
        ],
    )
    parents = []
    for i in range(8):
        parent = await queue.claim(
            f"w{i}",
            browser=False,
            lane_limit=20,
            per_source_limit=20,
            lease_seconds=60,
            cycle_id=cycle,
        )
        parents.append(parent)

    target = f"http://{source.slug}.onion/shared"
    candidate = NewUrl(url=target, url_normalized=normalize_crawl_url(target), kind="link")
    results = await asyncio.gather(
        *(
            queue.enqueue_links(
                p, [candidate], max_depth=3, per_page_limit=5, source_cycle_limit=25
            )
            for p in parents
        )
    )

    assert sum(r.inserted for r in results) == 1
    assert len(await link_rows(pool, source)) == 1


async def test_concurrent_discovery_cannot_overspend_the_cycle_budget(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(
        cycle,
        source.id,
        [
            NewUrl(
                url=f"http://{source.slug}.onion/p{n}",
                url_normalized=f"http://{source.slug}.onion/p{n}",
                kind="listing",
                page_no=n + 1,
            )
            for n in range(8)
        ],
    )
    parents = [
        await queue.claim(
            f"w{i}",
            browser=False,
            lane_limit=20,
            per_source_limit=20,
            lease_seconds=60,
            cycle_id=cycle,
        )
        for i in range(8)
    ]

    async def discover(i: int, parent: ClaimedUrl):  # type: ignore[no-untyped-def]
        urls = [f"http://{source.slug}.onion/p{i}/c{k}" for k in range(5)]
        return await queue.enqueue_links(
            parent,
            [NewUrl(url=u, url_normalized=u, kind="link") for u in urls],
            max_depth=3,
            per_page_limit=5,
            source_cycle_limit=12,
        )

    await asyncio.gather(*(discover(i, p) for i, p in enumerate(parents)))

    assert len(await link_rows(pool, source)) == 12  # exactly the budget, not 8 x 5


async def test_a_link_with_no_cycle_is_not_queued(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    parent = await queue.claim(
        "w", browser=False, lane_limit=5, per_source_limit=5, lease_seconds=60, cycle_id=cycle
    )
    parent.cycle_id = None  # type: ignore[misc]
    target = f"http://{source.slug}.onion/v"
    result = await queue.enqueue_links(
        parent,
        [NewUrl(url=target, url_normalized=target, kind="link")],
        max_depth=3,
        per_page_limit=5,
        source_cycle_limit=25,
    )
    assert result.accepted == 0 and await link_rows(pool, source) == []


# ---------------------------------------------------------------- links only from changed content


async def test_links_are_found_on_a_new_page(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", "/v/1"), site.url("/v/1"): page_html("v1")}
    await run_cycle(pool, queue, source, site)
    assert len(await link_rows(pool, source)) == 1


async def test_an_unchanged_page_yields_no_links(pool, queue) -> None:  # type: ignore[no-untyped-def]
    from intel.collectors import to_text
    from intel.storage import content_hash

    source = await new_source(pool)
    site = Site(source, {})
    html = page_html("list", "/v/1")
    site.content = {site.listing(): html, site.url("/v/1"): page_html("v1")}
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    await pool.execute("update crawl_urls set content_sha256 = $1", content_hash(to_text(html)))

    config = cfg()
    storage = FakeIngestStorage()
    fetcher = CrawlFetcher(storage, config, collectors={"http": site}, queue=queue)
    await CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle)

    assert storage.upserts == 0  # extraction did not run...
    assert await link_rows(pool, source) == []  # ...and neither did discovery
    assert site.calls == [site.listing()]


async def test_a_page_whose_ingestion_failed_yields_no_links(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", "/v/1"), site.url("/v/1"): page_html("v1")}
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)

    config = cfg()
    storage = FakeIngestStorage(fail_upserts=99)
    fetcher = CrawlFetcher(storage, config, collectors={"http": site}, queue=queue)
    await CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle)

    assert await link_rows(pool, source) == []
    assert (await by_page(pool, source))[1]["st"] == "failed"


async def test_a_page_already_extracted_elsewhere_yields_no_links(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """Identical content already stored and extracted (say by the legacy crawler) is unchanged."""
    from intel.collectors import to_text
    from intel.storage import content_hash

    source = await new_source(pool)
    site = Site(source, {})
    html = page_html("list", "/v/1")
    site.content = {site.listing(): html, site.url("/v/1"): page_html("v1")}
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)

    config = cfg()
    storage = FakeIngestStorage()
    storage.extracted.add(content_hash(to_text(html)))
    fetcher = CrawlFetcher(storage, config, collectors={"http": site}, queue=queue)
    await CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle)

    assert storage.upserts == 0
    assert await link_rows(pool, source) == []


async def test_a_worker_that_lost_its_lease_discovers_nothing(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", "/v/1"), site.url("/v/1"): page_html("v1")}

    original = site.fetch_detailed

    async def stalled(url: str) -> FetchResult:
        await pool.execute(
            "update crawl_urls set leased_by = 'thief', leased_until = now() + interval '60 s'"
        )
        return await original(url)

    site.fetch_detailed = stalled  # type: ignore[method-assign]
    cycle = await queue.start_cycle("cli")
    await seed_source(queue, cycle, source)
    config = cfg()
    fetcher = CrawlFetcher(FakeIngestStorage(), config, collectors={"http": site}, queue=queue)
    stop = asyncio.Event()
    task = asyncio.create_task(
        CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle, stop=stop)
    )
    await asyncio.sleep(0.6)
    stop.set()
    stats = await task

    assert stats.lost_lease == 1
    assert await link_rows(pool, source) == []  # the URL's new holder will find them


# ---------------------------------------------------------------- link pages, runs and cadence


async def test_a_followed_page_is_stored_but_not_run_through_the_listing_extractor(
    pool, queue
) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {
        site.listing(): page_html("list", "/v/1", "/v/2"),
        site.url("/v/1"): page_html("v1"),
        site.url("/v/2"): page_html("v2"),
    }
    _, storage, _ = await run_cycle(pool, queue, source, site)

    assert len(storage.saves) == 3  # the listing and both followed pages are stored...
    assert storage.upserts == 1  # ...but only the listing was run through extraction


async def test_followed_pages_use_the_longer_recrawl_interval(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", "/v/1"), site.url("/v/1"): page_html("v1")}
    await run_cycle(pool, queue, source, site, config=cfg(CRAWL_LINK_INTERVAL=1234))

    link_secs = await pool.fetchval(
        "select extract(epoch from next_crawl_at - now()) from crawl_urls where kind = 'link'"
    )
    listing_secs = await pool.fetchval(
        "select extract(epoch from next_crawl_at - now()) from crawl_urls where kind = 'listing'"
    )
    assert 1100 < link_secs <= 1234
    assert 800 < listing_secs <= 900  # not confused with the listing's own cadence


async def test_the_default_link_interval_is_a_week() -> None:
    assert make_settings().link_recrawl_seconds == 7 * 24 * 3600


async def test_pages_are_filed_under_one_crawl_run_per_source_per_cycle(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    site = Site(source, {})
    site.content = {site.listing(): page_html("list", "/v/1"), site.url("/v/1"): page_html("v1")}
    _, storage, cycle = await run_cycle(pool, queue, source, site)

    runs = await pool.fetch(
        "select id from crawl_runs where cycle_id = $1 and source_id = $2", cycle, source.id
    )
    assert len(runs) == 1
    assert set(storage.run_ids) == {runs[0]["id"]}  # every stored page carried that run


async def test_concurrent_workers_share_one_run_row(pool, queue) -> None:  # type: ignore[no-untyped-def]
    source = await new_source(pool)
    cycle = await queue.start_cycle("cli")
    ids = await asyncio.gather(*(queue.ensure_run(cycle, source.id) for _ in range(12)))
    assert len(set(ids)) == 1
    assert await pool.fetchval("select count(*) from crawl_runs where cycle_id = $1", cycle) == 1


# ---------------------------------------------------------------- the acceptance picture


async def test_a_large_frontier_is_processed_concurrently_and_deduplicated(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """sources -> many listing URLs -> concurrent workers -> changed pages -> controlled link
    discovery -> more URLs -> one deduplicated frontier."""
    sources = [await new_source(pool, style="query", max_pages=4) for _ in range(3)]
    sites = []
    for s in sources:
        site = Site(s, {}, latency=0.02)
        for p in range(1, 5):
            # Every listing page links to the same two victim pages: shared, so deduplicated.
            site.content[site.listing(p)] = page_html(site.listing(p), "/v/shared-1", "/v/shared-2")
        site.content[site.url("/v/shared-1")] = page_html("s1")
        site.content[site.url("/v/shared-2")] = page_html("s2")
        sites.append(site)

    class Router:
        name = "http"

        async def fetch_detailed(self, url: str) -> FetchResult:
            for site in sites:
                if f"{site.source.slug}.onion" in url:
                    return await site.fetch_detailed(url)
            return fail("permanent", 404, "HTTP 404")

        async def aclose(self) -> None:
            return None

    cycle = await queue.start_cycle("cli")
    for s in sources:
        await seed_source(queue, cycle, s)
    config = cfg(CRAWL_WORKERS=6, CRAWL_PER_SOURCE_INFLIGHT=3)
    storage = FakeIngestStorage()
    fetcher = CrawlFetcher(storage, config, collectors={"http": Router()}, queue=queue)
    stats = await CrawlWorkers(queue, fetcher, config, poll_seconds=0.05).run(cycle)

    for s in sources:
        got = await rows(pool, s)
        assert len([r for r in got if r["k"] == "listing"]) == 4
        assert len([r for r in got if r["k"] == "link"]) == 2  # 8 mentions, 2 URLs
        assert {r["st"] for r in got} == {"succeeded"}
    assert stats.ok == 3 * (4 + 2)
    assert datetime.now(UTC)  # (keeps the import honest if a time assertion is added)
