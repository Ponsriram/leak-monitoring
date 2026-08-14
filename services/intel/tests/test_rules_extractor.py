"""End-to-end extraction over a realistic leak-site page.

This is the fixture-driven test the old pipeline never had: page text in, expected leaks
out, no database and no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intel.extract import RulesExtractor
from intel.models import LeakStatus
from intel.pipeline import extract_page

FIXTURE = Path(__file__).parent / "fixtures" / "sample_leak_page.txt"


@pytest.fixture(scope="module")
def leaks():  # type: ignore[no-untyped-def]
    text = FIXTURE.read_text(encoding="utf-8")
    return extract_page(
        text,
        source_group="lockbit",
        source_url="http://example.onion/",
        page_no=1,
        extractor_name="rules",
    )


def test_finds_every_victim_and_nothing_else(leaks) -> None:  # type: ignore[no-untyped-def]
    """The page lists three victims. A page header is not a fourth."""
    names = [leak.victim_name for leak in leaks]
    assert len(leaks) == 3, f"expected 3 victims, got {names}"
    assert "Northwind Logistics" in names
    assert any("Contoso" in (name or "") for name in names)
    assert any("Fabrikam" in (name or "") for name in names)


def test_page_header_is_not_treated_as_a_victim(leaks) -> None:  # type: ignore[no-untyped-def]
    """'LOCKBIT LEAKED DATA' is a banner: shouty, and contains a site-chrome word."""
    for leak in leaks:
        assert "LEAKED" not in (leak.victim_name or "")


def test_domains_attach_to_the_right_victim(leaks) -> None:  # type: ignore[no-untyped-def]
    by_name = {leak.victim_name: leak for leak in leaks}
    assert by_name["Northwind Logistics"].victim_domain == "northwind.example"


def test_dates_are_parsed_from_mixed_formats(leaks) -> None:  # type: ignore[no-untyped-def]
    """The fixture deliberately mixes 'Published: 2026-02-10' and '15.04.2026'."""
    dates = {leak.victim_name: leak.published_at for leak in leaks}
    assert dates["Northwind Logistics"] == datetime(2026, 2, 10, tzinfo=UTC)
    fabrikam = next(v for k, v in dates.items() if k and "Fabrikam" in k)
    assert fabrikam == datetime(2026, 4, 15, tzinfo=UTC)


def test_sizes_including_decimal_comma(leaks) -> None:  # type: ignore[no-untyped-def]
    by_name = {leak.victim_name: leak for leak in leaks}
    assert by_name["Northwind Logistics"].leak_size_bytes == int(1.2 * 1024**4)
    fabrikam = next(v for k, v in by_name.items() if "Fabrikam" in k)
    # "2,5 TB" — decimal comma, not a thousands separator.
    assert fabrikam.leak_size_bytes == int(2.5 * 1024**4)


def test_statuses(leaks) -> None:  # type: ignore[no-untyped-def]
    by_name = {leak.victim_name: leak for leak in leaks}
    assert by_name["Northwind Logistics"].status is LeakStatus.PUBLISHED
    fabrikam = next(v for k, v in by_name.items() if "Fabrikam" in k)
    assert fabrikam.status is LeakStatus.SOLD


def test_actor_group_comes_from_the_source(leaks) -> None:  # type: ignore[no-untyped-def]
    assert {leak.actor_group for leak in leaks} == {"lockbit"}


def test_contact_and_onion_domains_are_not_victims(leaks) -> None:  # type: ignore[no-untyped-def]
    """The fixture ends with protonmail.com and the crew's own .onion mirror."""
    domains = {leak.victim_domain for leak in leaks}
    assert "protonmail.com" not in domains
    assert not any((domain or "").endswith(".onion") for domain in domains)


def test_every_extracted_leak_is_usable(leaks) -> None:  # type: ignore[no-untyped-def]
    """Unusable rows would all hash identically and poison dedupe."""
    assert all(leak.is_usable for leak in leaks)


def test_dedupe_hashes_are_distinct(leaks) -> None:  # type: ignore[no-untyped-def]
    assert len({leak.dedupe_hash for leak in leaks}) == len(leaks)


def test_extractor_is_deterministic() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    extractor = RulesExtractor()
    assert [(s.label, s.text) for s in extractor.extract(text)] == [
        (s.label, s.text) for s in extractor.extract(text)
    ]


def test_handles_empty_and_junk_input() -> None:
    """Extractors must never raise on messy input — pages are messy."""
    extractor = RulesExtractor()
    for text in ["", "   ", "\x00\x01\x02", "a" * 10_000]:
        assert isinstance(extractor.extract(text), list)
