"""Interstitial detection — the pages that answer 200 and are not a listing."""

from __future__ import annotations

import pytest

from intel.pipeline import gate_kind


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # Each of these is the shape of a real stored page 1 from a monitored source.
        (
            "TorZon\nYou have been placed in a queue, awaiting forwarding to the platform.",
            "access queue",
        ),
        ("CL0P^_- LEAKS\nYou have been placed in a queue, awaiting forwarding to", "access queue"),
        ("Blackout Gateway\nWe are checking that you are human, stay with us", "human check"),
        ("Under Maintenance\nSystem maintenance is currently underway.", "maintenance"),
        ("Welcome to\nThe Secret Garden\n.\nLog in\nSign up\nlight mode", "login wall"),
    ],
)
def test_interstitials_are_recognised(text: str, kind: str) -> None:
    assert gate_kind(text) == kind


def test_a_real_listing_is_not_a_gate() -> None:
    listing = (
        "Login\n"  # plenty of leak sites link a login in their header
        + "\n".join(
            f"victim{n}.example.com\nA regional manufacturer of industrial valves.\n"
            f"Updated: 24 Apr, 2025, 11:09 UTC"
            for n in range(20)
        )
    )
    assert gate_kind(listing) is None


def test_a_short_page_without_gate_wording_is_left_to_the_other_checks() -> None:
    assert gate_kind("Northwind Logistics\nnorthwind.example\n12 GB\npublished") is None
