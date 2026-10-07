"""Following links down a leak site's tree, and giving every source a fair share of time.

Both are bounded loops, so the tests are about the bounds: depth, fan-out, total pages, and
what is never followed at all (other hosts, files).
"""

# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from test_pipeline_concurrency import (  # noqa: F401 - the autouse fixture must be in scope
    FakeStorage,
    make_settings,
    make_source,
    use_fake_collector,
)

from intel.collectors.links import extract_links, normalize_url
from intel.pipeline import crawl_source, stalest_first
from intel.scheduling import source_time_budget

BASE = "http://example.onion"


def page(
    *links: str, body: str = "Victim details and a long enough description of the leak."
) -> str:
    anchors = "".join(f'<a href="{link}">link</a>' for link in links)
    return f"<html><body><h1>Leak</h1><p>{body}</p>{anchors}</body></html>"


class TreeCollector:
    """Serves a dict of url -> html, counting what was asked for."""

    name = "http"

    def __init__(self, site: dict[str, str]) -> None:
        self.site = site
        self.requested: list[str] = []
        self.last_error: str | None = None

    async def fetch(self, url: str) -> str | None:
        self.requested.append(url)
        await asyncio.sleep(0)
        return self.site.get(url)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------- link selection


def test_only_same_host_page_links_are_followed() -> None:
    html = page(
        "/victim/1",
        "http://other.onion/victim/2",
        "https://example.onion/victim/3",
        "mailto:a@b.c",
        "javascript:void(0)",
        "#top",
    )
    assert extract_links(html, BASE + "/", limit=10) == [
        BASE + "/victim/1",
        "https://example.onion/victim/3",
    ]


def test_files_and_account_pages_are_never_followed() -> None:
    html = page(
        "/dump.zip", "/leak.sql", "/login", "/proof.png", "/files/data.torrent", "/victim/9"
    )
    assert extract_links(html, BASE, limit=10) == [BASE + "/victim/9"]


def test_the_fan_out_is_capped_and_duplicates_collapse() -> None:
    html = page(*[f"/v/{n}" for n in range(20)], "/v/0", "/v/0/", "/v/0#x")
    assert extract_links(html, BASE, limit=5) == [BASE + f"/v/{n}" for n in range(5)]


def test_visited_pages_are_skipped() -> None:
    html = page("/a", "/b")
    assert extract_links(html, BASE, visited={normalize_url(BASE + "/a/")}, limit=5) == [
        BASE + "/b"
    ]


# ---------------------------------------------------------------- the crawl


def settings(**overrides: object):  # type: ignore[no-untyped-def]
    return make_settings(EXPOSURE_DETECTION=False, **overrides)


async def crawl(site: dict[str, str], use_fake_collector, **overrides):  # type: ignore[no-untyped-def]
    collector = use_fake_collector(TreeCollector(site))
    storage = FakeStorage()
    result = await crawl_source(
        make_source(pagination_style="none"),
        storage=storage,  # type: ignore[arg-type]
        settings=settings(**overrides),
        extractor_name="rules",
    )
    return result, collector, storage


async def test_links_are_followed_to_the_configured_depth(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    site = {
        BASE: page("/l1"),
        BASE + "/l1": page("/l2", body="Level one page with plenty of readable text in it."),
        BASE + "/l2": page("/l3", body="Level two page with plenty of readable text in it."),
        BASE + "/l3": page("/l4", body="Level three page with plenty of readable text in it."),
        BASE + "/l4": page(body="Level four page with plenty of readable text in it."),
    }
    result, collector, _ = await crawl(site, use_fake_collector, CRAWL_LINK_DEPTH=3)

    assert collector.requested == [BASE, BASE + "/l1", BASE + "/l2", BASE + "/l3"]
    assert result.link_pages == 3


async def test_each_page_contributes_at_most_links_per_page(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    site = {BASE: page(*[f"/v/{n}" for n in range(12)])}
    site.update(
        {
            BASE + f"/v/{n}": page(body=f"Victim number {n} with plenty of text to read.")
            for n in range(12)
        }
    )
    result, _, _ = await crawl(site, use_fake_collector, CRAWL_LINKS_PER_PAGE=3)

    assert result.link_pages == 3


async def test_total_followed_pages_are_capped(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    """5 per page over 3 levels could be 155 fetches; the cap is what stops that."""
    site: dict[str, str] = {BASE: page(*[f"/a{n}" for n in range(5)])}
    for n in range(5):
        site[BASE + f"/a{n}"] = page(
            *[f"/b{n}{m}" for m in range(5)], body=f"Branch a{n} text text text text."
        )
        for m in range(5):
            site[BASE + f"/b{n}{m}"] = page(
                *[f"/c{n}{m}{k}" for k in range(5)], body=f"Branch b{n}{m} text text text."
            )
    result, _, _ = await crawl(site, use_fake_collector, CRAWL_LINK_MAX_PAGES=8)

    assert result.link_pages <= 8


async def test_a_cycle_is_fetched_once(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    site = {
        BASE: page("/a"),
        BASE + "/a": page("/b", "/", body="Page A with plenty of readable text in it here."),
        BASE + "/b": page("/a", body="Page B with plenty of readable text in it here."),
    }
    _, collector, _ = await crawl(site, use_fake_collector)

    assert sorted(collector.requested) == sorted([BASE, BASE + "/a", BASE + "/b"])


async def test_a_shallow_probe_follows_nothing(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    collector = use_fake_collector(TreeCollector({BASE: page("/a"), BASE + "/a": page()}))
    result = await crawl_source(
        make_source(pagination_style="none"),
        storage=FakeStorage(),  # type: ignore[arg-type]
        settings=settings(),
        extractor_name="rules",
        depth="shallow",
    )
    assert collector.requested == [BASE]
    assert result.link_pages == 0


async def test_following_can_be_switched_off(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    _, collector, _ = await crawl(
        {BASE: page("/a"), BASE + "/a": page()}, use_fake_collector, CRAWL_FOLLOW_LINKS=False
    )
    assert collector.requested == [BASE]


async def test_a_dead_link_does_not_fail_the_crawl(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    result, _, _ = await crawl({BASE: page("/gone")}, use_fake_collector)
    assert result.status == "succeeded"
    assert result.link_pages == 0


async def test_the_time_budget_stops_the_walk_without_failing_it(use_fake_collector) -> None:  # type: ignore[no-untyped-def]
    site = {BASE: page("/a"), BASE + "/a": page(body="Level one with plenty of readable text.")}
    collector = use_fake_collector(TreeCollector(site))
    storage = FakeStorage()
    result = await crawl_source(
        make_source(pagination_style="none"),
        storage=storage,  # type: ignore[arg-type]
        settings=settings(),
        extractor_name="rules",
        time_budget=0.0,
    )

    assert collector.requested == [BASE]
    assert result.status == "succeeded"
    assert result.truncated
    # Cut short, so it must not count as a completed deep walk.
    assert storage.finished["depth"] == "shallow"


# ---------------------------------------------------------------- fairness


def test_the_budget_is_an_even_share_of_the_window() -> None:
    args = {"concurrency": 4, "window_seconds": 1800, "floor_seconds": 90, "ceiling_seconds": 900}
    assert source_time_budget(20, **args) == 360
    assert source_time_budget(40, **args) == 180
    # More sources, smaller share — the window holds.
    assert source_time_budget(40, **args) * 40 / 4 == 1800


def test_the_budget_is_clamped() -> None:
    args = {"concurrency": 4, "window_seconds": 1800, "floor_seconds": 90, "ceiling_seconds": 900}
    assert source_time_budget(2, **args) == 900
    assert source_time_budget(500, **args) == 90


def test_the_longest_waiting_source_goes_first() -> None:
    now = datetime.now(UTC)
    never = make_source(slug="zzz-never", last_crawl_at=None)
    old = make_source(slug="mmm-old", last_crawl_at=now - timedelta(hours=5))
    recent = make_source(slug="aaa-recent", last_crawl_at=now - timedelta(minutes=5))

    assert [s.slug for s in stalest_first([recent, old, never])] == [
        "zzz-never",
        "mmm-old",
        "aaa-recent",
    ]
