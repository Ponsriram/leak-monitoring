"""Listing pages laid out as repeated tiles: one tile, one victim.

The fixture `tile_listing.html` is modelled on the inc-ransom disclosure page, where the
whole-page linker merged four tiles into one leak — victim "Cristo Electric Association",
summary "Encrypted Proof Views Grupo Caberj Proof Views Rimrock Foundation …". Its tiles carry
names with no legal suffix and, for some, no domain: exactly the names the whole-page rules
refuse to accept as victims.
"""

# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

from pathlib import Path

import pytest
from test_pipeline_concurrency import (  # noqa: F401 - the autouse fixture must be in scope
    FakeStorage,
    make_settings,
    make_source,
    use_fake_collector,
)

from intel.collectors import (
    BLOCK_BREAK,
    RECORD_SEPARATOR,
    FetchResult,
    listing_blocks,
    to_text,
)
from intel.crawl.fetch import CrawlFetcher
from intel.crawl.queue import ClaimedUrl
from intel.models import ExtractedLeak
from intel.pipeline import crawl_source, extract_page, split_blocks
from intel.storage import UpsertResult

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "http://example.onion/blog/disclosure"

TILE_NAMES = [
    "Cristo Electric Association",
    "Grupo Caberj",
    "Rimrock Foundation",
    "Post Metal Recycling",
    "Tec Imports",
    "Wavecrest HFA",
    "Nordic Freight AB",
    "Harbor Dental Group",
    "Alpine Ridge School District",
]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def extract(text: str) -> list[ExtractedLeak]:
    return extract_page(
        text,
        source_group="inc-ransom",
        source_url="http://x.onion/",
        page_no=1,
        extractor_name="rules",
    )


@pytest.fixture(scope="module")
def tile_leaks() -> list[ExtractedLeak]:
    return extract(to_text(fixture("tile_listing.html")))


# ---------------------------------------------------------------- tiles


def test_every_tile_is_one_leak_with_its_own_name(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    assert [leak.victim_name for leak in tile_leaks] == TILE_NAMES


def test_names_without_a_suffix_or_a_domain_are_still_victims(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    by_name = {leak.victim_name: leak for leak in tile_leaks}
    assert by_name["Grupo Caberj"].victim_domain is None
    assert by_name["Post Metal Recycling"].victim_domain is None
    assert by_name["Wavecrest HFA"].victim_domain == "wavecresthfa.com"


def test_no_summary_carries_another_tiles_text(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    for leak in tile_leaks:
        others = [name for name in TILE_NAMES if name != leak.victim_name]
        assert not any(name in (leak.summary or "") for name in others), leak
        # The icon labels and counters are chrome, never a description.
        assert "Proof Views" not in (leak.summary or "")


def test_tile_details_are_captured(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    by_name = {leak.victim_name: leak for leak in tile_leaks}
    cristo = by_name["Cristo Electric Association"]
    assert cristo.victim_domain == "cristoelectric.org"
    assert cristo.summary is not None
    assert cristo.summary.startswith("Revenue: $14M Rural electric cooperative")
    assert cristo.leak_size_bytes == 412 * 1024**3

    tec = by_name["Tec Imports"]
    assert tec.published_at is not None and tec.published_at.date().isoformat() == "2026-09-14"
    assert "industrial machinery" in (tec.summary or "")
    # Its ccTLD, not the "GB" of "61 GB".
    assert tec.victim_country == "Brazil"

    assert by_name["Harbor Dental Group"].status.value == "negotiating"


def test_flags_given_as_markup_or_emoji_set_the_country(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    countries = {leak.victim_name: leak.victim_country for leak in tile_leaks}
    assert countries["Cristo Electric Association"] == "United States"  # flags/us.svg
    assert countries["Grupo Caberj"] == "Brazil"  # emoji
    assert countries["Rimrock Foundation"] == "United States"  # class="fi fi-us"
    assert countries["Post Metal Recycling"] == "Canada"  # alt text
    assert countries["Wavecrest HFA"] == "United Kingdom"


def test_tiles_are_tagged_as_tile_extractions(tile_leaks) -> None:  # type: ignore[no-untyped-def]
    assert {leak.extraction.model_dump().get("mode") for leak in tile_leaks} == {"tile"}


def test_a_countdown_inside_a_tile_is_still_stripped() -> None:
    text = to_text(fixture("tile_listing.html"))
    assert "2d 04h 11m" not in text


def test_text_outside_the_tiles_is_kept_but_makes_no_leaks() -> None:
    text = to_text(fixture("tile_listing.html"))
    parts = text.split(BLOCK_BREAK)
    assert "Companies that chose not to cooperate" in parts[0]
    assert "Our mirror" in parts[-1]
    assert len(split_blocks(text) or []) == len(TILE_NAMES)


def test_nav_links_are_never_blocks() -> None:
    for name in ("tile_listing.html", "nav_page.html"):
        for block in listing_blocks(fixture(name)):
            assert block.tag not in ("nav", "li")
            assert "Contact" not in block.text()


# ---------------------------------------------------------------- other layouts


def test_a_table_listing_is_one_leak_per_row_and_the_header_row_is_none() -> None:
    leaks = extract(to_text(fixture("table_listing.html")))
    assert [(leak.victim_name, leak.victim_domain) for leak in leaks] == [
        ("Bayside Plumbing Supply", "baysideplumbing.com"),
        ("Kestrel Aviation Services", "kestrelaviation.com"),
        ("Marlow & Finch Solicitors", "marlowfinch.co.uk"),
        ("Vela Biotech", "velabiotech.de"),
        ("Copperline Credit Union", "copperlinecu.org"),
    ]
    assert leaks[3].leak_size_bytes == 310 * 1024**3


@pytest.mark.parametrize("name", ["no_structure.html", "nav_page.html"])
def test_a_page_without_repeated_structure_is_byte_identical(name: str) -> None:
    html = fixture(name)
    assert listing_blocks(html) == []
    assert to_text(html) == to_text(html, segment=False)
    assert RECORD_SEPARATOR not in to_text(html)


def test_a_page_without_repeated_structure_extracts_as_before() -> None:
    text = to_text(fixture("no_structure.html"))
    names = [leak.victim_name for leak in extract(text)]
    assert names == ["Northwind Logistics", "Contoso Manufacturing Ltd", "Fabrikam Inc"]
    assert all(leak.extraction.model_dump().get("mode") is None for leak in extract(text))


def test_a_grid_of_rows_is_split_into_its_tiles() -> None:
    def card(n: int) -> str:
        return (
            f'<div class="col"><div class="card"><h3>Victim {n} Company</h3>'
            f"<p>victim{n}.example</p></div></div>"
        )

    rows = "".join(
        '<div class="row">' + "".join(card(r * 4 + c) for c in range(count)) + "</div>"
        for r, count in enumerate([4, 4, 1])
    )
    leaks = extract(to_text(f'<body><div class="container">{rows}</div></body>'))
    assert [leak.victim_name for leak in leaks] == [f"Victim {n} Company" for n in range(9)]
    assert leaks[8].victim_domain == "victim8.example"


def test_a_cards_own_fields_are_not_split_into_tiles() -> None:
    def card(n: int) -> str:
        fields = [
            ("Name:", f"Victim {n} Holdings"),
            ("Website:", f"v{n}.example"),
            ("Revenue:", "$5M"),
        ]
        inner = "".join(
            f'<div class="field"><span>{label}</span><span>{value}</span></div>'
            for label, value in fields
        )
        return f'<div class="card">{inner}</div>'

    html = f'<body><div class="list">{"".join(card(n) for n in range(4))}</div></body>'
    leaks = extract(to_text(html))
    assert [leak.victim_name for leak in leaks] == [f"Victim {n} Holdings" for n in range(4)]
    assert [leak.victim_domain for leak in leaks] == [f"v{n}.example" for n in range(4)]


def test_a_paragraph_first_tile_has_no_name_line() -> None:
    blurb = "We have downloaded everything from this company and will publish it in full soon."
    tiles = "".join(f'<div class="post"><p>{blurb}</p><p>x{n}.example</p></div>' for n in range(3))
    leaks = extract(to_text(f"<body><div>{tiles}</div></body>"))
    # No sentence becomes a name; the domain still identifies each victim.
    assert [(leak.victim_name, leak.victim_domain) for leak in leaks] == [
        (None, f"x{n}.example") for n in range(3)
    ]


# ---------------------------------------------------------------- item_selector


def test_an_item_selector_overrides_detection() -> None:
    html = fixture("tile_listing.html")
    # Only the tiles' heads: no domains, no panels.
    blocks = listing_blocks(html, item_selector="div.blog__card-head")
    assert len(blocks) == len(TILE_NAMES)
    leaks = extract(to_text(html, item_selector="div.blog__card-head"))
    assert [leak.victim_name for leak in leaks] == TILE_NAMES
    assert all(leak.victim_domain is None for leak in leaks)


def test_an_item_selector_that_matches_nothing_falls_back_to_the_whole_page() -> None:
    html = fixture("no_structure.html")
    assert to_text(html, item_selector="div.nope") == to_text(html, segment=False)


def test_a_bad_item_selector_does_not_fail_the_page() -> None:
    html = fixture("tile_listing.html")
    assert to_text(html, item_selector="div[[[") == to_text(html, segment=False)


def test_a_page_cannot_fake_a_block_boundary() -> None:
    spoof = f"<body><p>Acme Corp {RECORD_SEPARATOR} acme.example</p><p>Contact</p></body>"
    text = to_text(spoof)
    assert RECORD_SEPARATOR not in text
    assert split_blocks(text) is None


# ---------------------------------------------------------------- both crawl engines


class RecordingStorage(FakeStorage):
    """`FakeStorage` that keeps the leaks it was asked to upsert."""

    def __init__(self) -> None:
        super().__init__()
        self.leaks: list[ExtractedLeak] = []

    async def upsert_leaks(self, leaks, *, source_id):  # type: ignore[no-untyped-def]
        self.leaks.extend(leaks)
        return UpsertResult(inserted=len(leaks))


class OnePageCollector:
    name = "http"

    def __init__(self, html: str) -> None:
        self._html = html
        self.last_error: str | None = None

    async def fetch(self, url: str) -> str | None:
        return self._html

    async def fetch_detailed(self, url: str) -> FetchResult:
        return FetchResult(url, "ok", text=self._html, http_status=200, size_bytes=len(self._html))

    async def aclose(self) -> None:
        return None


def claimed(kind: str = "listing", item_selector: str | None = None) -> ClaimedUrl:
    return ClaimedUrl(
        id=1,
        source_id=1,
        source_slug="inc-ransom",
        collector="http",
        cycle_id=None,
        url=LISTING_URL,
        kind=kind,
        page_no=1 if kind == "listing" else None,
        depth=0,
        attempt=1,
        parent_id=None,
        content_sha256=None,
        failure_count=0,
        interval_seconds=900,
        deep_interval_seconds=21600,
        base_url="http://example.onion",
        item_selector=item_selector,
    )


async def test_both_crawl_engines_extract_the_same_tiles(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    html = fixture("tile_listing.html")
    settings = make_settings(EXPOSURE_DETECTION=False, CRAWL_FOLLOW_LINKS=False)

    use_fake_collector(OnePageCollector(html))
    legacy = RecordingStorage()
    source = make_source(
        slug="inc-ransom", base_url=LISTING_URL, pagination_style="none", max_pages=1
    )
    await crawl_source(source, storage=legacy, settings=settings, extractor_name="rules")

    queue = RecordingStorage()
    fetcher = CrawlFetcher(queue, settings, collectors={"http": OnePageCollector(html)})  # type: ignore[arg-type]
    outcome = await fetcher(claimed())

    assert outcome.kind == "ok"
    assert [leak.victim_name for leak in queue.leaks] == TILE_NAMES
    assert [leak.model_dump() for leak in queue.leaks] == [
        leak.model_dump() for leak in legacy.leaks
    ]
    # And the text both stored, so both hash it the same.
    assert queue.saved[0][1] == legacy.saved[0][1]


async def test_both_engines_honour_the_item_selector(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    html = fixture("tile_listing.html")
    settings = make_settings(EXPOSURE_DETECTION=False, CRAWL_FOLLOW_LINKS=False)
    selector = "div.blog__card-head"

    use_fake_collector(OnePageCollector(html))
    legacy = RecordingStorage()
    source = make_source(pagination_style="none", max_pages=1, item_selector=selector)
    await crawl_source(source, storage=legacy, settings=settings, extractor_name="rules")

    queue = RecordingStorage()
    fetcher = CrawlFetcher(queue, settings, collectors={"http": OnePageCollector(html)})  # type: ignore[arg-type]
    await fetcher(claimed(item_selector=selector))

    assert queue.saved[0][1] == legacy.saved[0][1] == to_text(html, item_selector=selector)
