"""Listing summaries and incident types."""

from __future__ import annotations

from intel.extract.describe import (
    classify_fields,
    classify_incident,
    classify_text,
    clean_summary,
    ordered,
)
from intel.pipeline import extract_page

# The shape of a real LockBit listing page: link, prose, update stamp, view counter.
DOMAIN_FIRST_PAGE = """LockBit BLOG
Contact us
jackpotjunction.com
Cash in on big wins this tax season! Every hour one lucky winner will snag a payout.
Updated: 24 Apr, 2025, 11:09 UTC
179727
acimfunds.com
Managing capital on behalf of institutional investors and family offices worldwide.
Updated: 23 Apr, 2025, 11:08 UTC
175392
"""


def test_each_listing_gets_its_own_prose() -> None:
    leaks = extract_page(
        DOMAIN_FIRST_PAGE,
        source_group="lockbit",
        source_url=None,
        page_no=1,
        extractor_name="rules",
    )
    by_domain = {leak.victim_domain: leak for leak in leaks}

    assert by_domain["jackpotjunction.com"].summary == (
        "Cash in on big wins this tax season! Every hour one lucky winner will snag a payout."
    )


def test_page_furniture_is_stripped() -> None:
    window = "\nThe company makes turbines.\nUpdated: 24 Apr, 2025, 11:09 UTC\n179727\n12 GB\n"
    assert clean_summary(window + "Their plants are in three countries.") == (
        "The company makes turbines. Their plants are in three countries."
    )


def test_counters_badges_and_location_lines_are_stripped() -> None:
    window = (
        "\n👁️ views: 7716\npublication date: 2026-08-13\nPUBLISHED FULL\nUnited States\n"
        "Platinum Group manufactures industrial fasteners for the aerospace market.\n"
    )
    assert clean_summary(window) == (
        "Platinum Group manufactures industrial fasteners for the aerospace market."
    )


def test_the_victims_own_name_is_not_repeated_as_prose() -> None:
    window = "\nNorthwind Logistics\nNorthwind moves freight across the northern ports.\n"
    assert clean_summary(window, drop=("Northwind Logistics",)) == (
        "Northwind moves freight across the northern ports."
    )


def test_a_fragment_is_not_a_summary() -> None:
    assert clean_summary("\nSee more\n") is None


def test_a_long_listing_is_kept_whole() -> None:
    summary = clean_summary("word " * 800)
    assert summary is not None
    assert not summary.endswith("…")
    assert summary == ("word " * 800).strip()


def test_fields_alone_give_the_guaranteed_types() -> None:
    assert classify_fields(leak_type="ransomware", status="published", leak_size_bytes=10) == [
        "ransomware",
        "data_breach",
        "data_leak",
    ]
    assert classify_fields(leak_type="ransomware", status="sold", leak_size_bytes=None) == [
        "ransomware",
        "sale",
    ]
    assert classify_fields(leak_type="ransomware", status="countdown", leak_size_bytes=None) == [
        "ransomware",
        "extortion",
    ]


def test_an_unknown_status_adds_nothing() -> None:
    assert classify_fields(leak_type="ransomware", status="unknown", leak_size_bytes=None) == [
        "ransomware"
    ]


def test_the_listings_words_add_types() -> None:
    text = "We hacked their network and exfiltrated 200 GB. The database is for sale."
    assert classify_text(text) == ["data_breach", "hacked", "sale"]


def test_a_plain_company_description_adds_nothing() -> None:
    assert classify_text("A family-owned bakery serving the valley since 1962.") == []


def test_types_merge_in_canonical_order() -> None:
    assert classify_incident(
        summary="Customer passwords were dumped.",
        leak_type="ransomware",
        status="unknown",
        leak_size_bytes=None,
    ) == ["ransomware", "data_leak", "credential_leak"]


def test_unrecognised_types_sort_after_the_known_ones() -> None:
    assert ordered({"zeta", "sale", "ransomware"}) == ["ransomware", "sale", "zeta"]
