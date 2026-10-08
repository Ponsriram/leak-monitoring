"""fetch → hash → parse → normalize → dedupe → upsert.

The whole point of this module is that it runs unattended: every source on its own
interval, every run safe to repeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import structlog

from .collectors import (
    BLOCK_BREAK,
    RECORD_SEPARATOR,
    CapturedJson,
    DetailPage,
    classify_onion_urls,
    detail_page,
    extract_links,
    get_collector,
    normalize_url,
    onion_host,
    page_url,
    tile_links,
    to_text,
)
from .config import Settings
from .extract import Label, Span, get_extractor, link_block, link_spans
from .extract.json_items import (
    JsonMapping,
    items_text,
    leaks_from_items,
    match_items,
    parse_mapping,
)
from .extract.rules import block_victim_name
from .extract.secrets import find_exposures
from .models import ExtractedLeak
from .scheduling import page_waves, source_time_budget
from .storage import SourceRow, Storage, UpsertResult, content_hash

log = structlog.get_logger(__name__)


# A page shorter than this carries no listings. It is a challenge page, a JS shell, or an
# error, and is recorded as a failed crawl rather than a healthy one.
MIN_PAGE_TEXT_CHARS = 50

# Interstitials that answer 200 with a little text: DDoS access queues, human checks,
# maintenance notices, login walls. Only a *short* page is judged — a real listing that
# happens to link "Login" in its header runs to thousands of characters and is never read
# as a gate. Every phrase below was seen on a monitored source's stored page 1.
_GATE_MAX_CHARS = 600
_GATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("access queue", re.compile(r"placed in a queue|access queue|awaiting forwarding", re.I)),
    (
        "human check",
        re.compile(
            r"captcha|checking (?:that|if) you are (?:a )?human|verify you are human|"
            r"are you (?:a )?human|i'?m not a robot",
            re.I,
        ),
    ),
    ("DDoS protection", re.compile(r"ddos[- ]?(?:protection|guard)|checking your browser", re.I)),
    ("maintenance", re.compile(r"under maintenance|maintenance is (?:currently )?underway", re.I)),
    ("login wall", re.compile(r"^\s*(?:log ?in|sign ?in)\s*$", re.I | re.M)),
)


def gate_kind(text: str) -> str | None:
    """Which kind of interstitial this page is, or None for a page worth extracting."""
    if len(text.strip()) > _GATE_MAX_CHARS:
        return None
    for kind, pattern in _GATES:
        if pattern.search(text):
            return kind
    return None


@dataclass(slots=True)
class SourceResult:
    slug: str
    status: str = "succeeded"
    pages_fetched: int = 0
    pages_changed: int = 0
    bytes_fetched: int = 0
    leaks: UpsertResult = field(default_factory=UpsertResult)
    error: str | None = None
    mirrors_found: int = 0
    switched_to: str | None = None
    # Pages fetched by a wave that turned out to sit past the end of the listing. The price
    # of not knowing where a listing ends until you have asked; watch it to tune
    # CRAWL_PAGE_CONCURRENCY against a source's real depth.
    pages_discarded: int = 0
    # Pages reached by following links out of the listing (already counted in
    # `pages_fetched`), and whether the source's time budget cut the crawl short.
    link_pages: int = 0
    truncated: bool = False


@dataclass(slots=True)
class _Page:
    """A fetch that has come back, before it is known to belong to the listing."""

    page_no: int
    url: str
    html: str | None
    # JSON the browser saw the page load, when the source maps its records from JSON.
    json_responses: list[CapturedJson] | None = None


@dataclass(slots=True)
class RunResult:
    sources: list[SourceResult] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(source.leaks.inserted for source in self.sources)

    @property
    def updated(self) -> int:
        return sum(source.leaks.updated for source in self.sources)

    @property
    def failed(self) -> list[str]:
        return [s.slug for s in self.sources if s.status == "failed"]

    @property
    def new_leak_ids(self) -> list[int]:
        return [id_ for source in self.sources for id_ in source.leaks.new_leak_ids]

    @property
    def discarded(self) -> int:
        """Speculatively fetched pages that turned out to sit past a listing's end."""
        return sum(source.pages_discarded for source in self.sources)


async def crawl_source(
    source: SourceRow,
    *,
    storage: Storage,
    settings: Settings,
    extractor_name: str | None = None,
    fetch_slots: asyncio.Semaphore | None = None,
    depth: str | None = None,
    time_budget: float | None = None,
) -> SourceResult:
    """Crawl one source end to end. Never raises — failures are recorded, not propagated.

    One bad source must not abort the run or lose the pages already fetched.

    Pages are fetched in concurrent, doubling waves (`intel.scheduling.page_waves`) rather
    than one at a time. Reaching the end of a P-page listing costs O(log P) sequential Tor
    round trips instead of O(P) — the single biggest term in a crawl's wall-clock time,
    since a page fetch over Tor is tens of seconds and everything downstream of it is
    milliseconds. `fetch_slots` is the run-wide budget on simultaneous fetches; pass the one
    `run_pipeline` builds so pages and sources share a ceiling instead of multiplying.

    `time_budget` is this source's share of the run's window, in seconds. It is a soft
    deadline checked between waves and tree levels, never a cancellation: a crawl that runs
    out of time stops cleanly with what it has, instead of being killed mid-write.

    On a deep walk, links found on changed listing pages are followed (see `follow_links`
    below) — the listing is only the top of a leak site's tree.
    """
    result = SourceResult(slug=source.slug)
    started = asyncio.get_running_loop().time()

    def out_of_time() -> bool:
        if time_budget is None:
            return False
        return asyncio.get_running_loop().time() - started >= time_budget

    # `depth` is normally decided by the source's own deep-walk schedule. An explicit value
    # is how the CLI forces a full walk on demand.
    crawl_kind = depth or crawl_depth_for(source)
    # A shallow probe is page 1 and nothing else — `page_waves` already yields page 1 alone
    # in its first wave, so capping the page count is the whole implementation.
    effective_max_pages = 1 if crawl_kind == "shallow" else source.max_pages

    run_id = await storage.start_crawl(source.id, depth=crawl_kind)

    collector = get_collector(
        source.collector,
        host=settings.tor_host,
        socks_ports=settings.tor_socks_ports,
        timeout=settings.request_timeout_seconds,
        max_retries=settings.max_retries,
        backoff_seconds=settings.retry_backoff_seconds,
        backoff_cap_seconds=settings.retry_backoff_cap_seconds,
    )
    extractor = get_extractor(extractor_name or settings.extractor)

    # The address this crawl actually uses. Starts at the source's configured (or
    # previously failed-over) address and may move to a mirror below.
    crawl_base = source.crawl_url
    known_hosts = await storage.known_onion_hosts() if settings.discover_mirrors else set()

    width = max(1, settings.page_concurrency)
    # A caller with no run-wide budget (the CLI crawling one source) gets a private one, so
    # this function is never unbounded regardless of how it is entered.
    slots = fetch_slots if fetch_slots is not None else asyncio.Semaphore(width)
    # `request_delay_seconds` is spread across a wave rather than slept between pages: the
    # site still sees requests arrive at the configured rate, but they overlap in flight
    # rather than queueing behind each other.
    stagger = source.request_delay_seconds / width if source.request_delay_seconds > 0 else 0.0

    json_mapping = parse_mapping(source.json_items) if source.collector == "browser" else None

    async def fetch_page(page_no: int, base: str, delay: float = 0.0) -> _Page | None:
        """Fetch one page. None means this source has no URL for that page number."""
        url = page_url(base, page_no, source.pagination_style)
        if url is None:
            return None
        if delay > 0:
            await asyncio.sleep(delay)
        async with slots:
            if json_mapping is None:
                return _Page(page_no, url, await collector.fetch(url))
            # The JSON the page loads only comes back on the detailed result. For the browser
            # collector `fetch` is this same single attempt, unwrapped.
            fetched = await collector.fetch_detailed(url)  # type: ignore[attr-defined]
            collector.last_error = fetched.error
            html = fetched.text if fetched.kind == "ok" else None
            return _Page(page_no, url, html, fetched.json_responses)

    async def fetch_wave(pages: list[int], base: str) -> list[_Page]:
        """Fetch a whole wave at once, returned in page order."""
        fetched = await asyncio.gather(
            *(fetch_page(no, base, stagger * offset) for offset, no in enumerate(pages))
        )
        return [page for page in fetched if page is not None]

    # Changed listing pages whose links are worth following, as (url, html, the text of their
    # new or changed tiles — None for a page without tiles).
    seeds: list[tuple[str, str, set[str] | None]] = []
    follow = settings.follow_links and crawl_kind == "deep"

    async def process(page: _Page) -> bool:
        """Fold one fetched page into the result. False means the listing ended here."""
        if page.html is None:
            # A missing page N>1 just means the listing ended.
            return False

        # Counted here, before the content checks below: a challenge page is a real fetch,
        # just not a listing, and must not be recorded as a successful crawl of zero pages.
        result.pages_fetched += 1
        result.bytes_fetched += len(page.html.encode("utf-8"))

        text = to_text(page.html, item_selector=source.item_selector)

        if settings.discover_mirrors:
            result.mirrors_found += await _record_mirrors(
                text, source=source, storage=storage, url=page.url, known_hosts=known_hosts
            )

        gate = gate_kind(text) if page.page_no == 1 else None
        if gate:
            # Same failure as the empty page below, one step subtler: the site answered 200
            # with a few hundred characters of interstitial. Passing the length check would
            # store it and reset the failure counter, so a source behind a DDoS queue or a
            # human check would read as healthy while collecting nothing. Passing the gate
            # is not something this crawler does.
            result.status = "failed"
            result.error = (
                f"page 1 is a {gate} page, not a listing — the site gates automated access"
            )
            log.info("gate page, stopping", source=source.slug, gate=gate)
            return False

        if len(text.strip()) < MIN_PAGE_TEXT_CHARS:
            if page.page_no == 1:
                # Reached, returned something, but nothing to read. Almost always a JS
                # challenge or an interstitial, and worth failing loudly: the source
                # needs `collector: browser` or a new address, and silence here is how
                # akira sat at "succeeded" for days while collecting nothing.
                result.status = "failed"
                result.error = (
                    f"page 1 returned {len(text.strip())} chars of text — challenge page "
                    f"or JS-rendered listing (try collector: browser)"
                )
            log.info("page empty, stopping", source=source.slug, page=page.page_no)
            return False

        json_records = match_items(page.json_responses, json_mapping)
        if json_records is not None and json_mapping is not None:
            text = f"{text}\n{items_text(json_records, json_mapping)}"

        ingested = await ingest_page(
            storage=storage,
            settings=settings,
            source=source,
            extractor=extractor,
            run_id=run_id,
            url=page.url,
            page_no=page.page_no,
            text=text,
            json_records=json_records,
            json_mapping=json_mapping,
        )

        if not ingested.changed:
            # The content hash already exists for this source: nothing new here, and
            # nothing downstream needs to run. This is the short circuit that makes
            # repeat crawls cheap.
            log.debug("page unchanged, skipping extraction", source=source.slug, page=page.page_no)
            return True

        result.pages_changed += 1
        if follow:
            seeds.append((page.url, page.html, ingested.changed_blocks))

        upserted = ingested.upserted
        result.leaks.inserted += upserted.inserted
        result.leaks.updated += upserted.updated
        result.leaks.skipped += upserted.skipped
        result.leaks.new_leak_ids.extend(upserted.new_leak_ids)
        return True

    async def follow_links(seed_pages: list[tuple[str, str, set[str] | None]]) -> None:
        """Breadth-first walk down the tree behind the listing.

        Level 1 is the links on the listing pages, level 2 the links on those, and so on to
        `link_depth`. Three things keep this bounded, and all three are needed: at most
        `links_per_page` links are taken from any page, at most `link_max_pages` pages are
        fetched in total, and the source's time budget is checked before every level. Each
        page is fetched once however many pages link to it.

        On a listing split into tiles, the links inside new or changed tiles are taken first
        and the links inside unchanged tiles not at all, so the budget goes on the pages of
        victims that just appeared or moved.

        A followed page is stored and searched for exposures and onion addresses, like a
        listing page, but it is not run through the listing extractor — a victim's own page
        is not a list of victims. It fills in the empty fields of the leak the listing holds
        for its victim (`ingest_page` with `detail`).
        """
        visited = {normalize_url(url) for url, _, _ in seed_pages}
        remaining = settings.link_max_pages
        level = seed_pages

        async def fetch_link(index: int, url: str) -> _Page:
            if stagger > 0:
                await asyncio.sleep(stagger * index)
            async with slots:
                return _Page(0, url, await collector.fetch(url))

        for depth_no in range(1, settings.link_depth + 1):
            if remaining <= 0:
                break
            if out_of_time():
                result.truncated = True
                log.warning("time budget spent, stopping link walk", source=source.slug)
                break

            targets: list[str] = []
            for url, html, changed_blocks in level:
                prefer, avoid = (
                    tile_links(html, item_selector=source.item_selector, changed=changed_blocks)
                    if changed_blocks is not None
                    else ([], [])
                )
                for link in extract_links(
                    html,
                    url,
                    visited=visited,
                    limit=settings.links_per_page,
                    prefer=prefer,
                    avoid=avoid,
                ):
                    visited.add(normalize_url(link))
                    targets.append(link)
            targets = targets[:remaining]
            if not targets:
                break

            fetched = await asyncio.gather(
                *(fetch_link(index, url) for index, url in enumerate(targets))
            )

            level = []
            for page in fetched:
                if page.html is None:
                    continue
                remaining -= 1
                result.pages_fetched += 1
                result.link_pages += 1
                result.bytes_fetched += len(page.html.encode("utf-8"))

                text = to_text(page.html, segment=False)
                if settings.discover_mirrors:
                    result.mirrors_found += await _record_mirrors(
                        text,
                        source=source,
                        storage=storage,
                        url=page.url,
                        known_hosts=known_hosts,
                    )
                if len(text.strip()) < MIN_PAGE_TEXT_CHARS:
                    continue

                ingested = await ingest_page(
                    storage=storage,
                    settings=settings,
                    source=source,
                    extractor=extractor,
                    run_id=run_id,
                    url=page.url,
                    page_no=depth_no,
                    text=text,
                    extract_leaks=False,
                    detail=detail_page(page.html),
                )
                if not ingested.changed:
                    continue
                result.pages_changed += 1
                result.leaks.updated += ingested.upserted.updated
                level.append((page.url, page.html, None))

            log.info(
                "link level done",
                source=source.slug,
                level=depth_no,
                fetched=len(fetched),
                followable=len(level),
            )

    try:
        waves = page_waves(effective_max_pages, width=width, cap=settings.page_wave_cap)
        # Page 1 is fetched on its own, ahead of any speculation. It decides whether the
        # source is reachable, whether to fail over to a mirror, and whether what came back
        # is a listing at all — and a failover changes the address every other page would
        # have been fetched from, so pages speculated on beforehand are wasted circuits
        # against an address we are in the middle of abandoning.
        next(waves)
        first = await fetch_page(1, crawl_base)
        if first is None:  # pragma: no cover - page 1 is always the base URL
            return result

        if first.html is None:
            # The primary address is unreachable. Before recording a failure, try the
            # addresses this site has published about itself — that is the whole point
            # of collecting them.
            html, moved_to = await _try_mirrors(
                source, storage=storage, collector=collector, settings=settings
            )
            if moved_to is not None:
                crawl_base = moved_to
                result.switched_to = moved_to
            first = _Page(1, moved_to or first.url, html)

        if first.html is None:
            result.status = "failed"
            reason = getattr(collector, "last_error", None)
            result.error = (
                f"could not fetch {first.url}: {reason}"
                if reason
                else f"could not fetch {first.url}"
            )
            return result

        batch = [first]
        listing_ended = False
        while not listing_ended:
            for index, page in enumerate(batch):
                if not await process(page):
                    # Everything after the terminal page in this wave was fetched
                    # speculatively and sits past the end of the listing. Dropped rather
                    # than processed: "the first page that comes back empty ends the
                    # listing" is the rule the sequential crawler enforced, and page
                    # numbering downstream assumes it.
                    result.pages_discarded += len(batch) - index - 1
                    listing_ended = True
                    break
            if listing_ended:
                break

            wave = next(waves, None)
            if wave is None:
                break
            if out_of_time():
                result.truncated = True
                log.warning("time budget spent, stopping listing", source=source.slug)
                break
            batch = await fetch_wave(wave, crawl_base)
            if not batch:
                break

        if result.status == "succeeded" and seeds:
            await follow_links(seeds)

    except asyncio.CancelledError:
        # Cancellation is NOT an Exception subclass, so without this it would fall straight
        # through to the `finally` below with `result.status` still at its default
        # "succeeded" — a source in flight when the job timeout fires would be written as a
        # successful crawl of zero pages. Record the truth, then re-raise: cancellation must
        # stay cancellation.
        result.status = "failed"
        result.error = "crawl cancelled (worker shutdown or job timeout)"
        log.warning("source crawl cancelled", source=source.slug)
        raise
    except Exception as exc:  # noqa: BLE001 - deliberate: record and continue
        log.exception("source crawl failed", source=source.slug)
        result.status = "failed"
        result.error = str(exc)[:1000]
    finally:
        with contextlib.suppress(Exception):
            await collector.aclose()
        # Shielded so that a cancellation arriving mid-cleanup cannot abandon the crawl_runs
        # row in 'running' forever. The write still completes; only our wait for it can be
        # interrupted.
        await asyncio.shield(
            storage.finish_crawl(
                run_id,
                source.id,
                status=result.status,
                # A walk the time budget cut short has not seen the whole listing, so it
                # must not push the next full walk six hours out.
                depth="shallow" if result.truncated else crawl_kind,
                pages_fetched=result.pages_fetched,
                pages_changed=result.pages_changed,
                bytes_fetched=result.bytes_fetched,
                error=result.error,
            )
        )

    return result


class SourceRef(Protocol):
    """The two things ingestion needs to know about a source."""

    id: int
    slug: str


@dataclass(slots=True)
class SourceIdent:
    """A `SourceRef` for callers that hold a claimed URL rather than a full `SourceRow`."""

    id: int
    slug: str


@dataclass(slots=True)
class IngestResult:
    page_id: int
    # False when this exact content was already stored and extracted for the source, so
    # nothing ran. True means the page went through extraction just now.
    changed: bool
    found: int = 0
    upserted: UpsertResult = field(default_factory=UpsertResult)
    # On a listing split into tiles: the text of the tiles that are new or changed since the
    # page was last stored. Only their links are followed. None when the page has no tiles.
    changed_blocks: set[str] | None = None


async def ingest_page(
    *,
    storage: Storage,
    settings: Settings,
    source: SourceRef,
    extractor: object,
    run_id: int | None,
    url: str,
    page_no: int,
    text: str,
    extract_leaks: bool = True,
    detail: DetailPage | None = None,
    json_records: list[dict] | None = None,
    json_mapping: JsonMapping | None = None,
) -> IngestResult:
    """Store a fetched page and run it through extraction. The one place this happens.

    Cleaned text in; `raw_pages` row, leaks and exposures out. Both crawlers call it - the
    source-at-a-time one (`crawl_source`) and the URL-queue one (`intel.crawl.fetch`) - so a
    page is ingested identically however it arrived.

    `extract_leaks=False` is for a page reached by following a link out of a listing: a
    victim's own page is stored and searched for exposures, but it is not a list of victims, so
    it is never run through the listing extractor and never creates a leak. With `detail` (the
    page read by `collectors.detail_page`) it fills the empty fields of the leak the listing
    already holds for that victim — see `extract_detail` and `Storage.enrich_leak`.

    `json_records` are the victim records a browser source's page loaded as JSON, picked out
    by the source's `json_mapping` (`extract.json_items`). When there are any, a listing's
    leaks are built from them instead of from its text.

    Raises if storage or extraction fails, after the page row may already have been written.
    That is deliberate and safe: the row is not counted as handled until `mark_extracted`
    (see `Storage.save_page`), so a caller that retries will extract it again.
    """
    page_id, changed = await storage.save_page(
        source_id=source.id,
        crawl_run_id=run_id,
        url=url,
        page_no=page_no,
        text=text,
    )
    if not changed:
        return IngestResult(page_id=page_id, changed=False)

    leaks: list[ExtractedLeak] = []
    upserted = UpsertResult()
    changed_blocks: set[str] | None = None
    if extract_leaks:
        if json_records is not None and json_mapping is not None:
            leaks = leaks_from_items(
                json_records,
                json_mapping,
                source_group=source.slug,
                source_url=url,
                page_no=page_no,
            )
        else:
            leaks = extract_page(
                text,
                source_group=source.slug,
                source_url=url,
                page_no=page_no,
                extractor_name=extractor.name,  # type: ignore[attr-defined]
                extractor=extractor,
            )
        upserted = await storage.upsert_leaks(leaks, source_id=source.id)
        blocks = split_blocks(text)
        if blocks is not None:
            previous = await storage.previous_text(
                source_id=source.id, url=url, exclude_sha256=content_hash(text)
            )
            seen = set(split_blocks(previous) or []) if previous else set()
            changed_blocks = {block for block in blocks if block not in seen}
    elif detail is not None:
        found = extract_detail(
            detail,
            source_group=source.slug,
            source_url=url,
            page_no=page_no,
            extractor_name=extractor.name,  # type: ignore[attr-defined]
            extractor=extractor,
        )
        if found is not None:
            leak, names = found
            leaks = [leak]
            leak_id = await storage.enrich_leak(
                leak, names=names, source_id=source.id, detail_url=url
            )
            if leak_id is None:
                # Never a new leak from a detail page: what is not on the listing is not
                # known to be a victim. Logged, so a site whose detail pages never match
                # (a name the listing prints differently) is visible.
                log.info(
                    "detail page matched no listed victim",
                    source=source.slug,
                    url=url,
                    names=names[:3],
                    domain=leak.victim_domain,
                )
            else:
                upserted.updated = 1
                log.info("detail page enriched a leak", source=source.slug, leak_id=leak_id)

    await storage.mark_extracted(page_id)

    if settings.exposure_detection:
        await _record_exposures(text, source=source, storage=storage, settings=settings, url=url)

    log.info(
        "page processed",
        source=source.slug,
        page=page_no,
        found=len(leaks),
        new=upserted.inserted,
        seen_again=upserted.updated,
    )
    return IngestResult(
        page_id=page_id,
        changed=True,
        found=len(leaks),
        upserted=upserted,
        changed_blocks=changed_blocks,
    )


async def _record_exposures(
    text: str,
    *,
    source: SourceRef,
    storage: Storage,
    settings: Settings,
    url: str,
) -> int:
    """Look for credentials, keys and cards in a changed page and store what is found.

    Runs only on a page whose content hash is new, so an unchanged page costs nothing here
    either. Failure is contained: this is a second opinion on a page whose listings have
    already been saved, and a bug in a regex must not turn a successful crawl into a failed
    one. The detector's `value` never reaches storage — only the masked preview and the keyed
    fingerprint do.
    """
    try:
        found = find_exposures(text)
        if not found:
            return 0
        new, _ = await storage.upsert_exposures(
            [
                {
                    "kind": f.kind.value,
                    "detector": f.detector,
                    "fingerprint": f.fingerprint(settings.exposure_salt),
                    "preview": f.preview,
                    "email_domain": f.email_domain,
                    "confidence": f.confidence,
                }
                for f in found
            ],
            source_id=source.id,
            source_url=url,
        )
    except Exception:  # noqa: BLE001 - deliberate: never fail a crawl over a detector
        log.exception("exposure detection failed", source=source.slug, url=url)
        return 0

    log.info("exposures found", source=source.slug, found=len(found), new=new)
    return new


async def _record_mirrors(
    text: str,
    *,
    source: SourceRef,
    storage: Storage,
    url: str,
    known_hosts: set[str],
) -> int:
    """Note every onion address on a page that isn't one we already track.

    Addresses the page presents as this site's own — "our mirror", "we have moved to" — are
    recorded as `self_declared` and are the only discovered addresses failover will consider.
    Everything else on the page is recorded as a plain `candidate`: still intelligence, but
    nothing acts on it without an operator saying so.
    """
    this_host = onion_host(url)
    announced, other = classify_onion_urls(text, exclude_hosts=known_hosts | {this_host or ""})
    if not announced and not other:
        return 0

    # Recorded so the next page in this run doesn't re-report the same addresses.
    known_hosts.update(announced)
    known_hosts.update(other)

    new = 0
    new += await storage.record_mirrors(
        source.id, announced, discovered_from=url, status="self_declared"
    )
    new += await storage.record_mirrors(source.id, other, discovered_from=url, status="candidate")

    if new:
        log.info(
            "new onion addresses seen",
            source=source.slug,
            new=new,
            announced=list(announced)[:5],
        )
    return new


async def _try_mirrors(
    source: SourceRow,
    *,
    storage: Storage,
    collector: object,
    settings: Settings,
) -> tuple[str | None, str | None]:
    """Try this source's known-good alternative addresses. Returns (html, url that worked).

    Only `approved` and `self_declared` addresses are tried, and only when
    `CRAWL_MIRROR_FAILOVER` is on. The restriction is the point: these addresses come from
    text served by the site being crawled, so following an arbitrary one would let a crawled
    host redirect the crawler anywhere it liked. A `self_declared` address at least came
    from the site it claims to replace, and every switch is logged and written to
    `sources.active_url` where an operator can see and undo it.
    """
    if not settings.mirror_failover:
        return None, None

    mirrors = await storage.failover_mirrors(source.id)
    if not mirrors:
        return None, None

    for mirror in mirrors:
        log.info(
            "primary address failed, trying mirror",
            source=source.slug,
            mirror=mirror.onion_host,
            trust=mirror.status,
        )
        html = await collector.fetch(mirror.url)  # type: ignore[attr-defined]
        if html is None:
            continue
        if len(to_text(html).strip()) < MIN_PAGE_TEXT_CHARS:
            continue

        await storage.promote_mirror(source.id, mirror.url)
        log.warning(
            "source switched to a mirror",
            source=source.slug,
            was=source.crawl_url,
            now=mirror.url,
            trust=mirror.status,
        )
        return html, mirror.url

    return None, None


def extract_page(
    text: str,
    *,
    source_group: str,
    source_url: str | None,
    page_no: int,
    extractor_name: str,
    extractor: object | None = None,
) -> list[ExtractedLeak]:
    """Page text -> validated leaks. Pure, so it is trivially testable against fixtures.

    Text that `to_text` split into listing blocks is extracted one block at a time, at most
    one leak per block, and text outside the blocks makes no leaks. Text without blocks goes
    through the whole-page linker exactly as before.
    """
    engine = extractor if extractor is not None else get_extractor(extractor_name)

    blocks = split_blocks(text)
    if blocks is not None:
        leaks: list[ExtractedLeak] = []
        for block in blocks:
            leak = link_block(
                engine.extract_block(block),  # type: ignore[attr-defined]
                block_text=block,
                source_group=source_group,
                source_url=source_url,
                page_no=page_no,
                method=extractor_name,
            )
            if leak is not None:
                leaks.append(leak)
        return leaks

    spans = engine.extract(text)  # type: ignore[attr-defined]
    return link_spans(
        spans,
        source_group=source_group,
        source_url=source_url,
        page_no=page_no,
        method=extractor_name,
        page_text=text,
    )


# How much of a victim's own page is read. The same backstop as a listing window
# (`describe._MAX_WINDOW_CHARS`): a detail page that goes on to list ten thousand file names
# has said what it has to say about the victim long before then.
_DETAIL_MAX_CHARS = 8000


def extract_detail(
    page: DetailPage,
    *,
    source_group: str,
    source_url: str | None,
    page_no: int,
    extractor_name: str,
    extractor: object | None = None,
) -> tuple[ExtractedLeak, list[str]] | None:
    """A victim's own page -> (its fields as a leak, the names it may be filed under).

    One victim per page. Its name is the first heading that reads as a name — `<h1>`, then
    `<h2>`, then a title-like element, then the document title — and every other field
    (domain, country, size, date, status, revenue and description into the summary) comes
    from the page's main text, read like one listing tile. The other name candidates are
    returned too: a page whose `<h1>` is the crew's banner still names its victim in an `<h2>`.

    The leak is never stored as such; it is what `Storage.enrich_leak` fills in from.
    """
    engine = extractor if extractor is not None else get_extractor(extractor_name)
    text = page.text[:_DETAIL_MAX_CHARS]

    names = [named[0] for title in page.titles if (named := block_victim_name(title))]
    spans = [
        span
        for span in engine.extract_block(text)  # type: ignore[attr-defined]
        if span.label != Label.VICTIM
    ]
    if names:
        at = max(text.find(names[0]), 0)
        spans.append(Span(Label.VICTIM, names[0], at, at + len(names[0]), 0.6))
        spans.sort(key=lambda span: (span.start, span.end))

    leak = link_block(
        spans,
        block_text=text,
        source_group=source_group,
        source_url=source_url,
        page_no=page_no,
        method=extractor_name,
        mode="detail",
        drop=tuple(page.titles),
    )
    if leak is None:
        return None
    return leak, names


def split_blocks(text: str) -> list[str] | None:
    """The listing blocks `to_text` marked in page text, or None for text it did not split.

    Segmented text is always `before, block, …, block, after`; the outer two are page text
    around the listing and are not blocks.
    """
    if RECORD_SEPARATOR not in text:
        return None
    parts = text.split(BLOCK_BREAK)
    return [part for part in parts[1:-1] if part.strip()]


def crawl_depth_for(source: SourceRow, *, now: datetime | None = None) -> str:
    """Whether this crawl should walk the whole listing or just probe page 1.

    Leak sites append new victims to the front of their listing, so page 1 is where anything
    new appears. A full walk exists to catch what page 1 cannot: edits to older entries, and
    pages missed by a crawl that failed partway down.

    A source that has never had a deep walk gets one — the first crawl must see the whole
    backlog, or the "new since yesterday" baseline is built from one page of a ten-page site.
    """
    if source.last_deep_crawl_at is None:
        return "deep"
    moment = now or datetime.now(UTC)
    elapsed = (moment - source.last_deep_crawl_at).total_seconds()
    return "deep" if elapsed >= source.deep_crawl_interval_seconds else "shallow"


_EPOCH = datetime.min.replace(tzinfo=UTC)


def stalest_first(sources: list[SourceRow]) -> list[SourceRow]:
    """Order sources so the one waiting longest goes first; never-crawled ones lead.

    Sources start in this order (the concurrency semaphore is first-in-first-out), so when a
    run cannot finish — the window closes, the worker is restarted — the sources it never
    reached are exactly the ones that go first next time. Alphabetical order would starve
    whoever sorts last, every time.
    """
    return sorted(
        sources,
        key=lambda s: (
            s.last_crawl_at is not None,
            s.last_crawl_at or datetime.min.replace(tzinfo=UTC),
        ),
    )


def due_sources(sources: list[SourceRow], *, now: datetime | None = None) -> list[SourceRow]:
    """Keep only the sources whose own interval says they are ready to be crawled again.

    Each source's `crawl_interval_seconds` is honoured individually, so a fast-moving site
    is refetched on its own cadence and a stable one is not refetched for nothing.

    A source that has never been crawled is always due.
    """
    moment = now or datetime.now(UTC)
    ready: list[SourceRow] = []
    for source in sources:
        if source.last_crawl_at is None:
            ready.append(source)
            continue
        elapsed = (moment - source.last_crawl_at).total_seconds()
        if elapsed >= source.crawl_interval_seconds:
            ready.append(source)
    return stalest_first(ready)


async def run_pipeline(
    *,
    storage: Storage,
    settings: Settings,
    slugs: list[str] | None = None,
    extractor_name: str | None = None,
    only_due: bool = False,
) -> RunResult:
    """Crawl sources concurrently, bounded by `settings.concurrency`.

    `only_due` is what the scheduler passes: crawl the sources whose `crawl_interval_seconds`
    has elapsed and leave the rest alone. Manual runs pass False, because "sync now" means
    now.
    """
    sources = await storage.list_sources(only_enabled=True)
    if slugs:
        wanted = set(slugs)
        sources = [source for source in sources if source.slug in wanted]

    considered = len(sources)
    if only_due:
        sources = due_sources(sources)

    if not sources:
        if only_due and considered:
            log.info("no sources due", considered=considered)
        else:
            log.warning("no sources to crawl", requested=slugs)
        return RunResult()

    sources = stalest_first(sources)
    budget = source_time_budget(
        len(sources),
        concurrency=settings.concurrency,
        window_seconds=settings.run_window_seconds,
        floor_seconds=settings.source_min_budget_seconds,
        ceiling_seconds=settings.source_max_budget_seconds,
    )
    # The floor wins over the window (a source with no time fetches nothing), so with enough
    # sources the run is longer than asked. Say so — the fix is more concurrency or Tor ports.
    needed = budget * len(sources) / max(1, settings.concurrency)
    if needed > settings.run_window_seconds * 1.05:
        log.warning(
            "run will overrun its window",
            sources=len(sources),
            window_seconds=settings.run_window_seconds,
            expected_seconds=round(needed),
            hint="raise CRAWL_CONCURRENCY (and TOR_SOCKS_PORTS) or CRAWL_RUN_WINDOW",
        )

    semaphore = asyncio.Semaphore(settings.concurrency)
    # One budget for the whole run. Sources are concurrent and so are the pages within each
    # of them, so without a shared ceiling the two settings multiply and Tor — not the
    # crawler — becomes the thing deciding how fast anything goes.
    fetch_slots = asyncio.Semaphore(settings.fetch_budget)

    async def guarded(source: SourceRow) -> SourceResult:
        async with semaphore:
            return await crawl_source(
                source,
                storage=storage,
                settings=settings,
                extractor_name=extractor_name,
                fetch_slots=fetch_slots,
                time_budget=budget,
            )

    log.info(
        "run starting",
        source_budget_seconds=round(budget),
        sources=len(sources),
        skipped_not_due=considered - len(sources),
        concurrency=settings.concurrency,
        page_concurrency=settings.page_concurrency,
        fetch_budget=settings.fetch_budget,
    )
    results = await asyncio.gather(*(guarded(source) for source in sources))

    run = RunResult(sources=list(results))

    log.info(
        "run complete",
        sources=len(run.sources),
        new_leaks=run.inserted,
        seen_again=run.updated,
        failed=run.failed,
        discarded_pages=run.discarded,
        switched=[s.slug for s in run.sources if s.switched_to],
    )
    return run


async def run_pipeline_locked(
    *,
    storage: Storage,
    settings: Settings,
    slugs: list[str] | None = None,
    extractor_name: str | None = None,
    only_due: bool = False,
) -> RunResult | None:
    """`run_pipeline`, but only if no other crawl is in flight. None means "skipped".

    Every entry point goes through this. Two crawls sharing one Tor daemon contend for
    circuits and both get slower — a scheduled run and a manual one overlapping is exactly
    how a set of sources ends up failing with "TTL expired" that succeed fine on their own.
    """
    async with storage.crawl_lock() as acquired:
        if not acquired:
            log.warning("another crawl is already running, skipping this one")
            return None
        return await run_pipeline(
            storage=storage,
            settings=settings,
            slugs=slugs,
            extractor_name=extractor_name,
            only_due=only_due,
        )
