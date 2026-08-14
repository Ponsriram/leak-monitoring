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

# A plain, current desktop UA. Nothing clever: .onion services are not behind anti-bot
# vendors, so fingerprint evasion buys nothing and just makes traffic look unusual.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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
    ) -> None:
        ports = socks_ports or [9050]
        self._timeout = timeout
        self._max_retries = max_retries

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
        for attempt in range(1, self._max_retries + 1):
            client = next(self._cycle)
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
            except Exception as exc:  # noqa: BLE001 - any failure is a retry candidate
                log.warning(
                    "fetch failed",
                    url=url,
                    attempt=attempt,
                    max_attempts=self._max_retries,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    # Exponential backoff. The old code slept a flat 10s regardless.
                    await asyncio.sleep(2**attempt)

        log.error("giving up on url", url=url, attempts=self._max_retries)
        return None

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
