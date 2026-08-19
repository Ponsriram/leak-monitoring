"""Country and sector tagging.

The tests that earn their place here are the negative ones. A country in `victim_country` is
indistinguishable from a correct one once written, and analysts filter on it — so the cost
of inferring `.io` as the British Indian Ocean Territory is a filter that quietly lies,
which is worse than a column full of nulls.
"""

from __future__ import annotations

from intel.extract.gazetteer import (
    country_from_domain,
    is_country_name,
    parse_country,
    parse_sector,
    resolve_country,
    resolve_sector,
)

# ---------------------------------------------------------------- countries


def test_aliases_normalize_to_one_canonical_name() -> None:
    """The dashboard's filter shows one entry per country, not one per spelling."""
    for spelling in ("USA", "u.s.a.", "United States of America", "american"):
        assert parse_country(spelling) == "United States"


def test_a_country_inside_a_phrase_is_found() -> None:
    assert parse_country("Country: Germany") == "Germany"
    assert parse_country("headquartered in the Netherlands") == "Netherlands"


def test_the_longest_alias_wins() -> None:
    """Alternation is first-match, so "united states" must not shadow the longer name."""
    assert parse_country("United States of America") == "United States"


def test_text_naming_no_country_returns_none() -> None:
    assert parse_country("Northwind Logistics Ltd") is None
    assert parse_country("") is None
    assert parse_country(None) is None


def test_cctlds_map_to_their_country() -> None:
    assert country_from_domain("acme.de") == "Germany"
    assert country_from_domain("acme.co.uk") == "United Kingdom"
    assert country_from_domain("acme.com.au") == "Australia"


def test_globally_sold_cctlds_infer_nothing() -> None:
    """`.io` is a country code that is sold worldwide; reading it as a location is a lie."""
    for domain in ("startup.io", "brand.co", "model.ai", "show.tv", "handle.me"):
        assert country_from_domain(domain) is None


def test_generic_tlds_infer_nothing() -> None:
    for domain in ("acme.com", "acme.org", "acme.net", "acme"):
        assert country_from_domain(domain) is None


def test_explicit_text_beats_the_domain() -> None:
    """A `.com` German company is ordinary; the page knows what the TLD cannot."""
    assert resolve_country(["Germany"], domain="acme.com") == "Germany"


def test_the_domain_is_the_fallback() -> None:
    assert resolve_country([], domain="acme.fr") == "France"
    assert resolve_country(["nothing here"], domain="acme.fr") == "France"


def test_a_listing_with_neither_gets_no_country() -> None:
    assert resolve_country([], domain="acme.com") is None


def test_country_names_are_recognised_as_such() -> None:
    """This is what keeps "United States" out of the victim column."""
    assert is_country_name("United States")
    assert is_country_name("germany")
    assert not is_country_name("Northwind Medical Group")
    assert not is_country_name(None)


# ---------------------------------------------------------------- sectors


def test_a_victim_name_carries_its_sector() -> None:
    assert parse_sector("Northwind Medical Group") == "Healthcare"
    assert parse_sector("Fairview Unified School District") == "Education"
    assert parse_sector("Coastal Freight & Haulage") == "Transportation & Logistics"


def test_a_name_with_no_industry_word_gets_no_sector() -> None:
    assert parse_sector("Northwind Ltd") is None
    assert parse_sector(None) is None


def test_the_best_supported_sector_wins() -> None:
    """A name that hits two sectors is decided by the rest of the listing, not by order."""
    assert (
        resolve_sector(["Northwind Medical Transport", "patient records", "clinic"])
        == "Healthcare"
    )


def test_sector_resolution_is_deterministic_on_a_tie() -> None:
    """Same input, same answer — otherwise a re-crawl rewrites the column at random."""
    candidates = ["Acme Medical Logistics"]
    assert resolve_sector(candidates) == resolve_sector(candidates)
