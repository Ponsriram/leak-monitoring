"""Async HTTP over Tor.

Replaces `requests_tor`. The change that matters is not the library but the concurrency
model: this is async, so the pipeline can crawl several sources at once. The old crawler was
strictly sequential with a 20-second sleep between every page — 83 sources took many hours
per cycle and could not be sped up without rewriting it.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator

import httpx
import structlog

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

# Status codes where trying again can plausibly succeed. Everything else in 4xx is the
# server saying no on purpose — retrying a 403 three times just spends six minutes to be
# refused three times, which is exactly what the akira source was doing every run.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TorHttpCollector:
    """Fetches pages through one or more Tor SOCKS ports."""

    name = "http"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        socks_ports: list[int] | None = None,
        timeout: int = 60,
        max_retries: int = 3,
        backoff_seconds: int = 15,
        backoff_cap_seconds: int = 120,
    ) -> None:
        ports = socks_ports or [9050]
        self._timeout = timeout
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
                proxy=f"socks5://{host}:{port}",
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

    async def fetch(self, url: str) -> str | None:
        self.last_error = None

        for attempt in range(1, self._max_retries + 1):
            client = next(self._cycle)
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.text

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                self.last_error = f"HTTP {status}"
                if status not in _RETRYABLE_STATUS:
                    # Deliberately not retried. A 403 here means the site is serving a
                    # challenge or blocking non-browser clients; the fix is the `browser`
                    # collector for that source, not more attempts.
                    log.warning(
                        "fetch refused, not retrying",
                        url=url,
                        status=status,
                        hint="try collector: browser for this source",
                    )
                    return None
                log.warning(
                    "fetch failed", url=url, attempt=attempt,
                    max_attempts=self._max_retries, error=self.last_error,
                )

            except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
                self.last_error = str(exc)
                log.warning(
                    "fetch failed", url=url, attempt=attempt,
                    max_attempts=self._max_retries, error=self.last_error,
                )

            if attempt < self._max_retries:
                # "Proxy Server could not connect: TTL expired" means Tor could not build a
                # rendezvous circuit in time — routinely transient, and routinely fixed by
                # waiting long enough for a new circuit. The old backoff was 2s/4s/8s, which
                # is far shorter than an onion circuit takes to rebuild, so every retry
                # reused a path that had just failed. These waits are long enough that
                # MaxCircuitDirtiness (180s) can actually rotate the circuit underneath us.
                delay = min(self._backoff * (2 ** (attempt - 1)), self._backoff_cap)
                await asyncio.sleep(delay)

        log.error("giving up on url", url=url, attempts=self._max_retries)
        return None

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
