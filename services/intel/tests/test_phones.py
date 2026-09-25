"""The scam-report phone number regex, its rejections, and the classifiers."""

from __future__ import annotations

import pytest

from intel.extract.phones import (
    INDIAN_MOBILE,
    classify_audience,
    classify_threats,
    find_numbers,
    has_phone_context,
)


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        # Real reports, as posted.
        ("had a call from a scammer on 0208 540 2764 today", "en", ["+442085402764"]),
        ("A scammer who phoned on 004520333365", "en", ["+4520333365"]),
        ("gebeld vanaf een 06 nummer: +31-6-34115189", "nl", ["+31634115189"]),
        ("call back on 779.222.0815 or +1 (775) 208-9213", "en", ["+17792220815", "+17752089213"]),
        ("Call me at (202) 555-0147", "en", ["+12025550147"]),
        # The Stack Overflow Indian mobile formats.
        ("text from 9876543210 about my SBI account", "en", ["+919876543210"]),
        ("whatsapp +91 98765 43210", "en", ["+919876543210"]),
        ("call 09876543211 now", "hi", ["+919876543211"]),
        # A UK mobile written with its trunk 0 fits the Indian pattern's shape too, and is
        # not Indian.
        ("scam call from 07371 303 644 claiming to be BT", "en", ["+447371303644"]),
        ("call 919876543212 now", "en", ["+919876543212"]),
        # Same number twice in one post is one number.
        ("+1 (775) 208-9213 and again 775.208.9213", "en", ["+17752089213"]),
    ],
)
def test_real_numbers_are_found(text: str, language: str, expected: list[str]) -> None:
    assert [n.e164 for n in find_numbers(text, language=language)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Top malicious IPs 158.94.208.177 and 91.92.241.87",
        "posted 2026-09-18 09:40 and 18/09/2026",
        "update to version 1.2.3 or 10.4.12",
        "urldna.io/scan/6aad43f53b77500 009866051",
        "order #123456789 shipped",
        "The Senate did 973061794562 things",
    ],
)
def test_things_shaped_like_numbers_are_not_numbers(text: str) -> None:
    assert find_numbers(text, language="en") == []


def test_the_indian_mobile_pattern_matches_the_answers_formats() -> None:
    for value in ("9876543210", "09876543210", "919876543210", "+919876543210"):
        assert INDIAN_MOBILE.match(value), value
    for value in ("5876543210", "98765432", "19876543210"):
        assert not INDIAN_MOBILE.match(value), value


def test_found_numbers_carry_country_and_line_type() -> None:
    (number,) = find_numbers("scam call from +31-6-34115189", language="nl")
    assert number.country == "Netherlands"
    assert number.region_code == "NL"
    assert number.line_type == "mobile"
    assert number.display == "+31 6 34115189"


def test_context_is_required_to_count_as_a_report() -> None:
    assert has_phone_context("they called me from this number")
    assert has_phone_context("Werd gebeld vanaf")
    assert not has_phone_context("price was 2085402764 dollars")


def test_classifiers_are_never_empty() -> None:
    assert classify_threats("something odd happened") == ["Phone Scam"]
    assert classify_audience("something odd happened") == ["General Public"]


def test_classifiers_read_the_reports_words() -> None:
    text = "Got an SMS from my bank saying my card is blocked, asked for the OTP"
    assert classify_threats(text) == ["Smishing", "Bank Impersonation", "Credential Harvesting"]
    assert classify_audience(text) == ["Bank Customers"]
