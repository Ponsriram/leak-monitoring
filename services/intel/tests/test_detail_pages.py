"""A victim's own page fills in the leak its listing tile created — and never creates one.

Also: the links worth following off a tiled listing are the ones inside its new or changed
tiles, so the per-page link budget is spent on victims' pages rather than on the menu.
"""

# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

from pathlib import Path

from test_pipeline_concurrency import (  # noqa: F401 - the autouse fixture must be in scope
    FakeStorage,
    make_settings,
    make_source,
    use_fake_collector,
)

from intel.collectors import FetchResult, detail_page, extract_links, tile_links, to_text
from intel.crawl.fetch import CrawlFetcher
from intel.crawl.queue import ClaimedUrl
from intel.extract import RulesExtractor
from intel.pipeline import SourceIdent, crawl_source, extract_detail, ingest_page, split_blocks
from intel.storage import UpsertResult, normalize_victim_name

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "http://example.onion"
LISTING_URL = f"{HOST}/blog/disclosure"
DETAIL_URL = f"{HOST}/blog/disclosure/66f1a0c3"
TILE_LINKS = [f"{LISTING_URL}/66f1a0c{n}" for n in range(2, 10)] + [f"{LISTING_URL}/66f1a0ca"]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class DetailStorage(FakeStorage):
    """Records what ingestion asked of storage; `previous` is the listing's last text."""

    def __init__(self, *, previous: str | None = None, enrich_returns: int | None = 7) -> None:
        super().__init__()
        self.previous = previous
        self.enrich_returns = enrich_returns
        self.upserted: list = []
        self.enriched: list[tuple] = []

    async def upsert_leaks(self, leaks, *, source_id):  # type: ignore[no-untyped-def]
        self.upserted.extend(leaks)
        return UpsertResult(inserted=len(leaks))

    async def previous_text(self, *, source_id, url, exclude_sha256):  # type: ignore[no-untyped-def]
        return self.previous

    async def enrich_leak(self, leak, *, names, source_id, detail_url):  # type: ignore[no-untyped-def]
        self.enriched.append((leak, names, detail_url))
        return self.enrich_returns


async def ingest(storage, text: str, **kwargs):  # type: ignore[no-untyped-def]
    return await ingest_page(
        storage=storage,
        settings=make_settings(EXPOSURE_DETECTION=False),
        source=SourceIdent(1, "inc-ransom"),
        extractor=RulesExtractor(),
        run_id=None,
        page_no=1,
        text=text,
        **kwargs,
    )


# ---------------------------------------------------------------- reading a detail page


def test_a_detail_page_is_read_without_its_chrome() -> None:
    page = detail_page(fixture("detail_page.html"))
    # The header's brand link and the nav are gone; the banner <h1> outside them is not.
    assert page.titles[:2] == ["INC. Ransom blog", "Grupo Caberj"]
    assert "Disclosures" not in page.text
    assert "Mirror:" not in page.text
    assert "Health insurance operator" in page.text


def test_a_detail_page_yields_its_victims_fields() -> None:
    found = extract_detail(
        detail_page(fixture("detail_page.html")),
        source_group="inc-ransom",
        source_url=DETAIL_URL,
        page_no=1,
        extractor_name="rules",
    )
    assert found is not None
    leak, names = found
    assert "Grupo Caberj" in names
    assert leak.victim_domain == "caberj.com.br"
    assert leak.victim_country == "Brazil"
    assert leak.leak_size_bytes == int(1.2 * 1024**4)
    assert leak.published_at is not None and leak.published_at.date().isoformat() == "2026-09-20"
    assert leak.status.value == "published"
    assert leak.summary is not None
    assert "Revenue: $210M" in leak.summary
    assert "Health insurance operator in Rio de Janeiro" in leak.summary
    # Name candidates are not description.
    assert "Grupo Caberj" not in leak.summary
    assert leak.extraction.model_dump()["mode"] == "detail"


def test_names_compare_without_case_or_punctuation() -> None:
    assert normalize_victim_name("Marlow & Finch, Solicitors") == "marlow finch solicitors"
    assert normalize_victim_name("  GRUPO   Caberj ") == "grupo caberj"


# ---------------------------------------------------------------- ingesting a detail page


async def test_a_followed_page_enriches_and_never_creates() -> None:
    storage = DetailStorage(enrich_returns=7)
    text = to_text(fixture("detail_page.html"), segment=False)
    result = await ingest(
        storage,
        text,
        url=DETAIL_URL,
        extract_leaks=False,
        detail=detail_page(fixture("detail_page.html")),
    )
    assert storage.upserted == []
    ((leak, names, url),) = storage.enriched
    assert "Grupo Caberj" in names and leak.victim_domain == "caberj.com.br"
    assert url == DETAIL_URL
    assert result.upserted.updated == 1 and result.upserted.inserted == 0


async def test_a_detail_page_with_no_listed_victim_creates_nothing() -> None:
    storage = DetailStorage(enrich_returns=None)
    result = await ingest(
        storage,
        to_text(fixture("detail_page.html"), segment=False),
        url=DETAIL_URL,
        extract_leaks=False,
        detail=detail_page(fixture("detail_page.html")),
    )
    assert storage.upserted == []
    assert len(storage.enriched) == 1
    assert result.upserted.total == 0


async def test_without_a_detail_a_followed_page_is_only_stored() -> None:
    storage = DetailStorage()
    await ingest(
        storage,
        to_text(fixture("detail_page.html"), segment=False),
        url=DETAIL_URL,
        extract_leaks=False,
    )
    assert storage.upserted == [] and storage.enriched == []


# ---------------------------------------------------------------- which tiles changed


def one_more_tile(html: str) -> str:
    """The listing with a new victim at the top, as a leak site adds one."""
    tile = (
        f'<a class="blog__card" href="{LISTING_URL}/77aa">'
        '<div class="blog__card-head"><span class="blog__card-name">Copperline Credit Union'
        '</span></div><div class="blog__card-domain">copperlinecu.org</div></a>'
    )
    return html.replace(
        '<div class="blog__list scroll-container">',
        f'<div class="blog__list scroll-container">{tile}',
        1,
    )


async def test_only_new_or_changed_tiles_are_marked_changed() -> None:
    old_html = fixture("tile_listing.html")
    new_html = one_more_tile(old_html)
    storage = DetailStorage(previous=to_text(old_html))
    result = await ingest(storage, to_text(new_html), url=LISTING_URL)
    assert result.changed_blocks is not None
    (changed,) = result.changed_blocks
    assert changed.startswith("Copperline Credit Union")


async def test_a_first_sight_marks_every_tile_changed() -> None:
    storage = DetailStorage(previous=None)
    text = to_text(fixture("tile_listing.html"))
    result = await ingest(storage, text, url=LISTING_URL)
    assert result.changed_blocks == set(split_blocks(text) or [])


async def test_a_page_without_tiles_reports_no_tile_changes() -> None:
    storage = DetailStorage(previous="anything")
    result = await ingest(storage, to_text(fixture("no_structure.html")), url=LISTING_URL)
    assert result.changed_blocks is None


# ---------------------------------------------------------------- choosing links


def test_tile_links_come_first_and_unchanged_tiles_are_skipped() -> None:
    html = one_more_tile(fixture("tile_listing.html"))
    changed = {block for block in split_blocks(to_text(html)) or [] if "Copperline" in block}
    prefer, avoid = tile_links(html, changed=changed)
    assert prefer == [f"{LISTING_URL}/77aa"]
    assert len(avoid) == len(TILE_LINKS)

    links = extract_links(html, LISTING_URL, limit=3, prefer=prefer, avoid=avoid)
    # The new victim first, then the page's own order — with every unchanged tile left out.
    assert links[0] == f"{LISTING_URL}/77aa"
    assert not set(links) & set(TILE_LINKS)


def test_without_change_information_every_tile_link_leads() -> None:
    html = fixture("tile_listing.html")
    prefer, _ = tile_links(html)
    links = extract_links(html, LISTING_URL, limit=3, prefer=prefer)
    # Without `prefer` the header's "/blog/leaks" would come first.
    assert links == TILE_LINKS[:3]


def test_preferred_links_obey_every_safety_rule() -> None:
    prefer = [
        "http://other.onion/victim",  # another host
        "/blog/disclosure/66f1a0c2/files/dump.zip",  # a file
        "/login",  # an account page
        "javascript:void(0)",
        "/blog/disclosure/66f1a0c2",
    ]
    links = extract_links("<html></html>", LISTING_URL, limit=10, prefer=prefer)
    assert links == [f"{LISTING_URL}/66f1a0c2"]


# ---------------------------------------------------------------- both engines


class SiteCollector:
    """Serves url -> html for both collector interfaces, recording what was fetched."""

    name = "http"

    def __init__(self, site: dict[str, str]) -> None:
        self.site = site
        self.requested: list[str] = []
        self.last_error: str | None = None

    async def fetch(self, url: str) -> str | None:
        self.requested.append(url)
        return self.site.get(url)

    async def fetch_detailed(self, url: str) -> FetchResult:
        self.requested.append(url)
        html = self.site.get(url)
        if html is None:
            return FetchResult(url, "permanent", http_status=404, error="HTTP 404")
        return FetchResult(url, "ok", text=html, http_status=200, size_bytes=len(html))

    async def aclose(self) -> None:
        return None


def claimed(url: str, kind: str) -> ClaimedUrl:
    return ClaimedUrl(
        id=1,
        source_id=1,
        source_slug="inc-ransom",
        collector="http",
        cycle_id=1,
        url=url,
        kind=kind,
        page_no=1 if kind == "listing" else None,
        depth=0 if kind == "listing" else 1,
        attempt=1,
        parent_id=None,
        content_sha256=None,
        failure_count=0,
        interval_seconds=900,
        deep_interval_seconds=21600,
        base_url=HOST,
    )


async def test_both_engines_spend_the_link_budget_on_tiles_first(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    listing = fixture("tile_listing.html")
    site = {LISTING_URL: listing, **{link: fixture("detail_page.html") for link in TILE_LINKS}}
    settings = make_settings(EXPOSURE_DETECTION=False, CRAWL_LINKS_PER_PAGE=3, CRAWL_LINK_DEPTH=1)

    legacy_collector = use_fake_collector(SiteCollector(site))
    legacy = DetailStorage()
    source = make_source(
        slug="inc-ransom", base_url=LISTING_URL, pagination_style="none", max_pages=1
    )
    await crawl_source(
        source, storage=legacy, settings=settings, extractor_name="rules", depth="deep"
    )
    followed = legacy_collector.requested[1:]
    assert followed == TILE_LINKS[:3]
    # Every followed victim page went to enrichment; none made a leak of its own.
    assert len(legacy.enriched) == 3
    assert len(legacy.upserted) == 9

    queue = DetailStorage()
    fetcher = CrawlFetcher(queue, settings, collectors={"http": SiteCollector(site)})  # type: ignore[arg-type]
    outcome = await fetcher(claimed(LISTING_URL, "listing"))
    # The queue takes candidates in this order, up to CRAWL_LINKS_PER_PAGE.
    assert [link.url for link in outcome.links][:3] == followed

    detail = await fetcher(claimed(TILE_LINKS[1], "link"))
    assert detail.kind == "ok"
    assert len(queue.enriched) == 1 and len(queue.upserted) == 9
    assert legacy.enriched[0][1] == queue.enriched[0][1]
