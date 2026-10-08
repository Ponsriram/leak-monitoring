"""Browser collector for JavaScript-rendered leak sites.

Requires the `browser` extra:

    uv sync --extra browser
    playwright install firefox

Two design points, both significant:

1. **One browser, reused.** The browser and context are created once and shared, rather
   than launching and tearing down a headless Firefox for every page request.

2. **Wait on the network, not the clock.** Playwright waits for the actual load state
   instead of a fixed sleep, which is both faster on quick pages and more reliable on slow
   ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any
from urllib.parse import urlsplit

import structlog

from .base import (
    CapturedJson,
    FetchResult,
    classify_exception,
    classify_status,
    is_page_content_type,
)

log = structlog.get_logger(__name__)

# Slack on top of the navigation timeout for the network-idle wait and reading the DOM back.
# Together they are the longest one browser fetch may take, which the crawl lease must exceed.
_SETTLE_SECONDS = 20

# How long to wait, after the DOM is read, for JSON bodies still being read off the wire.
_JSON_DRAIN_SECONDS = 5


class _JsonCapture:
    """Keeps the JSON a page loads from its own host while it renders.

    A site that fills its listing — or a victim's details — by XHR/fetch has that data in
    JSON before it is ever in the DOM, and a detail panel that opens on click never reaches
    the DOM at all. Only responses from the page's own host with a JSON content type are kept,
    and only up to `max_bytes` in total: the same ceiling as the page itself.
    """

    def __init__(self, page_url: str, max_bytes: int | None) -> None:
        self._host = urlsplit(page_url).netloc.lower()
        self._max_bytes = max_bytes
        self._used = 0
        self._reads: list[asyncio.Task[None]] = []
        self.captured: list[CapturedJson] = []

    def on_response(self, response: Any) -> None:
        """`page.on("response")` handler. Synchronous: bodies are read in tasks."""
        if urlsplit(response.url).netloc.lower() != self._host:
            return
        mime = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not mime.endswith("json"):
            return
        self._reads.append(asyncio.ensure_future(self._read(response)))

    async def _read(self, response: Any) -> None:
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001 - a body that cannot be read is simply not kept
            return
        size = len(body.encode("utf-8"))
        if self._max_bytes is not None and self._used + size > self._max_bytes:
            log.info("captured JSON over the byte limit, dropped", url=response.url, bytes=size)
            return
        self._used += size
        self.captured.append(CapturedJson(response.url, body))

    async def drain(self) -> list[CapturedJson] | None:
        """Wait briefly for bodies still being read; None if there was nothing to keep."""
        if self._reads:
            _, late = await asyncio.wait(self._reads, timeout=_JSON_DRAIN_SECONDS)
            for task in late:
                task.cancel()
        return self.captured or None


# Network and proxy failures as Firefox and Playwright word them: the circuit would not build,
# the service did not answer, the connection dropped. None of them is the request's fault.
_NETWORK_ERROR = re.compile(
    r"NS_ERROR_(?:NET|PROXY|CONNECTION|UNKNOWN_HOST|UNKNOWN_PROXY_HOST|OFFLINE)|net::ERR_|"
    r"timeout|timed out|TTL expired|connection (?:reset|refused|closed)",
    re.IGNORECASE,
)


def _classify_playwright(exc: BaseException) -> str:
    """Playwright exception -> transient / permanent, without guessing."""
    name = type(exc).__name__
    message = str(exc)
    if name == "TimeoutError":  # Playwright's own, not the builtin: a slow navigation
        return "transient"
    if name == "Error":
        # A closed page, context or browser means this process lost its browser; the next
        # attempt starts a fresh one. A network error is the circuit or the service.
        if _NETWORK_ERROR.search(message) or "has been closed" in message:
            return "transient"
        return "permanent"
    return classify_exception(exc)


class TorBrowserCollector:
    """Playwright + Firefox over Tor's SOCKS proxy."""

    name = "browser"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: float = 60,
        max_bytes: int | None = None,
    ) -> None:
        self._proxy = f"socks5://{host}:{socks_port}"
        self._timeout = timeout
        self._timeout_ms = int(timeout * 1000)
        self._max_bytes = max_bytes
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.last_error: str | None = None

    async def _ensure_started(self) -> None:
        if self._context is not None:
            return

        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415 - optional extra
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "TorBrowserCollector needs the 'browser' extra:\n"
                "    uv sync --extra browser\n"
                "    playwright install firefox"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.firefox.launch(
            headless=True,
            proxy={"server": self._proxy},
        )
        # Remote DNS matters: resolving .onion locally would both fail and leak the lookup.
        self._context = await self._browser.new_context(
            ignore_https_errors=True,
            java_script_enabled=True,
        )
        self._context.set_default_timeout(self._timeout_ms)
        log.info("browser started", proxy=self._proxy)

    async def fetch_detailed(self, url: str) -> FetchResult:
        """One attempt, no retries, no sleeping. Never raises.

        The same contract as `TorHttpCollector.fetch_detailed`, so the crawl worker does not
        care which collector a source uses. A browser has no streaming body to cut short, so
        the size limit is applied to the rendered document instead.
        """
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            async with asyncio.timeout(self._timeout + _SETTLE_SECONDS):
                return await self._attempt(url, started)
        except TimeoutError:
            return FetchResult(
                url,
                "transient",
                error=f"timed out after {self._timeout + _SETTLE_SECONDS:g}s",
                response_ms=elapsed_ms(),
            )
        except Exception as exc:  # noqa: BLE001 - classified, never raised
            kind = _classify_playwright(exc)
            return FetchResult(
                url,
                kind,
                error=f"{type(exc).__name__}: {exc}"[:300],
                response_ms=elapsed_ms(),
            )

    async def _attempt(self, url: str, started: float) -> FetchResult:
        await self._ensure_started()
        assert self._context is not None

        page = await self._context.new_page()
        # Registered before navigating, so the requests the page makes while it loads are seen.
        capture = _JsonCapture(url, self._max_bytes)
        page.on("response", capture.on_response)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            status = response.status if response is not None else None
            content_type = response.headers.get("content-type") if response is not None else None

            def result(kind, **kw):  # type: ignore[no-untyped-def]
                return FetchResult(
                    url,
                    kind,
                    http_status=status,
                    content_type=content_type,
                    response_ms=int((time.monotonic() - started) * 1000),
                    **kw,
                )

            if status is not None:
                kind = classify_status(status)
                if kind != "ok":
                    return result(kind, error=f"HTTP {status}")
            if not is_page_content_type(content_type):
                return result("permanent", error=f"unsupported content type {content_type!r}")

            # Give late XHR-driven listings a chance, but don't fail the page if the
            # network never fully quiets - many of these sites keep a socket open.
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=15_000)

            html = await page.content()
            size = len(html.encode("utf-8"))
            if self._max_bytes and size > self._max_bytes:
                return result(
                    "permanent", size_bytes=size, error=f"page exceeds {self._max_bytes} bytes"
                )
            return result(
                "ok", text=html, size_bytes=size, json_responses=await capture.drain()
            )
        finally:
            await page.close()

    async def fetch(self, url: str) -> str | None:
        """Page HTML, or None. A single attempt, as it always was."""
        result = await self.fetch_detailed(url)
        self.last_error = result.error
        if result.kind != "ok":
            log.warning("browser fetch failed", url=url, error=result.error)
            return None
        return result.text

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None
