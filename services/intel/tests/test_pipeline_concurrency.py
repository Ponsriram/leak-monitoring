"""How `crawl_source` walks a listing.

These are the tests for the change from "fetch page 1, wait, fetch page 2, wait…" to
doubling concurrent waves. Two things have to hold at once, and they pull against each
other:

* pages of one source are genuinely in flight together, otherwise the change bought nothing;
* the listing still ends at the first page that comes back empty, and pages a wave
  speculated on past that point are dropped rather than stored — the sequential crawler's
  contract, which page numbering downstream depends on.

Everything is faked. A test that needs Tor is a test nobody runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from intel import pipeline as pipeline_module
from intel.config import Settings
from intel.pipeline import crawl_source, due_sources
from intel.storage import SourceRow, UpsertResult


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql://unused/unused",
        "CRAWL_PAGE_CONCURRENCY": 4,
        "CRAWL_DISCOVER_MIRRORS": False,
        "CRAWL_MIRROR_FAILOVER": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_source(**overrides: object) -> SourceRow:
    base: dict[str, object] = {
        "id": 1,
        "slug": "testgroup",
        "name": "Test Group",
        "base_url": "http://example.onion",
        "collector": "http",
        "pagination_style": "query",
        "max_pages": 10,
        "crawl_interval_seconds": 3600,
        # Zero, so the politeness stagger does not make these tests sleep.
        "request_delay_seconds": 0,
        "enabled": True,
        "last_crawl_at": None,
        "consecutive_failures": 0,
    }
    base.update(overrides)
    return SourceRow(**base)  # type: ignore[arg-type]


class FakeCollector:
    """Records how many fetches overlap, and how long the whole walk took in rounds."""

    name = "http"

    def __init__(self, pages: dict[int, str | None], *, latency: float = 0.02) -> None:
        self._pages = pages
        self._latency = latency
        self.last_error: str | None = None
        self.requested: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    @staticmethod
    def page_of(url: str) -> int:
        return int(url.split("page=")[1]) if "page=" in url else 1

    async def fetch(self, url: str) -> str | None:
        self.requested.append(url)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._latency)
            return self._pages.get(self.page_of(url))
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        return None


class FakeStorage:
    """Just enough of `Storage` for the crawl loop, with the pages it was handed."""

    def __init__(self) -> None:
        self.saved: list[tuple[int, str]] = []
        self.finished: dict[str, object] = {}

    async def start_crawl(self, source_id: int) -> int:
        return 99

    async def known_onion_hosts(self) -> set[str]:
        return set()

    async def save_page(self, *, source_id, crawl_run_id, url, page_no, text):  # type: ignore[no-untyped-def]
        self.saved.append((page_no, text))
        return len(self.saved), True

    async def upsert_leaks(self, leaks, *, source_id):  # type: ignore[no-untyped-def]
        return UpsertResult()

    async def mark_extracted(self, raw_page_id: int) -> None:
        return None

    async def finish_crawl(self, run_id, source_id, **kwargs):  # type: ignore[no-untyped-def]
        self.finished = kwargs


def listing(page_no: int) -> str:
    """A page with enough text to clear MIN_PAGE_TEXT_CHARS and one recognisable victim."""
    return (
        f"<html><body><h1>Leaked data</h1>"
        f"<p>Northwind Logistics {page_no} — northwind{page_no}.example — 2026-02-10</p>"
        f"<p>This listing has been published in full.</p></body></html>"
    )


async def run(source: SourceRow, collector: FakeCollector, settings: Settings) -> tuple:
    storage = FakeStorage()
    result = await crawl_source(
        source, storage=storage, settings=settings, extractor_name="rules"
    )
    return result, storage


@pytest.fixture(autouse=True)
def use_fake_collector(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """`get_collector` is called inside `crawl_source`, so the fake has to be injected here."""
    holder: dict[str, FakeCollector] = {}

    def install(collector: FakeCollector) -> FakeCollector:
        holder["collector"] = collector
        monkeypatch.setattr(
            pipeline_module, "get_collector", lambda *args, **kwargs: collector
        )
        return collector

    return install


async def test_pages_of_one_source_are_fetched_concurrently(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    collector = use_fake_collector(
        FakeCollector({no: listing(no) for no in range(1, 11)})
    )
    result, _ = await run(make_source(), collector, make_settings())

    assert result.pages_fetched == 10
    # Page 1 alone, then waves of 4 and 5, held to CRAWL_PAGE_CONCURRENCY at a time. If
    # anything had serialised the walk this would be 1.
    assert collector.peak_in_flight == 4


async def test_the_walk_takes_logarithmically_many_rounds(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    """Wall clock is what this change is for: 10 pages must not cost 10 round trips."""
    latency = 0.05
    collector = use_fake_collector(
        FakeCollector({no: listing(no) for no in range(1, 11)}, latency=latency)
    )

    started = asyncio.get_running_loop().time()
    await run(make_source(), collector, make_settings())
    elapsed = asyncio.get_running_loop().time() - started

    # Three waves: [1], [2-5], [6-10]. Sequentially this would be 10 * latency.
    assert elapsed < 6 * latency


async def test_the_listing_still_ends_at_the_first_empty_page(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    """Page 4 is empty, so pages 5+ are past the end even though a wave already fetched them."""
    pages: dict[int, str | None] = {no: listing(no) for no in (1, 2, 3)}
    pages[4] = "<html><body></body></html>"
    collector = use_fake_collector(FakeCollector(pages))

    result, storage = await run(make_source(), collector, make_settings())

    assert result.status == "succeeded"
    assert [page_no for page_no, _ in storage.saved] == [1, 2, 3]
    # Pages 5 was in the same wave as 4 and came back after the listing had ended.
    assert result.pages_discarded == 1


async def test_a_wave_that_overshoots_does_not_store_pages_past_the_end(  # noqa: E501
    use_fake_collector,  # type: ignore[no-untyped-def]
) -> None:
    """A site that 404s page 3 must not have page 4's content filed under it."""
    pages: dict[int, str | None] = {1: listing(1), 2: listing(2), 3: None, 4: listing(4)}
    collector = use_fake_collector(FakeCollector(pages))

    result, storage = await run(make_source(), collector, make_settings())

    assert [page_no for page_no, _ in storage.saved] == [1, 2]
    assert result.pages_fetched == 2


async def test_page_one_is_fetched_before_anything_else(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    """A failover on page 1 changes the address every later page would come from."""
    collector = use_fake_collector(FakeCollector({1: None}))
    result, _ = await run(make_source(), collector, make_settings())

    assert collector.requested == ["http://example.onion"]
    assert result.status == "failed"


async def test_an_unpaginated_source_fetches_exactly_one_page(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    collector = use_fake_collector(FakeCollector({1: listing(1)}))
    result, _ = await run(
        make_source(pagination_style="none"), collector, make_settings()
    )

    assert collector.requested == ["http://example.onion"]
    assert result.pages_fetched == 1


async def test_a_challenge_page_on_page_one_still_fails_loudly(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    collector = use_fake_collector(FakeCollector({1: "<html><body>ok</body></html>"}))
    result, _ = await run(make_source(), collector, make_settings())

    assert result.status == "failed"
    assert "challenge page" in (result.error or "")


async def test_the_shared_fetch_budget_bounds_a_wave(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    """The run-wide budget, not the per-source width, is the real ceiling."""
    collector = use_fake_collector(
        FakeCollector({no: listing(no) for no in range(1, 11)})
    )
    storage = FakeStorage()
    await crawl_source(
        make_source(),
        storage=storage,  # type: ignore[arg-type]
        settings=make_settings(CRAWL_PAGE_CONCURRENCY=8),
        extractor_name="rules",
        fetch_slots=asyncio.Semaphore(2),
    )

    assert collector.peak_in_flight <= 2


# ---------------------------------------------------------------- due-source selection


def test_a_source_never_crawled_is_due() -> None:
    assert due_sources([make_source(last_crawl_at=None)])


def test_a_source_inside_its_interval_is_not_due() -> None:
    now = datetime.now(UTC)
    source = make_source(
        crawl_interval_seconds=3600, last_crawl_at=now - timedelta(minutes=10)
    )
    assert due_sources([source], now=now) == []


def test_a_source_past_its_interval_is_due() -> None:
    now = datetime.now(UTC)
    source = make_source(
        crawl_interval_seconds=900, last_crawl_at=now - timedelta(minutes=20)
    )
    assert due_sources([source], now=now) == [source]
