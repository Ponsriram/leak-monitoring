"""ransomware.live, TweetFeed and the scam-report collectors.

Offline, against record shapes captured from the live endpoints on 2026-09-19.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from intel.feeds.ransomware_live import fetch_ransomware_live, unwrap_description
from intel.feeds.scam_reports import fetch_mastodon_reports, fetch_reddit_reports, post_text
from intel.feeds.tweetfeed import fetch_tweetfeed
from intel.models import ExtractionMethod


def _client(payload: Any = None, *, text: str | None = None) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(200, text=text)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- ransomware.live -------------------------------------------------------------------------

_WRAPPED = (
    "Paylogix is an insuretech pioneer offering premium technology sol\n"
    "utions that streamline the administration of voluntary benefits. \n"
    "Their robust suite of services includes enrollment, premium billi\n"
    "ng, alternative funding.\n"
    "\n"
    "We will upload 185gb of corporate data soon.\n"
)


def test_the_feeds_hard_wrap_is_undone_without_splitting_words() -> None:
    assert unwrap_description(_WRAPPED) == (
        "Paylogix is an insuretech pioneer offering premium technology solutions that "
        "streamline the administration of voluntary benefits. Their robust suite of services "
        "includes enrollment, premium billing, alternative funding.\n\n"
        "We will upload 185gb of corporate data soon."
    )


def test_an_ai_profile_that_knows_nothing_is_dropped() -> None:
    assert unwrap_description("[AI generated] N/A\n\nI don't have any reliable information") is None


_VICTIM = {
    "activity": "Financial Services",
    "attackdate": "2026-11-18 00:00:00.000000",
    "claim_url": "http://example.onion/entity/1",
    "country": "US",
    "data_size": None,
    "description": _WRAPPED,
    "discovered": "2026-01-15T13:48:29.435004+00:00",
    "domain": "paylogix.com",
    "group": "incransom",
    "infostealer": {
        "employees": 0,
        "infostealer_stats": {"RedLine": 1, "Generic Stealer": 1},
        "thirdparties": 0,
        "users": 2,
    },
    "press": {
        "source": "https://therecord.media/paylogix-cyberattack",
        "summary": "Paylogix disclosed a breach.",
    },
    "ransom": None,
    "screenshot": "",
    "url": "https://www.ransomware.live/id/abc",
    "victim": "Paylogix",
}


async def test_a_victim_becomes_a_leak_with_every_field_the_feed_holds() -> None:
    async with _client([_VICTIM, {"group": "", "victim": "x"}]) as client:
        leaks = await fetch_ransomware_live(client)

    assert len(leaks) == 1
    leak = leaks[0]
    # Mapped onto the slug our own crawler uses, so the two routes share a dedupe key.
    assert leak.actor_group == "inc-ransom"
    assert leak.victim_name == "Paylogix"
    assert leak.victim_domain == "paylogix.com"
    assert leak.victim_country == "United States"
    assert leak.victim_sector == "Financial Services"
    assert leak.source_url == "https://www.ransomware.live/id/abc"
    assert leak.published_at == datetime(2026, 1, 15, 13, 48, 29, 435004, tzinfo=UTC)
    assert leak.published_at_raw == "2026-11-18 00:00:00.000000"
    assert leak.extraction.method is ExtractionMethod.FEED
    assert leak.extraction.model_dump()["feed"] == "ransomware.live"

    # Description, press coverage and infostealer figures, all of it, none shortened.
    assert leak.summary is not None
    assert leak.summary.startswith("Paylogix is an insuretech pioneer")
    assert "We will upload 185gb of corporate data soon." in leak.summary
    assert "Press coverage: Paylogix disclosed a breach." in leak.summary
    assert "2 users compromised" in leak.summary
    assert "RedLine (1)" in leak.summary
    assert "ransomware" in leak.incident_types


async def test_unknown_activity_is_null_not_a_sector() -> None:
    async with _client([{**_VICTIM, "activity": "Not Found", "country": ""}]) as client:
        (leak,) = await fetch_ransomware_live(client)
    assert leak.victim_sector is None
    assert leak.victim_country is None


# --- TweetFeed -------------------------------------------------------------------------------

_TWEETS = [
    {
        "date": "2026-09-18 00:35:07",
        "user": "G60930953",
        "type": "sha256",
        "value": "a6ceacda670b88e8a8ec9ff5da6a77d9f1c896d6479b2dadb700474a8c408f80",
        "tags": ["#APT"],
        "tweet": "https://x.com/G60930953/status/2100745399275270165",
    },
    {
        "date": "2026-09-18 01:40:04",
        "user": "scanmalware",
        "type": "url",
        "value": "http://berquni-mpt-zavfelo-r4x8ka65.pages.dev",
        "tags": ["#malware", "#phishing"],
        "tweet": "https://x.com/scanmalware/status/2100761742397604156",
    },
]


async def test_tweetfeed_indicators_are_normalized_newest_first() -> None:
    async with _client(_TWEETS) as client:
        iocs = await fetch_tweetfeed(client)

    assert [ioc.ioc_type for ioc in iocs] == ["url", "file_hash"]
    url = iocs[0]
    assert url.feed == "tweetfeed"
    assert url.host == "berquni-mpt-zavfelo-r4x8ka65.pages.dev"
    # The hashtag sigil is dropped so tags filter the same way as the other feeds'.
    assert url.tags == ["malware", "phishing"]
    assert url.threat == "malware"
    assert url.reporter == "scanmalware"
    assert url.feed_ref == "https://x.com/scanmalware/status/2100761742397604156"
    assert url.reported_at == datetime(2026, 9, 18, 1, 40, 4, tzinfo=UTC)


# --- scam reports ----------------------------------------------------------------------------


def test_post_html_keeps_breaks_and_joins_hashtags() -> None:
    markup = (
        "<p>Scam call from 0208 540 2764</p>"
        '<p>Said he was <a href="#">#<span>Microsoft</span></a><br>support</p>'
    )
    assert post_text(markup) == "Scam call from 0208 540 2764\n\nSaid he was #Microsoft\nsupport"


_STATUSES = [
    {
        "url": "https://mastodon.social/@someone/1",
        "created_at": "2026-09-18T11:11:10.232Z",
        "language": "en",
        "spoiler_text": "",
        "content": (
            "<p>JUST had a call from a phone scammer on 0208 540 2764 who said she was from "
            "Microsoft tech support and wanted remote access to my computer.</p>"
        ),
        "account": {"acct": "someone"},
    },
    # A boost of the same post must not produce a second report.
    {"reblog": {"url": "https://mastodon.social/@someone/1", "content": "dup"}},
    # A post with an address and a date but no phone number produces nothing.
    {
        "url": "https://mastodon.social/@feed/2",
        "created_at": "2026-09-18T11:11:10.232Z",
        "language": "en",
        "content": "<p>Top malicious IPs on 2026-09-18: 158.94.208.177, call it a day</p>",
        "account": {"acct": "feed"},
    },
]


async def test_mastodon_posts_become_reports_with_every_mandatory_field() -> None:
    async with _client(_STATUSES) as client:
        reports = await fetch_mastodon_reports(
            client, instance="https://mastodon.social", tags=["scam", "scammer"]
        )

    assert len(reports) == 1
    report = reports[0]
    assert report.number.e164 == "+442085402764"
    assert report.number.raw == "0208 540 2764"
    assert report.number.country == "United Kingdom"
    assert "Vishing" in report.threat_types
    assert "Tech Support Scam" in report.threat_types
    assert "Windows / Computer Users" in report.target_audience
    assert report.details.startswith("JUST had a call")
    assert report.details.endswith("my computer.")
    assert report.source == "mastodon"
    assert report.author == "someone"


_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/victim</name></author>
    <content type="html">&lt;p&gt;They texted me from +1 (775) 208-9213 saying my USPS parcel
    was held and I had to pay a customs fee.&lt;/p&gt;
    submitted by /u/victim [link] [comments]</content>
    <link href="https://www.reddit.com/r/Scams/comments/abc/usps/"/>
    <published>2026-09-18T20:38:14+00:00</published>
    <title>USPS text scam number</title>
  </entry>
</feed>"""


async def test_reddit_rss_posts_become_reports() -> None:
    async with _client(text=_RSS) as client:
        reports = await fetch_reddit_reports(client, subreddits=["Scams"])

    assert len(reports) == 1
    report = reports[0]
    assert report.number.e164 == "+17752089213"
    assert report.details.startswith("USPS text scam number\n\nThey texted me")
    assert "submitted by" not in report.details
    assert "Delivery Scam" in report.threat_types
    assert "Parcel Recipients" in report.target_audience
    assert report.source == "reddit"
    assert report.author == "/u/victim"
