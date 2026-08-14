"""Normalizer tests.

`parse_date` is the highest-value function in the codebase to get right: storing dates as
free text is what made the old weekly chart return an empty array for months.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intel.extract.normalize import (
    extract_domain,
    parse_date,
    parse_size,
    parse_status,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-02-10", datetime(2026, 2, 10, tzinfo=UTC)),
        ("10 Feb 2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("10 Feb, 2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("Feb 10, 2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("February 10, 2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("10.02.2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("10/02/2026", datetime(2026, 2, 10, tzinfo=UTC)),
        ("2026-02-10 14:30:00", datetime(2026, 2, 10, 14, 30, tzinfo=UTC)),
        # Decoration these sites wrap around dates
        ("Published: 2026-02-10", datetime(2026, 2, 10, tzinfo=UTC)),
        ("[2026-02-10]", datetime(2026, 2, 10, tzinfo=UTC)),
        ("2026-02-10 14:30 (UTC)", datetime(2026, 2, 10, 14, 30, tzinfo=UTC)),
    ],
)
def test_parses_real_world_date_formats(raw: str, expected: datetime) -> None:
    assert parse_date(raw) == expected


def test_parsed_dates_are_timezone_aware() -> None:
    """A naive datetime would be read in the server's zone and silently shift every date."""
    result = parse_date("2026-02-10")
    assert result is not None
    assert result.tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "   ", "sometime last week", "n/a", "TBA"])
def test_unparseable_dates_return_none_rather_than_guessing(raw: str | None) -> None:
    assert parse_date(raw) is None


def test_ambiguous_dates_prefer_day_first() -> None:
    """These sites are overwhelmingly European/Russian; 03/04 is 3 April, not 4 March."""
    result = parse_date("03/04/2026")
    assert result == datetime(2026, 4, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2 TB", int(1.2 * 1024**4)),
        ("500 GB", 500 * 1024**3),
        ("500GB", 500 * 1024**3),
        ("2 TiB", 2 * 1024**4),
        ("750 MB", 750 * 1024**2),
        # Decimal comma, common in European listings
        ("1,5 GB", int(1.5 * 1024**3)),
        ("Size: 340 GB of data", 340 * 1024**3),
    ],
)
def test_parses_sizes(raw: str, expected: int) -> None:
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "unknown", "lots of files"])
def test_missing_size_is_none_not_zero(raw: str | None) -> None:
    """None means 'not stated'. Zero would mean 'an empty leak', which is a different claim."""
    assert parse_size(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Data published", "published"),
        ("FULL DUMP RELEASED", "published"),
        ("countdown: 3 days left", "countdown"),
        ("Time left: 14:22:01", "countdown"),
        ("Sold to a private buyer", "sold"),
        ("Files removed after payment", "removed"),
        ("", "unknown"),
        (None, "unknown"),
        ("some unrelated text", "unknown"),
    ],
)
def test_parses_status(raw: str | None, expected: str) -> None:
    assert parse_status(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Visit northwind.example for details", "northwind.example"),
        ("https://www.contoso.example/about", "contoso.example"),
        ("contact: mail@fabrikam.example", "fabrikam.example"),
    ],
)
def test_extracts_victim_domain(text: str, expected: str) -> None:
    assert extract_domain(text) == expected


def test_ignores_onion_and_infrastructure_domains() -> None:
    """The leak site's own address and its contact links are not victims."""
    text = (
        "Contact us at lockbitapt.onion or protonmail.com, "
        "victim site is northwind.example"
    )
    assert extract_domain(text) == "northwind.example"


def test_no_domain_returns_none() -> None:
    assert extract_domain("no domains here at all") is None
