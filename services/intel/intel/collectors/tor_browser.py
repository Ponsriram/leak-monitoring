"""Browser collector for JavaScript-rendered leak sites.

Requires the `browser` extra:

    uv sync --extra browser
    playwright install firefox

Two things differ from the old Selenium collector, and both are significant:

1. **One browser, reused.** The old `scrape_with_selenium()` called `init_selenium()`
   internally, so a fresh headless Firefox process was launched and torn down for *every
   single page request*. Here the browser and context are created once and shared.

2. **Wait on the network, not the clock.** The old code slept a hardcoded 20 seconds per
   page hoping the JavaScript had finished. Playwright waits for the actual load state,
   which is both faster on quick pages and more reliable on slow ones.
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class TorBrowserCollector:
    """Playwright + Firefox over Tor's SOCKS proxy."""

    name = "browser"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: int = 60,
    ) -> None:
        self._proxy = f"socks5://{host}:{socks_port}"
        self._timeout_ms = timeout * 1000
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

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

    async def fetch(self, url: str) -> str | None:
        await self._ensure_started()
        assert self._context is not None

        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            # Give late XHR-driven listings a chance, but don't fail the page if the
            # network never fully quiets — many of these sites keep a socket open.
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=15_000)
            return await page.content()
        except Exception as exc:  # noqa: BLE001
            log.warning("browser fetch failed", url=url, error=str(exc))
            return None
        finally:
            await page.close()

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None
