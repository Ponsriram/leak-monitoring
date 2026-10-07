"""Async HTTP over Tor.

What matters is the concurrency model: this is async, so the pipeline crawls several sources
at once instead of one page at a time.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import structlog

from .base import FetchResult, classify_exception, classify_status, is_page_content_type

log = structlog.get_logger(__name__)

# A plain, current desktop UA. Most .onion services are not behind anti-bot vendors, so
# fingerprint evasion buys nothing — but a few (akira, and anything serving a JS challenge)
# do refuse anything that doesn't look like a browser, which is what the rest of these
# headers are for: they are what Firefox actually sends on a top-level navigation.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}


class TorHttpCollector:
    """Fetches pages through one or more Tor SOCKS ports."""

    name = "http"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        socks_ports: list[int] | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        backoff_seconds: int = 15,
        backoff_cap_seconds: int = 120,
        max_bytes: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        ports = socks_ports or [9050]
        self._timeout = timeout
        # Largest body that will be read. None means unlimited, which is what the legacy
        # source-at-a-time crawler has always done; the URL-queue crawler always sets it.
        self._max_bytes = max_bytes
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._backoff_cap = backoff_cap_seconds

        # The reason the last fetch failed, for the pipeline to record against the source.
        # Without it every failure landed in the database as "could not fetch <url>", which
        # cannot distinguish a dead site from a site that refused us.
        self.last_error: str | None = None

        # One client per port, round-robined. Running several Tor instances on separate
        # ports multiplies the effective circuit-rotation rate; with one port this is
        # simply a single client.
        self._clients = [
            httpx.AsyncClient(
                # `transport` exists for tests. A proxy would take precedence over it, so
                # the two are never given together.
                proxy=None if transport is not None else f"socks5://{host}:{port}",
                transport=transport,
                timeout=httpx.Timeout(timeout),
                headers=_HEADERS,
                follow_redirects=True,
                # .onion certificates are self-signed and the transport is already
                # authenticated by the address itself.
                verify=False,
            )
            for port in ports
        ]
        self._cycle: Iterator[httpx.AsyncClient] = itertools.cycle(self._clients)

    async def fetch_detailed(self, url: str) -> FetchResult:
        """One attempt, no retries, no sleeping. Never raises.

        This is what the URL-queue crawler calls: the decision to try again belongs to the
        queue, which can release the worker slot while a URL waits out its backoff. A
        collector that slept through the backoff itself would hold the slot the whole time.

        The whole attempt - connect, headers, body - runs under one deadline of `timeout`
        seconds. httpx's own timeout is per operation, so a server that sends one byte every
        few seconds would otherwise never time out at all.
        """
        # Defence in depth behind the crawl allowlist: this collector only ever speaks http(s),
        # whatever it is asked for. Refused before a request exists.
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return FetchResult(url, "permanent", error="unsupported URL scheme")

        client = next(self._cycle)
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            async with asyncio.timeout(self._timeout):
                return await self._attempt(client, url, started)
        except TimeoutError:
            return FetchResult(
                url,
                "transient",
                error=f"timed out after {self._timeout:g}s",
                response_ms=elapsed_ms(),
            )
        except Exception as exc:  # noqa: BLE001 - classified, never raised
            return FetchResult(
                url,
                classify_exception(exc),
                error=f"{type(exc).__name__}: {exc}"[:300],
                response_ms=elapsed_ms(),
            )

    async def _attempt(self, client: httpx.AsyncClient, url: str, started: float) -> FetchResult:
        async with client.stream("GET", url) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")

            def result(kind, **kw):  # type: ignore[no-untyped-def]
                return FetchResult(
                    url,
                    kind,
                    http_status=status,
                    content_type=content_type,
                    response_ms=int((time.monotonic() - started) * 1000),
                    **kw,
                )

            kind = classify_status(status)
            if kind != "ok":
                return result(kind, error=f"HTTP {status}")

            # Judged before the body is read: a file download is refused without fetching it.
            if not is_page_content_type(content_type):
                return result("permanent", error=f"unsupported content type {content_type!r}")

            limit = self._max_bytes
            declared = response.headers.get("content-length")
            if limit and declared and declared.isdigit() and int(declared) > limit:
                return result("permanent", error=f"response is {declared} bytes, over {limit}")

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if limit and len(body) > limit:
                    # Stop reading now. Leaving the `async with` closes the connection, so
                    # the rest of an oversized body is never pulled into memory.
                    return result(
                        "permanent",
                        size_bytes=len(body),
                        error=f"response exceeds {limit} bytes",
                    )

            text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
            return result("ok", text=text, size_bytes=len(body))

    async def fetch(self, url: str) -> str | None:
        """Page text, or None. Retries transient failures with backoff.

        The legacy API, kept for the source-at-a-time crawler and the CLI. It is a loop over
        `fetch_detailed`, so both paths classify failures identically; only this one sleeps
        between attempts.
        """
        self.last_error = None

        for attempt in range(1, self._max_retries + 1):
            result = await self.fetch_detailed(url)
            if result.kind == "ok":
                return result.text

            self.last_error = result.error
            if result.kind == "permanent":
                # Deliberately not retried. A 403 means the site is serving a challenge or
                # blocking non-browser clients; the fix is the `browser` collector for that
                # source, not more attempts.
                log.warning(
                    "fetch refused, not retrying",
                    url=url,
                    error=result.error,
                    hint="try collector: browser for this source",
                )
                return None

            log.warning(
                "fetch failed",
                url=url,
                attempt=attempt,
                max_attempts=self._max_retries,
                error=self.last_error,
            )
            if attempt < self._max_retries:
                # "Proxy Server could not connect: TTL expired" means Tor could not build a
                # rendezvous circuit in time - routinely transient, and routinely fixed by
                # waiting long enough for a new circuit. A backoff of a few seconds is far
                # shorter than an onion circuit takes to rebuild, so every retry would reuse
                # a path that had just failed. These waits are long enough that
                # MaxCircuitDirtiness (180s) can actually rotate the circuit underneath us.
                delay = min(self._backoff * (2 ** (attempt - 1)), self._backoff_cap)
                await asyncio.sleep(delay)

        log.error("giving up on url", url=url, attempts=self._max_retries)
        return None

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
