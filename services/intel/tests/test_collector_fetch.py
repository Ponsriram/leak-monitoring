"""`TorHttpCollector.fetch_detailed`: one attempt, classified, size-capped.

A scripted server (httpx's mock transport) stands in for Tor, so every status, timeout and
body shape below is the real collector code reading a real response stream.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from intel.collectors import TorHttpCollector
from intel.collectors.base import classify_exception, classify_status, is_page_content_type

HTML = "<html><body><h1>Leaks</h1><p>Northwind Logistics - northwind.example</p></body></html>"
URL = "http://abc.onion/list"


class ChunkStream(httpx.AsyncByteStream):
    """A body served in chunks, counting how many were actually pulled by the reader."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk, self._count = chunk, count
        self.served = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(self._count):
            self.served += 1
            yield self._chunk


def collector(handler, **kw) -> TorHttpCollector:  # type: ignore[no-untyped-def]
    kw.setdefault("timeout", 5)
    return TorHttpCollector(transport=httpx.MockTransport(handler), **kw)


def respond(
    status: int = 200, body: str = HTML, content_type: str | None = "text/html"
) -> httpx.Response:
    headers = {"content-type": content_type} if content_type else {}
    return httpx.Response(status, headers=headers, content=body.encode())


# ---------------------------------------------------------------- success


async def test_a_page_comes_back_with_status_size_and_timing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return respond()

    result = await collector(handler).fetch_detailed(URL)

    assert result.kind == "ok"
    assert result.http_status == 200
    assert result.text == HTML
    assert result.size_bytes == len(HTML.encode())
    assert result.content_type == "text/html"
    assert result.response_ms >= 50
    assert result.error is None


async def test_a_response_with_no_content_type_is_still_a_page() -> None:
    result = await collector(lambda r: respond(content_type=None)).fetch_detailed(URL)
    assert result.kind == "ok"


async def test_a_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list":
            return httpx.Response(301, headers={"location": "http://abc.onion/moved"})
        return respond()

    result = await collector(handler).fetch_detailed(URL)
    assert (result.kind, result.http_status) == ("ok", 200)


# ---------------------------------------------------------------- status classification


@pytest.mark.parametrize("status", [404, 410, 403, 401, 400])
async def test_a_refusal_is_permanent(status: int) -> None:
    result = await collector(lambda r: respond(status, "no")).fetch_detailed(URL)
    assert (result.kind, result.http_status, result.error) == (
        "permanent",
        status,
        f"HTTP {status}",
    )
    assert result.text is None


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
async def test_a_server_that_failed_to_answer_is_transient(status: int) -> None:
    result = await collector(lambda r: respond(status, "busy")).fetch_detailed(URL)
    assert (result.kind, result.http_status) == ("transient", status)


def test_status_classification_table() -> None:
    assert [classify_status(s) for s in (200, 204, 301, 302)] == ["ok"] * 4
    assert [classify_status(s) for s in (400, 401, 403, 404, 410, 451)] == ["permanent"] * 6
    assert [classify_status(s) for s in (408, 425, 429, 500, 502, 503, 504)] == ["transient"] * 7


# ---------------------------------------------------------------- transport failures


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ProxyError("Proxy Server could not connect: TTL expired"),
    ],
)
async def test_tor_and_network_failures_are_transient(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result = await collector(handler).fetch_detailed(URL)
    assert result.kind == "transient"
    assert type(exc).__name__ in (result.error or "")
    assert result.http_status is None


@pytest.mark.parametrize("url", ["ftp://abc.onion/file", "not a url", "http://"])
async def test_a_url_that_can_never_work_is_permanent(url: str) -> None:
    result = await collector(lambda r: respond()).fetch_detailed(url)
    assert result.kind == "permanent"


async def test_an_unrecognised_exception_is_permanent_not_retried_forever() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("a bug, not a network")

    result = await collector(handler).fetch_detailed(URL)
    assert result.kind == "permanent"


def test_exception_classification_table() -> None:
    assert classify_exception(httpx.ProxyError("TTL expired")) == "transient"
    assert classify_exception(httpx.ReadTimeout("x")) == "transient"
    assert classify_exception(ConnectionResetError()) == "transient"
    assert classify_exception(TimeoutError()) == "transient"
    assert classify_exception(httpx.UnsupportedProtocol("x")) == "permanent"
    assert classify_exception(httpx.TooManyRedirects("x")) == "permanent"
    assert classify_exception(ValueError("x")) == "permanent"


async def test_a_server_that_drips_bytes_still_hits_the_total_deadline() -> None:
    """httpx's timeout is per operation, so a slow drip would never trip it on its own."""

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Drip())

    result = await collector(handler, timeout=0.3).fetch_detailed(URL)
    assert result.kind == "transient"
    assert "timed out" in (result.error or "")
    assert 250 <= result.response_ms < 1500


# ---------------------------------------------------------------- content type


@pytest.mark.parametrize(
    "content_type", ["application/zip", "application/octet-stream", "image/png", "application/pdf"]
)
async def test_a_file_is_refused_without_reading_it(content_type: str) -> None:
    stream = ChunkStream(b"x" * 1024, 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, stream=stream)

    result = await collector(handler).fetch_detailed(URL)

    assert result.kind == "permanent"
    assert "unsupported content type" in (result.error or "")
    assert stream.served == 0  # the body was never pulled
    assert result.text is None


def test_content_type_rules() -> None:
    assert is_page_content_type("text/html; charset=utf-8")
    assert is_page_content_type("application/xhtml+xml")
    assert is_page_content_type("text/plain")
    assert is_page_content_type(None)
    assert not is_page_content_type("application/json")
    assert not is_page_content_type("video/mp4")


# ---------------------------------------------------------------- size limit


async def test_an_oversized_body_is_abandoned_mid_stream() -> None:
    limit = 10_000
    stream = ChunkStream(b"x" * 1000, 100_000)  # 100 MB on offer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    result = await collector(handler, max_bytes=limit).fetch_detailed(URL)

    assert result.kind == "permanent"
    assert "exceeds" in (result.error or "")
    assert result.text is None
    # Reading stopped almost at once: nowhere near the 100,000 chunks that were available.
    assert stream.served <= limit // 1000 + 2


async def test_a_declared_oversize_is_refused_before_any_body_is_read() -> None:
    stream = ChunkStream(b"x" * 1000, 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "99999999"}, stream=stream
        )

    result = await collector(handler, max_bytes=1000).fetch_detailed(URL)

    assert result.kind == "permanent"
    assert stream.served == 0


async def test_a_body_exactly_at_the_limit_is_accepted() -> None:
    body = "x" * 2048
    result = await collector(lambda r: respond(body=body), max_bytes=2048).fetch_detailed(URL)
    assert result.kind == "ok" and result.size_bytes == 2048


# ---------------------------------------------------------------- one attempt


async def test_fetch_detailed_makes_exactly_one_attempt_and_never_sleeps() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return respond(503, "busy")

    # A backoff of 999s: if the collector slept on a failure this test would never return.
    c = collector(handler, max_retries=4, backoff_seconds=999, backoff_cap_seconds=999)
    started = asyncio.get_running_loop().time()
    result = await c.fetch_detailed(URL)

    assert calls == 1
    assert result.kind == "transient"
    assert asyncio.get_running_loop().time() - started < 1


# ---------------------------------------------------------------- the legacy fetch() wrapper


async def test_legacy_fetch_still_returns_text() -> None:
    assert await collector(lambda r: respond()).fetch(URL) == HTML


async def test_legacy_fetch_still_retries_transient_failures() -> None:
    statuses = iter([503, 503, 200])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return respond(next(statuses))

    c = collector(handler, max_retries=4, backoff_seconds=0, backoff_cap_seconds=0)
    assert await c.fetch(URL) == HTML
    assert calls == 3


async def test_legacy_fetch_does_not_retry_a_refusal() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return respond(403, "no")

    c = collector(handler, max_retries=4, backoff_seconds=0, backoff_cap_seconds=0)
    assert await c.fetch(URL) is None
    assert calls == 1
    assert c.last_error == "HTTP 403"


async def test_legacy_fetch_gives_up_after_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return respond(500, "boom")

    c = collector(handler, max_retries=3, backoff_seconds=0, backoff_cap_seconds=0)
    assert await c.fetch(URL) is None
    assert calls == 3
    assert c.last_error == "HTTP 500"


# ---------------------------------------------------------------- browser collector classification
#
# Playwright and Firefox are not exercised by the suite, so the one piece of the browser
# collector's new contract that is pure logic - how its errors are classified - is pinned here.


def _playwright_error(name: str, message: str) -> Exception:
    return type(name, (Exception,), {})(message)


@pytest.mark.parametrize(
    ("name", "message", "expected"),
    [
        ("TimeoutError", "Page.goto: Timeout 60000ms exceeded.", "transient"),
        ("Error", "Page.goto: NS_ERROR_PROXY_CONNECTION_REFUSED", "transient"),
        ("Error", "Page.goto: net::ERR_CONNECTION_RESET", "transient"),
        ("Error", "Page.goto: NS_ERROR_NET_TIMEOUT", "transient"),
        ("Error", "Target page, context or browser has been closed", "transient"),
        ("Error", "Page.goto: Protocol error: invalid URL", "permanent"),
        ("ValueError", "something unrelated", "permanent"),
    ],
)
def test_browser_errors_are_classified_without_guessing(
    name: str, message: str, expected: str
) -> None:
    from intel.collectors.tor_browser import _classify_playwright

    assert _classify_playwright(_playwright_error(name, message)) == expected
