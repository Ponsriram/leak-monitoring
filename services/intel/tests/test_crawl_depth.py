"""The shallow-probe / deep-walk split.

New victims are appended to the front of a leak-site listing, so page 1 answers "is there
anything new?" on its own. Walking all ten pages to find that out costs ten Tor round trips
at twenty to thirty seconds each, and the content-hash short circuit then discards nine of
them — the fetches still happened, they just produced nothing.

These tests pin the two halves: that the schedule picks the right depth, and that a shallow
crawl really does stop at one page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# Absolute, not relative: `tests/` has no `__init__.py`, so pytest puts the directory itself
# on sys.path and the modules in it are top-level.
from test_pipeline_concurrency import (
    FakeCollector,
    FakeStorage,
    listing,
    make_settings,
    make_source,
    use_fake_collector,  # noqa: F401 - imported so the autouse fixture is in scope here
)

from intel.pipeline import crawl_depth_for, crawl_source

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_a_source_that_has_never_had_a_deep_walk_gets_one() -> None:
    """The first crawl must see the whole backlog.

    Otherwise `first_seen_at` — and with it every "new since yesterday" answer — is built
    from page 1 of a ten-page site, and nine pages of existing listings are recorded days
    later as though they had only just appeared.
    """
    assert crawl_depth_for(make_source(last_deep_crawl_at=None), now=NOW) == "deep"


def test_a_recent_deep_walk_means_the_next_crawl_only_probes() -> None:
    source = make_source(
        last_deep_crawl_at=NOW - timedelta(hours=1),
        deep_crawl_interval_seconds=6 * 3600,
    )
    assert crawl_depth_for(source, now=NOW) == "shallow"


def test_a_stale_deep_walk_comes_due_again() -> None:
    source = make_source(
        last_deep_crawl_at=NOW - timedelta(hours=7),
        deep_crawl_interval_seconds=6 * 3600,
    )
    assert crawl_depth_for(source, now=NOW) == "deep"


@pytest.mark.parametrize(("depth", "expected_fetches"), [("shallow", 1), ("deep", 5)])
async def test_a_shallow_crawl_fetches_only_page_one(
    use_fake_collector,  # noqa: ANN001, F811 - pytest fixture
    depth: str,
    expected_fetches: int,
) -> None:
    """The whole point of the split, measured in fetches.

    Four pages of listing plus the empty page that ends it costs five Tor round trips deep,
    and one shallow. That ratio is the latency win: the probe can run every few minutes
    precisely because it is this cheap.
    """
    collector = use_fake_collector(FakeCollector({no: listing(no) for no in range(1, 5)}))

    storage = FakeStorage()
    await crawl_source(
        make_source(max_pages=10),
        storage=storage,  # type: ignore[arg-type]
        settings=make_settings(),
        extractor_name="rules",
        depth=depth,
    )

    assert len(collector.requested) == expected_fetches
    assert storage.depths == [depth]


async def test_the_crawl_run_records_which_depth_it_was(
    use_fake_collector,  # noqa: ANN001, F811 - pytest fixture
) -> None:
    """Shallow and deep runs are not comparable, so they must be distinguishable.

    Averaging a one-page probe together with a full walk makes both numbers meaningless —
    and "is the probe actually finding anything?" is the question that decides whether this
    split earns its complexity.
    """
    use_fake_collector(FakeCollector({1: listing(1), 2: listing(2)}))
    storage = FakeStorage()

    await crawl_source(
        make_source(),
        storage=storage,  # type: ignore[arg-type]
        settings=make_settings(),
        extractor_name="rules",
        depth="shallow",
    )

    assert storage.depths == ["shallow"]
    assert storage.finished["depth"] == "shallow"
