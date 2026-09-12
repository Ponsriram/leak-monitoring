"""Feed parsing tests.

Offline, against captured record shapes. The feeds are public and free, but a test suite that
fetches seven megabytes from abuse.ch on every run is both slow and rude — and it would fail
for reasons that have nothing to do with our code.
"""

from __future__ import annotations

import httpx
import pytest

from intel.feeds.base import classify_value, host_of, parse_feed_time, split_tags
from intel.feeds.threatfox import fetch_threatfox
from intel.feeds.urlhaus import fetch_urlhaus


@pytest.mark.parametrize(
    ("value", "declared", "expected"),
    [
        ("http://evil.example/a", None, "url"),
        ("https://evil.example", "url", "url"),
        ("evil.example", None, "domain"),
        ("1.2.3.4", None, "ip"),
        # A port is part of the observation and stays in the value, but the thing is still
        # an address — this is the shape ThreatFox uses for botnet C2.
        ("1.2.3.4:8080", "ip:port", "ip"),
        ("2001:db8::1", None, "ip"),
        ("d41d8cd98f00b204e9800998ecf8427e", None, "file_hash"),
        ("a" * 64, None, "file_hash"),
        ("someone@evil.example", None, "email"),
        # A feed inventing a type name must not drop the indicator — shape decides.
        ("http://evil.example/a", "brand_new_type", "url"),
        ("not a thing", None, None),
    ],
)
def test_classify_value(value: str, declared: str | None, expected: str | None) -> None:
    assert classify_value(value, declared) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://x.evil.example/a?b=1", "x.evil.example"),
        ("https://user@evil.example:8443/p", "evil.example"),
        ("evil.example", "evil.example"),
        # An address is not a host: domain_enrichment is keyed by domain, and joining an IP
        # to it would either miss or collide.
        ("1.2.3.4", None),
        ("1.2.3.4:8080", None),
        ("http://1.2.3.4:8080/x", None),
    ],
)
def test_host_of(value: str, expected: str | None) -> None:
    assert host_of(value) == expected


def test_parse_feed_time_applies_utc() -> None:
    """Neither feed sends an offset, and both mean UTC.

    A naive datetime here would be reinterpreted as local time on write, shifting every
    indicator by the host's offset — a silent, entirely invisible corruption.
    """
    urlhaus = parse_feed_time("2026-08-29 20:11:20 UTC")
    threatfox = parse_feed_time("2026-08-29 20:11:20")
    assert urlhaus is not None and threatfox is not None
    assert urlhaus == threatfox
    assert urlhaus.tzinfo is not None
    assert urlhaus.utcoffset().total_seconds() == 0


def test_parse_feed_time_returns_none_for_junk() -> None:
    assert parse_feed_time(None) is None
    assert parse_feed_time("not a date") is None


def test_split_tags_handles_every_shape_the_feeds_send() -> None:
    assert split_tags(["a", "b", "a"]) == ["a", "b"]
    assert split_tags("a,b, a ") == ["a", "b"]
    assert split_tags(None) == []


def _client(payload: object) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# One entry, trimmed to the fields the parser reads, in the exact shape the live dump uses:
# a dict keyed by upstream id, each value a single-element list.
_URLHAUS_DUMP = {
    "3909806": [
        {
            "dateadded": "2026-08-29 20:11:20 UTC",
            "url": "http://malicious.example/payload.bin",
            "url_status": "online",
            "threat": "malware_download",
            "tags": "elf,mips",
            "urlhaus_link": "https://urlhaus.abuse.ch/url/3909806/",
            "reporter": "someone",
        }
    ],
    "3909805": [
        {
            "dateadded": "2026-08-29 20:08:13 UTC",
            "url": "http://malicious.example/second.bin",
            "url_status": "offline",
            "threat": "malware_download",
            "tags": None,
            "urlhaus_link": "https://urlhaus.abuse.ch/url/3909805/",
            "reporter": "someone",
        }
    ],
}


async def test_urlhaus_emits_a_url_and_one_host_indicator() -> None:
    """Two URLs on one host must not produce two identical domain indicators.

    A campaign posts hundreds of URLs on a single domain. Emitting a domain indicator per URL
    would upsert the same row hundreds of times per run for no added information.
    """
    async with _client(_URLHAUS_DUMP) as client:
        items = await fetch_urlhaus(client)

    urls = [i for i in items if i.ioc_type == "url"]
    domains = [i for i in items if i.ioc_type == "domain"]

    assert len(urls) == 2
    assert len(domains) == 1
    assert domains[0].value == "malicious.example"

    newest = urls[0]
    assert newest.value == "http://malicious.example/payload.bin"
    assert newest.host == "malicious.example"
    assert newest.threat == "malware_download"
    assert newest.tags == ["elf", "mips"]
    assert newest.feed == "urlhaus"
    assert newest.feed_ref == "https://urlhaus.abuse.ch/url/3909806/"
    assert newest.reported_at is not None
    # The feed's own liveness check is real context and belongs in the note.
    assert newest.note is not None and "online" in newest.note


async def test_urlhaus_returns_newest_first_when_limited() -> None:
    """A limit must keep the newest entries, not an arbitrary slice of dict ordering."""
    async with _client(_URLHAUS_DUMP) as client:
        items = await fetch_urlhaus(client, limit=1)

    assert items[0].value == "http://malicious.example/payload.bin"


_THREATFOX_DUMP = {
    "1891178": [
        {
            "ioc_value": "1.2.3.4:8811",
            "ioc_type": "ip:port",
            "threat_type": "botnet_cc",
            "malware": "win.remus",
            "malware_printable": "Remus",
            "first_seen_utc": "2026-08-29 20:05:27",
            "confidence_level": 75,
            "is_compromised": False,
            "reference": "https://bazaar.abuse.ch/sample/abc/",
            "tags": "remus",
            "reporter": "abuse_ch",
        }
    ]
}


async def test_threatfox_keeps_the_family_and_the_raw_slug() -> None:
    """The printable name is for people; the slug is what someone pastes into a search."""
    async with _client(_THREATFOX_DUMP) as client:
        items = await fetch_threatfox(client)

    assert len(items) == 1
    ioc = items[0]
    assert ioc.ioc_type == "ip"
    # The port stays in the value — it is part of the observation.
    assert ioc.value == "1.2.3.4:8811"
    assert ioc.host is None
    assert ioc.threat == "Remus"
    assert "win.remus" in ioc.tags
    assert "botnet_cc" in ioc.tags
    assert ioc.confidence == 75
    assert ioc.feed == "threatfox"


async def test_a_feed_serving_junk_yields_nothing_rather_than_raising() -> None:
    """A shape change upstream must degrade to an empty result, not take the worker down."""
    async with _client({"1": [{"no_useful_fields": True}]}) as client:
        assert await fetch_threatfox(client) == []
        assert await fetch_urlhaus(client) == []

    async with _client(["not", "a", "dict"]) as client:
        assert await fetch_threatfox(client) == []
        assert await fetch_urlhaus(client) == []
