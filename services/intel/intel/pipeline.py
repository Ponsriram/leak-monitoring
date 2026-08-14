"""fetch → hash → parse → normalize → dedupe → upsert.

The whole point of this module is that it runs unattended. The old workflow was: open
Jupyter, run `Scrape.ipynb`, wait hours, open `Mapping.ipynb`, edit a filename in a cell,
run every cell in order, and hope you didn't run it twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from .collectors import get_collector, page_url, to_text
from .config import Settings
from .extract import get_extractor, link_spans
from .models import ExtractedLeak
from .storage import SourceRow, Storage, UpsertResult

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SourceResult:
    slug: str
    status: str = "succeeded"
    pages_fetched: int = 0
    pages_changed: int = 0
    bytes_fetched: int = 0
    leaks: UpsertResult = field(default_factory=UpsertResult)
    error: str | None = None


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
    )
    extractor = get_extractor(extractor_name or settings.extractor)

    try:
        for page_no in range(1, source.max_pages + 1):
            url = page_url(source.base_url, page_no, source.pagination_style)
            if url is None:
                break

            html = await collector.fetch(url)
            if html is None:
                # A missing page N>1 just means the listing ended.
                if page_no == 1:
                    result.status = "failed"
                    result.error = f"could not fetch {url}"
                break

            text = to_text(html)
            if len(text.strip()) < 50:
                log.info("page empty, stopping", source=source.slug, page=page_no)
                break

            result.pages_fetched += 1
            result.bytes_fetched += len(html.encode("utf-8"))

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

    except Exception as exc:  # noqa: BLE001 - deliberate: record and continue
        log.exception("source crawl failed", source=source.slug)
        result.status = "failed"
        result.error = str(exc)[:1000]
    finally:
        await collector.aclose()
        await storage.finish_crawl(
            run_id,
            source.id,
            status=result.status,
            pages_fetched=result.pages_fetched,
            pages_changed=result.pages_changed,
            bytes_fetched=result.bytes_fetched,
            error=result.error,
        )

    return result


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
    log.info(
        "run complete",
        sources=len(run.sources),
        new_leaks=run.inserted,
        seen_again=run.updated,
        failed=run.failed,
    )
    return run
