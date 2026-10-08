"""JSON a JavaScript leak site loads: captured by the browser, read through a per-source mapping.

Playwright is never started: the collector is handed a fake context whose page "loads" a
scripted set of responses, which is all the capture code ever sees of a real one.
"""

# ruff: noqa: F811 - pytest fixtures are imported, then named as parameters
from __future__ import annotations

import json

import pytest
from test_detail_pages import DetailStorage
from test_pipeline_concurrency import (  # noqa: F401 - the autouse fixture must be in scope
    make_settings,
    make_source,
    use_fake_collector,
)

from intel.collectors import CapturedJson, FetchResult, to_text
from intel.collectors.tor_browser import TorBrowserCollector
from intel.crawl.fetch import CrawlFetcher
from intel.crawl.queue import ClaimedUrl
from intel.extract.json_items import (
    items_text,
    leaks_from_items,
    match_items,
    parse_mapping,
)
from intel.pipeline import crawl_source

PAGE = "http://jsleak.onion/disclosures"
API = "http://jsleak.onion/api/v1/posts?page=1"
HTML = (
    "<html><body><h1>Disclosures</h1><div id='app'>Loading the latest companies, please "
    "wait while the list is rendered.</div></body></html>"
)
RECORDS = {
    "data": {
        "posts": [
            {
                "company": {"title": "Tec Imports", "site": "tecimports.com.br"},
                "geo": "BR",
                "revenue": "$12M",
                "about": "Importer of industrial machinery parts.",
                "created": 1758326400000,
            },
            {
                "company": {"title": "Wavecrest HFA", "site": None},
                "geo": "United Kingdom",
                "about": "Housing finance agency.",
                "created": "2026-09-01",
            },
            {"company": {"title": None, "site": None}},
        ]
    }
}
MAPPING = {
    "match": "/api/v1/posts",
    "path": "data.posts",
    "name": "company.title",
    "domain": "company.site",
    "country": "geo",
    "revenue": "revenue",
    "description": "about",
    "date": "created",
}


# ---------------------------------------------------------------- fake Playwright


class FakeResponse:
    def __init__(
        self, url: str, content_type: str, body: str | Exception = "", status: int = 200
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body

    async def text(self) -> str:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakePage:
    def __init__(self, loads: list[FakeResponse], html: str = HTML) -> None:
        self._loads = loads
        self._html = html
        self._handlers: list = []

    def on(self, event: str, handler) -> None:  # type: ignore[no-untyped-def]
        assert event == "response"
        self._handlers.append(handler)

    async def goto(self, url: str, **_: object) -> FakeResponse:
        main = FakeResponse(url, "text/html; charset=utf-8", self._html)
        for response in [main, *self._loads]:
            for handler in self._handlers:
                handler(response)
        return main

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        return None

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        return None


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def new_page(self) -> FakePage:
        return self._page


def browser(loads: list[FakeResponse], *, max_bytes: int | None = 5_000_000) -> TorBrowserCollector:
    collector = TorBrowserCollector(max_bytes=max_bytes)
    collector._context = FakeContext(FakePage(loads))  # noqa: SLF001 - skips launching Firefox
    return collector


# ---------------------------------------------------------------- capture


async def test_same_host_json_is_captured() -> None:
    result = await browser(
        [
            FakeResponse(API, "application/json; charset=utf-8", json.dumps(RECORDS)),
            FakeResponse("http://jsleak.onion/api/v1/stats", "application/vnd.api+json", "{}"),
        ]
    ).fetch_detailed(PAGE)
    assert result.kind == "ok"
    assert result.json_responses is not None
    assert [r.url for r in result.json_responses] == [API, "http://jsleak.onion/api/v1/stats"]
    assert json.loads(result.json_responses[0].body) == RECORDS


async def test_other_hosts_and_other_types_are_not_captured() -> None:
    result = await browser(
        [
            FakeResponse("http://tracker.onion/api/posts", "application/json", "{}"),
            FakeResponse("http://jsleak.onion/app.js", "application/javascript", "var a;"),
            FakeResponse("http://jsleak.onion/logo.png", "image/png", "png"),
        ]
    ).fetch_detailed(PAGE)
    assert result.kind == "ok"
    # Nothing captured: the result is what it always was.
    assert result.json_responses is None
    assert result.text == HTML


async def test_captured_json_is_capped_at_the_byte_limit() -> None:
    big = json.dumps({"x": "a" * 400})
    result = await browser(
        [
            FakeResponse("http://jsleak.onion/api/1", "application/json", big),
            FakeResponse("http://jsleak.onion/api/2", "application/json", big),
        ],
        max_bytes=len(HTML) + 600,
    ).fetch_detailed(PAGE)
    assert [r.url for r in result.json_responses or []] == ["http://jsleak.onion/api/1"]


async def test_an_unreadable_body_is_skipped_not_fatal() -> None:
    result = await browser(
        [FakeResponse(API, "application/json", RuntimeError("body discarded"))]
    ).fetch_detailed(PAGE)
    assert result.kind == "ok" and result.json_responses is None


# ---------------------------------------------------------------- mapping


def test_a_mapping_needs_a_name_or_a_domain() -> None:
    assert parse_mapping(None) is None
    assert parse_mapping({}) is None
    assert parse_mapping({"path": "data", "country": "geo"}) is None
    assert parse_mapping("not json") is None
    assert parse_mapping(json.dumps(MAPPING)) == parse_mapping(MAPPING)
    assert parse_mapping(MAPPING) is not None


def test_records_come_from_the_first_matching_response() -> None:
    mapping = parse_mapping(MAPPING)
    responses = [
        CapturedJson("http://jsleak.onion/api/v1/stats", json.dumps({"data": {"posts": [1]}})),
        CapturedJson(API, "not json at all"),
        CapturedJson(API, json.dumps(RECORDS)),
    ]
    items = match_items(responses, mapping)
    assert items is not None and len(items) == 3
    assert match_items([CapturedJson(API, json.dumps({"data": {}}))], mapping) is None
    assert match_items(None, mapping) is None


def test_records_become_normalized_leaks() -> None:
    mapping = parse_mapping(MAPPING)
    assert mapping is not None
    leaks = leaks_from_items(
        RECORDS["data"]["posts"], mapping, source_group="jsleak", source_url=PAGE, page_no=1
    )
    # The record that names no one is dropped.
    assert [(leak.victim_name, leak.victim_domain) for leak in leaks] == [
        ("Tec Imports", "tecimports.com.br"),
        ("Wavecrest HFA", None),
    ]
    tec, wave = leaks
    assert tec.victim_country == "Brazil"
    assert tec.summary == "Revenue: $12M Importer of industrial machinery parts."
    assert tec.published_at is not None and tec.published_at.date().isoformat() == "2025-09-20"
    assert wave.victim_country == "United Kingdom"
    assert wave.published_at is not None and wave.published_at.date().isoformat() == "2026-09-01"
    assert {leak.extraction.model_dump()["mode"] for leak in leaks} == {"json"}


def test_the_hashed_text_carries_only_mapped_fields() -> None:
    mapping = parse_mapping(MAPPING)
    assert mapping is not None
    rendered = items_text(RECORDS["data"]["posts"], mapping)
    assert rendered.splitlines()[0] == (
        "Tec Imports | tecimports.com.br | BR | $12M | Importer of industrial machinery parts. "
        "| 1758326400000"
    )


# ---------------------------------------------------------------- both engines


class JsonCollector:
    """A browser-kind collector whose page loads `RECORDS` from the API."""

    name = "browser"

    def __init__(self, responses: list[CapturedJson] | None) -> None:
        self._responses = responses
        self.last_error: str | None = None

    async def fetch(self, url: str) -> str | None:
        return HTML

    async def fetch_detailed(self, url: str) -> FetchResult:
        return FetchResult(
            url,
            "ok",
            text=HTML,
            http_status=200,
            size_bytes=len(HTML),
            json_responses=self._responses,
        )

    async def aclose(self) -> None:
        return None


def claimed(json_items: str | None) -> ClaimedUrl:
    return ClaimedUrl(
        id=1,
        source_id=1,
        source_slug="jsleak",
        collector="browser",
        cycle_id=None,
        url=PAGE,
        kind="listing",
        page_no=1,
        depth=0,
        attempt=1,
        parent_id=None,
        content_sha256=None,
        failure_count=0,
        interval_seconds=900,
        deep_interval_seconds=21600,
        base_url="http://jsleak.onion",
        json_items=json_items,
    )


@pytest.mark.parametrize("mapped", [True, False])
async def test_both_engines_build_the_same_leaks_from_json(use_fake_collector, mapped) -> None:  # type: ignore[no-untyped-def]
    responses = [CapturedJson(API, json.dumps(RECORDS))]
    settings = make_settings(EXPOSURE_DETECTION=False, CRAWL_FOLLOW_LINKS=False)
    mapping = MAPPING if mapped else None

    use_fake_collector(JsonCollector(responses))
    legacy = DetailStorage()
    source = make_source(
        slug="jsleak",
        base_url=PAGE,
        collector="browser",
        pagination_style="none",
        max_pages=1,
        json_items=json.dumps(mapping) if mapping else None,
    )
    await crawl_source(source, storage=legacy, settings=settings, extractor_name="rules")

    queue = DetailStorage()
    fetcher = CrawlFetcher(queue, settings, collectors={"browser": JsonCollector(responses)})  # type: ignore[arg-type]
    await fetcher(claimed(json.dumps(mapping) if mapping else None))

    assert [leak.model_dump() for leak in queue.upserted] == [
        leak.model_dump() for leak in legacy.upserted
    ]
    assert queue.saved[0][1] == legacy.saved[0][1]
    if mapped:
        assert [leak.victim_name for leak in queue.upserted] == ["Tec Imports", "Wavecrest HFA"]
        assert "Tec Imports | tecimports.com.br" in queue.saved[0][1]
    else:
        # No mapping: captured JSON changes nothing, text and leaks are the page's own.
        assert queue.saved[0][1] == to_text(HTML)
        assert queue.upserted == []
