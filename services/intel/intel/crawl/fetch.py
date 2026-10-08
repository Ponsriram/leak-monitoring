"""The crawl worker's handler: fetch one URL, judge it, and hand a changed page to ingestion.

    claimed URL
        -> ONE fetch attempt (no retries, no sleeping)
        -> content checks: gate page, empty page
        -> SHA-256 of the cleaned text, compared with the URL's stored hash
             same  -> unchanged: extraction does not run
             new   -> `ingest_page` (existing extraction, leaks, exposures), and only if that
                      succeeds is the new hash handed back to be stored

The handler never retries and never decides *whether* to retry. It reports `ok`, `transient`
or `permanent`, and the queue turns that into a state change. A worker slot is therefore only
ever occupied by a fetch in flight, never by a backoff.

What is hashed is the cleaned text, not the raw response, for the same reason `raw_pages`
hashes it: `to_text` strips countdown timers and other volatile markup, so a page whose only
change is a ticking clock hashes the same each time and is not re-extracted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import structlog

from ..collectors import extract_links, get_collector, to_text
from ..config import Settings
from ..extract import get_extractor
from ..pipeline import (
    MIN_PAGE_TEXT_CHARS,
    SourceIdent,
    _record_mirrors,
    gate_kind,
    ingest_page,
)
from ..storage import Storage, content_hash
from .frontier import normalize_crawl_url
from .queue import ClaimedUrl, CrawlQueue, NewUrl
from .worker import Outcome

log = structlog.get_logger(__name__)

# How many links to read off a page before the queue applies its own limits. The queue —
# not this — decides how many are taken; this only bounds the work on a page that is all links.
_CANDIDATE_CAP = 200

# Slack the browser collector adds on top of the request timeout (network-idle wait, reading
# the DOM back). Mirrors `tor_browser._SETTLE_SECONDS`.
_BROWSER_SETTLE_SECONDS = 20


def lease_problems(settings: Settings) -> list[str]:
    """Why this configuration could let a lease expire while a fetch is still running.

    The worker cuts a handler off at 90% of the lease (see `CrawlWorkers._process`), and a
    fetch must finish — or time out on its own — before that. If the lease is shorter than the
    longest fetch the reaper would hand a URL to a second worker mid-flight. Returned rather
    than raised so a misconfiguration is loud in the log without refusing to start.
    """
    cutoff = settings.lease_seconds * 0.9
    problems = []
    if cutoff <= settings.request_timeout_seconds:
        problems.append(
            f"handler cutoff {cutoff:g}s <= CRAWL_TIMEOUT {settings.request_timeout_seconds}s: "
            "every slow fetch will be cut off before it can time out on its own"
        )
    browser_worst = settings.request_timeout_seconds + _BROWSER_SETTLE_SECONDS
    if settings.browser_workers > 0 and cutoff <= browser_worst:
        problems.append(
            f"handler cutoff {cutoff:g}s <= the longest browser fetch ({browser_worst}s)"
        )
    return problems


class CrawlFetcher:
    """Callable handler for `CrawlWorkers`."""

    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        *,
        collectors: Mapping[str, Any] | None = None,
        queue: CrawlQueue | None = None,
        run_ids: Mapping[int, int] | None = None,
    ) -> None:
        self._storage = storage
        self._settings = settings
        # Injected in tests; built lazily otherwise, one per collector kind and shared by every
        # worker in this process (an httpx client is a connection pool, a browser is ~500MB).
        self._collectors: dict[str, Any] = dict(collectors or {})
        self._collector_lock = asyncio.Lock()
        # With a queue, each page is filed under the `crawl_runs` row of its source in its
        # cycle (created on first use, once, however many workers get there together).
        # `run_ids` is a fixed source_id -> run mapping for callers without a queue.
        self._queue = queue
        self._run_ids = run_ids or {}
        self._run_cache: dict[tuple[int, int], int] = {}
        self._extractor = get_extractor(settings.extractor)
        self._known_hosts: set[str] | None = None

        for problem in lease_problems(settings):
            log.warning("unsafe lease configuration", problem=problem)

    async def aclose(self) -> None:
        for collector in self._collectors.values():
            await collector.aclose()
        self._collectors.clear()

    async def _collector(self, kind: str) -> Any:
        async with self._collector_lock:
            if kind not in self._collectors:
                s = self._settings
                self._collectors[kind] = get_collector(
                    kind,
                    host=s.tor_host,
                    socks_ports=s.tor_socks_ports,
                    timeout=s.request_timeout_seconds,
                    # Irrelevant to `fetch_detailed`, which makes one attempt, but the
                    # constructor wants them and the legacy `fetch` would use them.
                    max_retries=1,
                    backoff_seconds=s.retry_backoff_seconds,
                    backoff_cap_seconds=s.retry_backoff_cap_seconds,
                    max_bytes=s.max_bytes,
                )
            return self._collectors[kind]

    async def __call__(self, url: ClaimedUrl) -> Outcome:
        collector = await self._collector(url.collector)
        fetched = await collector.fetch_detailed(url.url)

        log.info(
            "fetched",
            source=url.source_slug,
            url_id=url.id,
            attempt=url.attempt,
            kind=fetched.kind,
            status=fetched.http_status,
            ms=fetched.response_ms,
            bytes=fetched.size_bytes,
        )

        if fetched.kind != "ok":
            return Outcome(
                fetched.kind,
                http_status=fetched.http_status,
                response_ms=fetched.response_ms,
                error=fetched.error or fetched.kind,
            )

        base = {"http_status": fetched.http_status, "response_ms": fetched.response_ms}
        # Listing pages keep their tile boundaries (see `to_text`); a followed page is one
        # victim's own page and is read whole.
        text = to_text(
            fetched.text or "",
            segment=url.kind == "listing",
            item_selector=url.item_selector,
        )

        await self._record_mirrors(url, text)

        # Page 1 of a listing is the one that must read as a listing. The same two checks as the
        # source-at-a-time crawler, and for the same reason: a site that answers 200 with an
        # interstitial must not read as a healthy source that merely had nothing new.
        if url.kind == "listing" and (url.page_no or 1) == 1:
            gate = gate_kind(text)
            if gate:
                return Outcome(
                    "permanent",
                    result="gate",
                    error=(
                        f"page 1 is a {gate} page, not a listing — the site gates automated access"
                    ),
                    **base,
                )
            if len(text.strip()) < MIN_PAGE_TEXT_CHARS:
                return Outcome(
                    "permanent",
                    result="empty",
                    error=(
                        f"page 1 returned {len(text.strip())} chars of text — challenge page "
                        "or JS-rendered listing (try collector: browser)"
                    ),
                    **base,
                )
        elif len(text.strip()) < MIN_PAGE_TEXT_CHARS:
            # Past page 1 an empty page is not a failure: it is where the listing ends.
            return Outcome("ok", result="empty", **base)

        digest = content_hash(text)

        if url.content_sha256 == digest:
            # Unchanged since the last successful ingest. Extraction does not run and the
            # stored hash is left exactly as it is (`content_sha256=None` means "do not touch").
            return Outcome("ok", result="unchanged", **base)

        try:
            ingested = await ingest_page(
                storage=self._storage,
                settings=self._settings,
                source=SourceIdent(url.source_id, url.source_slug),
                extractor=self._extractor,
                run_id=await self._run_id(url),
                url=url.url,
                page_no=url.page_no or 1,
                text=text,
                extract_leaks=url.kind == "listing",
            )
        except Exception as exc:  # noqa: BLE001 - reported as a failed attempt, never raised
            # The old hash stays. The page row may already be stored, but it is not marked
            # extracted, so the retry runs extraction again rather than skipping it.
            log.exception("ingest failed", source=url.source_slug, url_id=url.id)
            return Outcome(
                "transient",
                error=f"ingest failed: {type(exc).__name__}: {str(exc)[:200]}",
                **base,
            )

        if not ingested.changed:
            # Identical content was already stored and extracted for this source — by the
            # legacy crawler, or at another URL. Nothing ran; the hash is safe to remember.
            return Outcome("ok", result="unchanged", content_sha256=digest, **base)

        # Links are looked for only now: on a page that was new or changed and went through
        # ingestion. An unchanged page was mined the last time it changed, and mining it again
        # every cycle would only re-find the same links.
        return Outcome(
            "ok",
            result="new" if url.content_sha256 is None else "changed",
            content_sha256=digest,
            leaks_found=ingested.upserted.inserted,
            leaks_updated=ingested.upserted.updated,
            links=self._candidate_links(url, fetched.text or ""),
            **base,
        )

    def _candidate_links(self, url: ClaimedUrl, html: str) -> list[NewUrl]:
        """Links on this page that could become crawl jobs, in page order.

        Cheap pre-filters only (depth, setting, a cycle to belong to) plus `extract_links`,
        which already keeps to the page's own host and drops files and account pages. The
        queue validates every one again against the source before anything is queued.
        """
        s = self._settings
        if not s.follow_links or url.cycle_id is None or url.depth >= s.link_depth:
            return []
        return [
            NewUrl(
                url=link,
                url_normalized=normalize_crawl_url(link),
                kind="link",
            )
            for link in extract_links(html, url.url, limit=_CANDIDATE_CAP)
        ]

    async def _run_id(self, url: ClaimedUrl) -> int | None:
        if self._queue is not None and url.cycle_id is not None:
            key = (url.cycle_id, url.source_id)
            if key not in self._run_cache:
                self._run_cache[key] = await self._queue.ensure_run(*key)
            return self._run_cache[key]
        return self._run_ids.get(url.source_id)

    async def _record_mirrors(self, url: ClaimedUrl, text: str) -> None:
        if not self._settings.discover_mirrors:
            return
        try:
            if self._known_hosts is None:
                self._known_hosts = await self._storage.known_onion_hosts()
            await _record_mirrors(
                text,
                source=SourceIdent(url.source_id, url.source_slug),
                storage=self._storage,
                url=url.url,
                known_hosts=self._known_hosts,
            )
        except Exception:  # noqa: BLE001 - mirror bookkeeping must not fail a page
            log.exception("mirror recording failed", source=url.source_slug)
