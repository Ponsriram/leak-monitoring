"""Linker tests — the logic that had five divergent copies and no test.

Each case pins down a behaviour the old order-dependent implementation got wrong or needed
its `orphan_entries` machinery to work around.
"""

from __future__ import annotations

from datetime import UTC, datetime

from intel.extract.linker import Label, Span, link_spans
from intel.models import LeakStatus


def span(label: str, text: str, start: int = 0) -> Span:
    return Span(label=label, text=text, start=start, end=start + len(text), confidence=0.9)


def test_links_attributes_to_the_preceding_victim() -> None:
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.VICTIM_URL, "northwind.example", 30),
            span(Label.DATE, "2026-02-10", 60),
            span(Label.SIZE, "1.2 TB", 80),
        ],
        source_group="lockbit",
    )

    assert len(leaks) == 1
    leak = leaks[0]
    assert leak.victim_name == "Northwind Logistics"
    assert leak.victim_domain == "northwind.example"
    assert leak.published_at == datetime(2026, 2, 10, tzinfo=UTC)
    assert leak.leak_size_bytes == int(1.2 * 1024**4)


def test_url_printed_above_its_victim_binds_to_that_victim() -> None:
    """The layout that silently misfiled every listing on a page.

    Termite prints the victim's link on the line above the company name. Under
    "attributes attach to the preceding victim", each record took the *next* company's
    domain — and since `dedupe_hash` is built from `victim_domain`, every victim was filed
    under another company's identity and domain alerts would have fired for the wrong one.
    """
    leaks = link_spans(
        [
            span(Label.VICTIM_URL, "affiniahealthcare.org", 0),
            span(Label.VICTIM, "Affinia Healthcare", 25),
            span(Label.VICTIM_URL, "jdyoung.com", 50),
            span(Label.VICTIM, "JD Young", 65),
            span(Label.VICTIM_URL, "calfresh.ca.gov", 80),
            span(Label.VICTIM, "Cal Fresh", 100),
        ],
        source_group="termite",
    )

    assert [(leak.victim_name, leak.victim_domain) for leak in leaks] == [
        ("Affinia Healthcare", "affiniahealthcare.org"),
        ("JD Young", "jdyoung.com"),
        ("Cal Fresh", "calfresh.ca.gov"),
    ]


def test_trailing_prose_does_not_drag_a_url_to_the_next_victim() -> None:
    """The URL still belongs to the name it sits next to, not the one it reads before."""
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.VICTIM_URL, "northwind.example", 25),
            # A long description, then the next listing.
            span(Label.VICTIM, "Contoso Manufacturing", 300),
            span(Label.VICTIM_URL, "contoso.example", 330),
        ],
        source_group="lockbit",
    )

    assert [(leak.victim_name, leak.victim_domain) for leak in leaks] == [
        ("Northwind Logistics", "northwind.example"),
        ("Contoso Manufacturing", "contoso.example"),
    ]


def test_a_bare_legal_suffix_is_not_used_as_the_victim_name() -> None:
    """"Financial" is a table heading, not eight different companies.

    The record survives — its domain identifies the victim — but the misleading label is
    dropped. Suppressing the record instead would strand the domain on a neighbouring
    victim, which is the worse of the two errors.
    """
    leaks = link_spans(
        [
            span(Label.VICTIM, "Financial", 0),
            span(Label.VICTIM_URL, "stellarrad.com", 20),
        ],
        source_group="5butbkrljkao",
    )

    assert len(leaks) == 1
    assert leaks[0].victim_name is None
    assert leaks[0].victim_domain == "stellarrad.com"


def test_a_real_name_containing_a_suffix_is_kept() -> None:
    leaks = link_spans(
        [
            span(Label.VICTIM, "Fabrikam Health GmbH", 0),
            span(Label.VICTIM_URL, "fabrikam.example", 30),
        ],
        source_group="lockbit",
    )

    assert leaks[0].victim_name == "Fabrikam Health GmbH"


def test_separates_consecutive_victims() -> None:
    """A new victim span opens a new record; attributes must not bleed across."""
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.DATE, "2026-02-10", 30),
            span(Label.VICTIM, "Contoso Manufacturing", 60),
            span(Label.DATE, "2026-03-15", 90),
        ],
        source_group="lockbit",
    )

    assert len(leaks) == 2
    assert leaks[0].victim_name == "Northwind Logistics"
    assert leaks[0].published_at == datetime(2026, 2, 10, tzinfo=UTC)
    assert leaks[1].victim_name == "Contoso Manufacturing"
    assert leaks[1].published_at == datetime(2026, 3, 15, tzinfo=UTC)


def test_attributes_before_the_first_victim_still_attach() -> None:
    """Some sites print the date above the company name."""
    leaks = link_spans(
        [
            span(Label.DATE, "2026-02-10", 0),
            span(Label.VICTIM, "Northwind Logistics", 20),
        ],
        source_group="lockbit",
    )

    assert len(leaks) == 1
    assert leaks[0].published_at == datetime(2026, 2, 10, tzinfo=UTC)


def test_actor_group_defaults_to_the_source() -> None:
    """We know which site we crawled — the group never needs guessing from prose."""
    leaks = link_spans([span(Label.VICTIM, "Northwind Logistics")], source_group="lockbit")
    assert leaks[0].actor_group == "lockbit"


def test_group_span_overrides_the_source() -> None:
    """Some sites republish other crews' listings."""
    leaks = link_spans(
        [
            span(Label.GROUP, "BlackCat", 0),
            span(Label.VICTIM, "Northwind Logistics", 20),
        ],
        source_group="lockbit",
    )
    assert leaks[0].actor_group == "blackcat"


def test_group_is_slugified() -> None:
    leaks = link_spans(
        [span(Label.GROUP, "  LockBit 3.0 ", 0), span(Label.VICTIM, "Acme", 20)],
        source_group="ignored",
    )
    assert leaks[0].actor_group == "lockbit-3.0"


def test_records_without_victim_identity_are_dropped() -> None:
    """They cannot be deduplicated — every nameless record would hash identically."""
    leaks = link_spans(
        [span(Label.DATE, "2026-02-10"), span(Label.SIZE, "1 TB")],
        source_group="lockbit",
    )
    assert leaks == []


def test_bare_url_listing_is_kept() -> None:
    """A listing with a URL but no company name is still a real leak."""
    leaks = link_spans(
        [span(Label.VICTIM_URL, "northwind.example", 0), span(Label.DATE, "2026-02-10", 30)],
        source_group="lockbit",
    )
    assert len(leaks) == 1
    assert leaks[0].victim_domain == "northwind.example"


def test_first_url_wins_over_later_mirrors() -> None:
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.VICTIM_URL, "northwind.example", 30),
            span(Label.VICTIM_URL, "mirror.example", 60),
        ],
        source_group="lockbit",
    )
    assert leaks[0].victim_domain == "northwind.example"


def test_status_is_parsed_into_the_enum() -> None:
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.STATUS, "countdown", 30),
        ],
        source_group="lockbit",
    )
    assert leaks[0].status is LeakStatus.COUNTDOWN


def test_missing_date_leaves_published_at_none() -> None:
    """Nullable, not epoch-zero: plenty of listings genuinely omit a date."""
    leaks = link_spans([span(Label.VICTIM, "Northwind Logistics")], source_group="lockbit")
    assert leaks[0].published_at is None
    assert leaks[0].status is LeakStatus.UNKNOWN


def test_raw_date_text_is_preserved_for_audit() -> None:
    leaks = link_spans(
        [
            span(Label.VICTIM, "Northwind Logistics", 0),
            span(Label.DATE, "sometime in 2026", 30),
        ],
        source_group="lockbit",
    )
    # Unparseable, so published_at is None — but the original text survives so a bad parse
    # can be diagnosed rather than guessed at.
    assert leaks[0].published_at is None
    assert leaks[0].published_at_raw == "sometime in 2026"
