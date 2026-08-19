"""Page wave planning.

The property that matters is the pair of bounds: reaching page P must cost O(log P) waves,
and no plan may request more than about twice the pages a listing actually has. Everything
else here is guarding the edges that a doubling loop gets wrong — a one-page source, a
source with `max_pages` of zero, and the wave that lands exactly on the last page.
"""

from __future__ import annotations

import math

from intel.scheduling import page_waves


def plan(max_pages: int, *, width: int = 4, cap: int = 16) -> list[list[int]]:
    return list(page_waves(max_pages, width=width, cap=cap))


def test_page_one_is_always_alone() -> None:
    """Page 1 decides reachability, failover and challenge detection before any speculation."""
    assert plan(10)[0] == [1]
    assert plan(1) == [[1]]


def test_waves_double() -> None:
    assert plan(31, width=2) == [
        [1],
        [2, 3],
        [4, 5, 6, 7],
        [8, 9, 10, 11, 12, 13, 14, 15],
        list(range(16, 32)),
    ]


def test_every_page_is_planned_exactly_once_and_in_order() -> None:
    for max_pages in range(1, 60):
        pages = [page for wave in plan(max_pages) for page in wave]
        assert pages == list(range(1, max_pages + 1))


def test_the_last_wave_is_truncated_to_max_pages() -> None:
    """A doubling wave must never plan a page the source said it does not have."""
    assert plan(6, width=4) == [[1], [2, 3, 4, 5], [6]]


def test_wave_count_is_logarithmic() -> None:
    """The whole point: sequential round trips grow with log P, not P."""
    waves = plan(1000, width=4, cap=1000)
    assert len(waves) <= 2 + math.ceil(math.log2(1000))


def test_a_wave_never_exceeds_the_cap() -> None:
    """A burst of simultaneous requests at one onion service has to stay bounded."""
    assert all(len(wave) <= 8 for wave in plan(200, width=2, cap=8))


def test_zero_pages_plans_nothing() -> None:
    assert plan(0) == []


def test_width_below_one_is_treated_as_one() -> None:
    assert plan(3, width=0) == [[1], [2], [3]]
