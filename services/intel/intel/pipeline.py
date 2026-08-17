"""fetch → hash → parse → normalize → dedupe → upsert.

The whole point of this module is that it runs unattended. The old workflow was: open
Jupyter, run `Scrape.ipynb`, wait hours, open `Mapping.ipynb`, edit a filename in a cell,
run every cell in order, and hope you didn't run it twice.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

import structlog

from .collectors import classify_onion_urls, get_collector, onion_host, page_url, to_text
from .config import Settings
from .extract import get_extractor, link_spans
from .models import ExtractedLeak
from .storage import SourceRow, Storage, UpsertResult

log = structlog.get_logger(__name__)


# A page shorter than this carries no listings. It is a challenge page, a JS shell, or an
# error — all of which used to be indistinguishable from a healthy crawl in the database.
MIN_PAGE_TEXT_CHARS = 50


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


async def crawl_source(
    source: SourceRow,
    *,
    storage: Storage,
    settings: Settings,
    extractor_name: str | None = None,
) -> SourceResult:
    """Crawl one source end to end. Never raises — failures are recorded, not propagated.

    One bad source must not abort the run; the old script died on the first unhandled
    exception and lost every page it had already fetched.
    """
    result = SourceResult(slug=source.slug)
    run_id = await storage.start_crawl(source.id)

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

    try:
        for page_no in range(1, source.max_pages + 1):
            url = page_url(crawl_base, page_no, source.pagination_style)
            if url is None:
                break

            html = await collector.fetch(url)

            if html is None and page_no == 1:
                # The primary address is unreachable. Before recording a failure, try the
                # addresses this site has published about itself — that is the whole point
                # of collecting them.
                html, moved_to = await _try_mirrors(
                    source, storage=storage, collector=collector, settings=settings
                )
                if moved_to is not None:
                    crawl_base = moved_to
                    result.switched_to = moved_to
                    url = moved_to

            if html is None:
                # A missing page N>1 just means the listing ended.
                if page_no == 1:
                    result.status = "failed"
                    reason = getattr(collector, "last_error", None)
                    result.error = (
                        f"could not fetch {url}: {reason}" if reason
                        else f"could not fetch {url}"
                    )
                break

            # Counted here, before the content checks below. These two lines used to sit
            # after the empty-page check, so a source that returned a challenge page — a
            # real fetch, just not a listing — was recorded as a successful crawl of zero
            # pages, which reset its failure counter and made a blocked site look healthy.
            result.pages_fetched += 1
            result.bytes_fetched += len(html.encode("utf-8"))

            text = to_text(html)

            if settings.discover_mirrors:
                result.mirrors_found += await _record_mirrors(
                    text, source=source, storage=storage, url=url, known_hosts=known_hosts
                )

            if len(text.strip()) < MIN_PAGE_TEXT_CHARS:
                if page_no == 1:
                    # Reached, returned something, but nothing to read. Almost always a JS
                    # challenge or an interstitial, and worth failing loudly: the source
                    # needs `collector: browser` or a new address, and silence here is how
                    # akira sat at "succeeded" for days while collecting nothing.
                    result.status = "failed"
                    result.error = (
                        f"page 1 returned {len(text.strip())} chars of text — challenge page "
                        f"or JS-rendered listing (try collector: browser)"
                    )
                log.info("page empty, stopping", source=source.slug, page=page_no)
                break

            page_id, changed = await storage.save_page(
                source_id=source.id,
                crawl_run_id=run_id,
                url=url,
                page_no=page_no,
                text=text,
            )

            if not changed:
                # The content hash already exists for this source: nothing new here, and
                # nothing downstream needs to run. This is the short circuit that makes
                # repeat crawls cheap.
                log.debug("page unchanged, skipping extraction", source=source.slug, page=page_no)
                continue

            result.pages_changed += 1

            leaks = extract_page(
                text,
                source_group=source.slug,
                source_url=url,
                page_no=page_no,
                extractor_name=extractor.name,
                extractor=extractor,
            )
            upserted = await storage.upsert_leaks(leaks, source_id=source.id)
            result.leaks.inserted += upserted.inserted
            result.leaks.updated += upserted.updated
            result.leaks.skipped += upserted.skipped
            result.leaks.new_leak_ids.extend(upserted.new_leak_ids)

            await storage.mark_extracted(page_id)

            log.info(
                "page processed",
                source=source.slug,
                page=page_no,
                found=len(leaks),
                new=upserted.inserted,
                seen_again=upserted.updated,
            )

            # Politeness delay between pages of the same source. Other sources are being
            # crawled concurrently, so this costs nothing in wall-clock terms.
            if page_no < source.max_pages:
                await asyncio.sleep(source.request_delay_seconds)

    except asyncio.CancelledError:
        # Cancellation is NOT an Exception subclass, so it used to fall straight through to
        # the `finally` below with `result.status` still at its default "succeeded". Every
        # source in flight when the worker's job timeout fired was therefore written to the
        # database as a successful crawl of zero pages — which reset `consecutive_failures`
        # and set `last_success_at`, so sources that had never been fetched showed as
        # healthy. Record the truth, then re-raise: cancellation must stay cancellation.
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
                pages_fetched=result.pages_fetched,
                pages_changed=result.pages_changed,
                bytes_fetched=result.bytes_fetched,
                error=result.error,
            )
        )

    return result


async def _record_mirrors(
    text: str,
    *,
    source: SourceRow,
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
    announced, other = classify_onion_urls(
        text, exclude_hosts=known_hosts | {this_host or ""}
    )
    if not announced and not other:
        return 0

    # Recorded so the next page in this run doesn't re-report the same addresses.
    known_hosts.update(announced)
    known_hosts.update(other)

    new = 0
    new += await storage.record_mirrors(
        source.id, announced, discovered_from=url, status="self_declared"
    )
    new += await storage.record_mirrors(
        source.id, other, discovered_from=url, status="candidate"
    )

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
    """Page text -> validated leaks. Pure, so it is trivially testable against fixtures."""
    engine = extractor if extractor is not None else get_extractor(extractor_name)
    spans = engine.extract(text)  # type: ignore[attr-defined]
    return link_spans(
        spans,
        source_group=source_group,
        source_url=source_url,
        page_no=page_no,
        method=extractor_name,
    )


async def run_pipeline(
    *,
    storage: Storage,
    settings: Settings,
    slugs: list[str] | None = None,
    extractor_name: str | None = None,
) -> RunResult:
    """Crawl sources concurrently, bounded by `settings.concurrency`."""
    sources = await storage.list_sources(only_enabled=True)
    if slugs:
        wanted = set(slugs)
        sources = [source for source in sources if source.slug in wanted]

    if not sources:
        log.warning("no sources to crawl", requested=slugs)
        return RunResult()

    semaphore = asyncio.Semaphore(settings.concurrency)

    async def guarded(source: SourceRow) -> SourceResult:
        async with semaphore:
            return await crawl_source(
                source, storage=storage, settings=settings, extractor_name=extractor_name
            )

    log.info("run starting", sources=len(sources), concurrency=settings.concurrency)
    results = await asyncio.gather(*(guarded(source) for source in sources))

    run = RunResult(sources=list(results))

    # Alert matching, driven by the ids that were actually inserted. `match_alerts` existed
    # as an arq task from the start but nothing ever enqueued it, so no alert had ever
    # fired; running it here means every path that can create a leak also evaluates it.
    if run.new_leak_ids:
        events = await storage.match_alerts(run.new_leak_ids)
        if events:
            log.info("alert events created", events=events, new_leaks=len(run.new_leak_ids))

    log.info(
        "run complete",
        sources=len(run.sources),
        new_leaks=run.inserted,
        seen_again=run.updated,
        failed=run.failed,
        switched=[s.slug for s in run.sources if s.switched_to],
    )
    return run


async def run_pipeline_locked(
    *,
    storage: Storage,
    settings: Settings,
    slugs: list[str] | None = None,
    extractor_name: str | None = None,
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
        )
