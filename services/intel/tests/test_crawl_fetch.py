"""The crawl fetch handler, end to end through the real queue and workers.

Fetching is stubbed (`StubCollector`, the same `fetch_detailed` contract as the real
collectors, whose own behaviour is covered in test_collector_fetch.py). Everything else is
real: the Postgres queue, the claim/lease/retry transitions, the workers, `CrawlFetcher`, and
`ingest_page` — with a storage double that records what extraction was asked to do.

Needs the scratch database; see test_crawl_queue.py for how to set one up.
"""

# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

import pytest
from test_crawl_queue import make_source, pages, pool, queue  # noqa: F401 - fixtures
from test_pipeline_concurrency import listing, make_settings

from intel.collectors import FetchResult, to_text
from intel.crawl.fetch import CrawlFetcher, lease_problems
from intel.crawl.worker import CrawlWorkers
from intel.storage import Storage, UpsertResult, content_hash


def settings(**overrides: object):  # type: ignore[no-untyped-def]
    base = {
        "CRAWL_WORKERS": 2,
        "CRAWL_PER_SOURCE_INFLIGHT": 3,
        "CRAWL_RETRY_BACKOFF": 0,
        "CRAWL_MAX_ATTEMPTS": 3,
        "EXPOSURE_DETECTION": False,
        # These tests are about fetching, retries and hashes with every page claimable at once;
        # the page-1-first gate has its own tests in test_crawl_discovery.py.
        "CRAWL_PAGE1_FIRST": False,
    }
    base.update(overrides)
    return make_settings(**base)


def ok(html: str, *, status: int = 200, ms: int = 42) -> FetchResult:
    return FetchResult(
        "u", "ok", text=html, http_status=status, response_ms=ms, size_bytes=len(html)
    )


def fail(kind: str, status: int | None, error: str, ms: int = 7) -> FetchResult:
    return FetchResult("u", kind, http_status=status, response_ms=ms, error=error)  # type: ignore[arg-type]


class StubCollector:
    """`fetch_detailed` answered from a script: (page_no, call number) -> FetchResult."""

    name = "http"

    def __init__(self, respond: Callable[[int, int], FetchResult], *, latency: float = 0.0) -> None:
        self._respond = respond
        self._latency = latency
        self.calls: list[int] = []
        self.counts: dict[int, int] = {}

    async def fetch_detailed(self, url: str) -> FetchResult:
        page_no = int(url.split("page=")[1])
        self.calls.append(page_no)
        self.counts[page_no] = self.counts.get(page_no, 0) + 1
        if self._latency:
            await asyncio.sleep(self._latency)
        return self._respond(page_no, self.counts[page_no])

    async def aclose(self) -> None:
        return None


class FakeIngestStorage:
    """What `ingest_page` needs, with `raw_pages` semantics: seen means seen *and extracted*."""

    def __init__(self, *, fail_upserts: int = 0) -> None:
        self.saves: list[str] = []
        self.run_ids: list[int | None] = []
        self.upserts = 0
        self.extracted: set[str] = set()
        self._ids: dict[str, int] = {}
        self._fail_upserts = fail_upserts

    async def save_page(self, *, source_id, crawl_run_id, url, page_no, text):  # type: ignore[no-untyped-def]
        digest = content_hash(text)
        self.saves.append(digest)
        self.run_ids.append(crawl_run_id)
        page_id = self._ids.setdefault(digest, len(self._ids) + 1)
        return page_id, digest not in self.extracted

    async def upsert_leaks(self, leaks, *, source_id):  # type: ignore[no-untyped-def]
        self.upserts += 1
        if self._fail_upserts > 0:
            self._fail_upserts -= 1
            raise RuntimeError("extraction blew up")
        return UpsertResult(inserted=2)

    async def mark_extracted(self, page_id: int) -> None:
        for digest, pid in self._ids.items():
            if pid == page_id:
                self.extracted.add(digest)

    async def known_onion_hosts(self) -> set[str]:
        return set()

    async def previous_text(self, *, source_id, url, exclude_sha256):  # type: ignore[no-untyped-def]
        return None

    async def enrich_leak(self, leak, *, names, source_id, detail_url):  # type: ignore[no-untyped-def]
        return None


async def crawl(
    pool,
    queue,
    source: int,
    n: int,
    collector: StubCollector,
    *,
    storage=None,
    cfg=None,  # type: ignore[no-untyped-def]
):
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, source, pages(source, n))
    storage = storage or FakeIngestStorage()
    cfg = cfg or settings()
    fetcher = CrawlFetcher(storage, cfg, collectors={"http": collector})  # type: ignore[arg-type]
    stats = await CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle)
    return stats, storage, cycle


async def row(pool, page_no: int = 1):  # type: ignore[no-untyped-def]
    return await pool.fetchrow(
        "select *, status::text as st from crawl_urls where page_no = $1 order by id desc limit 1",
        page_no,
    )


# ---------------------------------------------------------------- success


async def test_a_successful_page_records_everything(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    html = listing(1)
    stats, storage, _ = await crawl(
        pool, queue, src, 1, StubCollector(lambda p, c: ok(html, ms=137))
    )

    r = await row(pool)
    assert stats.ok == 1
    assert r["st"] == "succeeded"
    assert r["result"] == "new"
    assert r["http_status"] == 200
    assert r["response_ms"] == 137
    assert r["content_sha256"] == content_hash(to_text(html))
    assert r["attempt"] == 1
    assert r["failure_count"] == 0
    assert r["last_error"] is None
    assert r["last_crawled_at"] is not None
    assert r["leaks_found"] == 2
    assert (r["leased_by"], r["leased_until"]) == (None, None)
    # Not due again until the source's own probe interval (900s) has passed.
    due_in = await pool.fetchval("select extract(epoch from next_crawl_at - now()) from crawl_urls")
    assert 800 < due_in <= 900
    assert storage.upserts == 1


# ---------------------------------------------------------------- permanent failures


@pytest.mark.parametrize("status", [404, 410, 403])
async def test_a_refusal_fails_once_and_is_never_retried(pool, queue, status) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    collector = StubCollector(lambda p, c: fail("permanent", status, f"HTTP {status}"))
    stats, storage, _ = await crawl(pool, queue, src, 1, collector)

    r = await row(pool)
    assert collector.calls == [1]
    assert (stats.failed, stats.retried) == (1, 0)
    assert r["st"] == "failed"
    assert r["http_status"] == status
    assert r["last_error"] == f"HTTP {status}"
    assert r["attempt"] == 1 and r["failure_count"] == 1
    assert storage.saves == []  # nothing reached ingestion


async def test_a_gate_page_is_a_permanent_failure(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    gate = "<html><body>Please complete the CAPTCHA to continue</body></html>"
    collector = StubCollector(lambda p, c: ok(gate))
    await crawl(pool, queue, src, 1, collector)

    r = await row(pool)
    assert r["st"] == "failed" and r["result"] == "gate"
    assert "captcha" in r["last_error"].lower() or "human check" in r["last_error"]
    assert collector.calls == [1]


async def test_an_empty_page_one_is_a_permanent_failure(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    empty = "<html><body></body></html>"
    collector = StubCollector(lambda p, c: ok(empty))
    # One worker, so the pages behind page 1 are still queued when page 1 resolves.
    await crawl(pool, queue, src, 3, collector, cfg=settings(CRAWL_WORKERS=1))

    first = await row(pool, 1)
    assert (first["st"], first["result"]) == ("failed", "empty")
    assert collector.calls == [1]


async def test_an_empty_later_page_is_not_a_failure_and_is_never_ingested(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    empty = "<html><body></body></html>"
    collector = StubCollector(lambda p, c: ok(empty) if p == 2 else ok(listing(p)))
    _, storage, _ = await crawl(pool, queue, src, 3, collector, cfg=settings(CRAWL_WORKERS=1))

    second = await row(pool, 2)
    assert (second["st"], second["result"]) == ("succeeded", "empty")
    assert second["content_sha256"] is None
    assert len(storage.saves) == 1  # page 1 only; page 3 was past the end and never fetched


async def test_an_oversized_or_non_page_response_fails_permanently(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    collector = StubCollector(lambda p, c: fail("permanent", 200, "response exceeds 5242880 bytes"))
    await crawl(pool, queue, src, 1, collector)

    r = await row(pool)
    assert r["st"] == "failed" and collector.calls == [1]
    assert r["http_status"] == 200  # the server did answer; the body is what was refused


# ---------------------------------------------------------------- transient failures and retries


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, "HTTP 429"),
        (500, "HTTP 500"),
        (503, "HTTP 503"),
        (None, "timed out after 60s"),
        (None, "ProxyError: Proxy Server could not connect: TTL expired"),
    ],
)
async def test_a_transient_failure_is_retried_and_then_succeeds(pool, queue, status, error) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    collector = StubCollector(
        lambda p, c: fail("transient", status, error) if c == 1 else ok(listing(p))
    )
    stats, _, _ = await crawl(pool, queue, src, 1, collector)

    r = await row(pool)
    assert collector.calls == [1, 1]
    assert (stats.retried, stats.ok) == (1, 1)
    assert r["st"] == "succeeded"
    assert r["attempt"] == 2
    assert r["failure_count"] == 0  # reset by the success
    assert r["last_error"] is None


async def test_attempts_are_capped_at_three(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    collector = StubCollector(lambda p, c: fail("transient", 503, "HTTP 503", ms=11))
    stats, storage, _ = await crawl(pool, queue, src, 1, collector)

    r = await row(pool)
    assert collector.calls == [1, 1, 1]
    assert (stats.retried, stats.failed) == (2, 1)
    assert r["st"] == "failed"
    assert r["attempt"] == 3
    assert r["failure_count"] == 3
    assert r["last_error"] == "HTTP 503"
    assert (r["http_status"], r["response_ms"]) == (503, 11)
    assert storage.saves == []


async def test_a_failed_attempt_keeps_the_last_known_status_and_hash(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """A timeout has no HTTP status; it must not wipe the one the last good fetch had."""
    src = await make_source(pool)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    await pool.execute(
        "update crawl_urls set content_sha256 = $1, http_status = 200, response_ms = 90, "
        "result = 'new'",
        "d" * 64,
    )
    cfg = settings(CRAWL_MAX_ATTEMPTS=1)
    collector = StubCollector(
        lambda p, c: FetchResult("u", "transient", error="timed out after 60s")
    )
    fetcher = CrawlFetcher(FakeIngestStorage(), cfg, collectors={"http": collector})  # type: ignore[arg-type]
    await CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle)

    r = await row(pool)
    assert r["st"] == "failed"
    assert r["http_status"] == 200 and r["result"] == "new"
    assert r["content_sha256"] == "d" * 64
    assert r["last_error"] == "timed out after 60s"


async def test_a_retry_does_not_hold_the_worker_slot(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    collector = StubCollector(
        lambda p, c: fail("transient", 503, "HTTP 503") if (p == 1 and c == 1) else ok(listing(p))
    )
    # One worker, and page 1 backs off for ~1s. If that worker slept through the backoff,
    # pages 2-4 could not finish ahead of page 1's second attempt.
    cfg = settings(CRAWL_WORKERS=1, CRAWL_RETRY_BACKOFF=1)
    stats, _, _ = await crawl(pool, queue, src, 4, collector, cfg=cfg)

    assert collector.calls == [1, 2, 3, 4, 1]
    assert (stats.retried, stats.ok) == (1, 4)


# ---------------------------------------------------------------- hash comparison


async def test_an_unchanged_page_skips_extraction_and_keeps_its_hash(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    html = listing(1)
    digest = content_hash(to_text(html))
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    await pool.execute(
        "update crawl_urls set content_sha256 = $1, result = 'new', http_status = 200", digest
    )

    storage = FakeIngestStorage()
    cfg = settings()
    fetcher = CrawlFetcher(
        storage, cfg, collectors={"http": StubCollector(lambda p, c: ok(html, ms=55))}
    )  # type: ignore[arg-type]
    stats = await CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle)

    r = await row(pool)
    assert stats.ok == 1
    assert (storage.saves, storage.upserts) == ([], 0)  # extraction never ran
    assert r["st"] == "succeeded" and r["result"] == "unchanged"
    assert r["content_sha256"] == digest
    assert r["response_ms"] == 55
    assert r["last_crawled_at"] is not None
    assert r["leaks_found"] == 0


async def test_a_changed_page_is_ingested_and_the_new_hash_is_stored(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    old, html = "a" * 64, listing(1)
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    await pool.execute("update crawl_urls set content_sha256 = $1", old)

    storage = FakeIngestStorage()
    cfg = settings()
    fetcher = CrawlFetcher(storage, cfg, collectors={"http": StubCollector(lambda p, c: ok(html))})  # type: ignore[arg-type]
    await CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle)

    r = await row(pool)
    assert storage.upserts == 1
    assert r["result"] == "changed"
    assert r["content_sha256"] == content_hash(to_text(html)) != old


async def test_failed_ingestion_never_replaces_the_previous_hash(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    old = "b" * 64
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    await pool.execute("update crawl_urls set content_sha256 = $1", old)

    storage = FakeIngestStorage(fail_upserts=99)  # extraction fails every time
    cfg = settings()
    fetcher = CrawlFetcher(
        storage, cfg, collectors={"http": StubCollector(lambda p, c: ok(listing(1)))}
    )  # type: ignore[arg-type]
    stats = await CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle)

    r = await row(pool)
    assert (stats.retried, stats.failed) == (2, 1)
    assert r["st"] == "failed"
    assert r["content_sha256"] == old
    assert "ingest failed" in r["last_error"]
    assert storage.upserts == 3


async def test_a_page_whose_extraction_failed_is_extracted_on_the_retry(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """The page row is written before extraction. The retry must not mistake it for done."""
    src = await make_source(pool)
    storage = FakeIngestStorage(fail_upserts=1)  # fails once, then works
    stats, _, _ = await crawl(
        pool, queue, src, 1, StubCollector(lambda p, c: ok(listing(1))), storage=storage
    )

    r = await row(pool)
    assert (stats.retried, stats.ok) == (1, 1)
    assert storage.upserts == 2  # extraction really ran again
    assert r["st"] == "succeeded" and r["result"] == "new"
    assert r["content_sha256"] == content_hash(to_text(listing(1)))


async def test_the_real_save_page_does_not_treat_an_unextracted_page_as_seen(pool) -> None:  # type: ignore[no-untyped-def]
    """The SQL half of the guarantee above, against the real `raw_pages`."""
    from test_crawl_queue import TEST_DSN

    src = await make_source(pool)
    storage = await Storage.connect(TEST_DSN, max_size=2)  # type: ignore[arg-type]
    try:
        text = f"Some listing text long enough to be real {uuid.uuid4()}"
        kw = dict(source_id=src, crawl_run_id=None, url="http://x/", page_no=1, text=text)

        page_id, changed = await storage.save_page(**kw)
        assert changed
        # Stored but extraction never finished: a second look must say "still to do".
        again_id, changed = await storage.save_page(**kw)
        assert (again_id, changed) == (page_id, True)

        await storage.mark_extracted(page_id)
        _, changed = await storage.save_page(**kw)
        assert not changed
        assert await pool.fetchval("select count(*) from raw_pages where source_id = $1", src) == 1
    finally:
        await storage.close()


# ---------------------------------------------------------------- leases


async def test_the_lease_outlasts_the_handler_cutoff(pool, queue) -> None:  # type: ignore[no-untyped-def]
    """A fetch that takes 80% of the lease finishes inside it, is accepted, and is not reaped."""
    src = await make_source(pool)
    cfg = settings(CRAWL_LEASE_SECONDS=2, CRAWL_TIMEOUT=1)  # lease 2s, cutoff 1.8s
    collector = StubCollector(lambda p, c: ok(listing(p)), latency=1.5)
    stats, _, _ = await crawl(pool, queue, src, 1, collector, cfg=cfg)

    assert (stats.ok, stats.lost_lease, stats.reaped, stats.retried) == (1, 0, 0, 0)
    assert (await row(pool))["st"] == "succeeded"


async def test_a_handler_past_the_cutoff_is_released_before_the_lease_expires(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)
    cfg = settings(CRAWL_LEASE_SECONDS=2, CRAWL_TIMEOUT=1)
    collector = StubCollector(lambda p, c: ok(listing(p)), latency=0.0)
    slow_first = {"n": 0}
    original = collector.fetch_detailed

    async def slow(url: str) -> FetchResult:
        slow_first["n"] += 1
        if slow_first["n"] == 1:
            await asyncio.sleep(10)
        return await original(url)

    collector.fetch_detailed = slow  # type: ignore[method-assign]
    stats, _, _ = await crawl(pool, queue, src, 1, collector, cfg=cfg)

    # Cut off by the worker itself (retry), not recovered by the reaper after the lease went.
    assert (stats.retried, stats.ok, stats.reaped, stats.lost_lease) == (1, 1, 0, 0)


async def test_a_late_result_from_a_worker_that_lost_its_lease_is_rejected(pool, queue) -> None:  # type: ignore[no-untyped-def]
    src = await make_source(pool)

    async def thief_takes_the_url() -> None:
        await pool.execute(
            "update crawl_urls set leased_by = 'thief', leased_until = now() + interval '60 s'"
        )

    class Stalled(StubCollector):
        async def fetch_detailed(self, url: str) -> FetchResult:
            await thief_takes_the_url()  # the lease is lost while this worker is mid-fetch
            return await super().fetch_detailed(url)

    stats, storage, _ = await crawl_without_waiting(
        pool, queue, src, Stalled(lambda p, c: ok(listing(p)))
    )

    r = await row(pool)
    assert stats.lost_lease == 1 and stats.ok == 0
    # The new holder's claim is untouched by the late worker.
    assert (r["st"], r["leased_by"]) == ("running", "thief")
    # ...and the late worker wrote nothing at all.
    assert (r["result"], r["http_status"], r["content_sha256"], r["last_crawled_at"]) == (
        None,
        None,
        None,
        None,
    )


async def crawl_without_waiting(pool, queue, src, collector):  # type: ignore[no-untyped-def]
    """Run one worker pass and stop: the stolen URL would otherwise keep the cycle open."""
    cycle = await queue.start_cycle("cli")
    await queue.enqueue(cycle, src, pages(src, 1))
    storage = FakeIngestStorage()
    cfg = settings(CRAWL_WORKERS=1)
    fetcher = CrawlFetcher(storage, cfg, collectors={"http": collector})  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(
        CrawlWorkers(queue, fetcher, cfg, poll_seconds=0.05).run(cycle, stop=stop)
    )
    for _ in range(100):
        await asyncio.sleep(0.05)
        if collector.calls:
            await asyncio.sleep(0.3)
            break
    stop.set()
    stats = await task
    return stats, storage, cycle


def test_the_default_lease_is_safe_for_the_default_timeout() -> None:
    s = make_settings()
    assert s.request_timeout_seconds == 60
    assert s.lease_seconds * 0.9 > s.request_timeout_seconds + 20  # even the browser's worst case
    assert lease_problems(s) == []


def test_an_unsafe_lease_is_reported() -> None:
    problems = lease_problems(make_settings(CRAWL_LEASE_SECONDS=50))
    assert problems and "CRAWL_TIMEOUT" in problems[0]
