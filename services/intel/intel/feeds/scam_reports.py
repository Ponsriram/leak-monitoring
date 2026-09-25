"""Scam phone-number reports, from public social posts.

The reference console's Mobile Number section is a list of numbers people reported after
being called, texted or messaged by a scammer, each with what the scam was, who it targeted
and the report itself. The same material is public on social platforms; this reads two
places that publish it without a login:

    mastodon    public hashtag timelines (#scam, #smishing, #scamcall, …) on one server,
                which carries posts federated from every other server
    reddit      a subreddit's RSS feed (r/Scams by default)

Each post is searched for phone numbers with the regex in `extract/phones.py`, and each
number that validates becomes one report: (number, post) is the identity. A post with no
valid number, or one that never mentions calling or messaging, produces nothing.

Only public posts are read, through each platform's own public endpoints, at a pace of one
request per tag or subreddit per run.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from ..extract.phones import (
    FoundNumber,
    classify_audience,
    classify_threats,
    find_numbers,
    has_phone_context,
)

log = structlog.get_logger(__name__)

__all__ = ["ScamReport", "fetch_mastodon_reports", "fetch_reddit_reports", "post_text"]

_ATOM = {"a": "http://www.w3.org/2005/Atom"}


@dataclass(slots=True)
class ScamReport:
    """One number in one post. Mirrors the `mobile_numbers` table."""

    number: FoundNumber
    threat_types: list[str]
    target_audience: list[str]
    details: str
    source: str
    source_url: str
    author: str | None
    language: str | None
    reported_at: datetime | None


async def fetch_mastodon_reports(
    client: httpx.AsyncClient, *, instance: str, tags: list[str]
) -> list[ScamReport]:
    """Read each tag's public timeline and return the reports found in it.

    One tag failing is logged and skipped; the others still run. A post carrying several of
    the tags arrives once per tag and is reduced to one set of reports here.
    """
    base = instance.rstrip("/")
    seen: set[str] = set()
    out: list[ScamReport] = []

    for tag in tags:
        url = f"{base}/api/v1/timelines/tag/{tag.strip().lstrip('#')}"
        try:
            response = await client.get(url, params={"limit": 40}, timeout=30.0)
            response.raise_for_status()
            statuses = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("mastodon tag fetch failed", tag=tag, error=str(exc))
            continue
        if not isinstance(statuses, list):
            continue

        for status in statuses:
            if not isinstance(status, dict):
                continue
            # A boost wraps the original post; the original is the report.
            status = status.get("reblog") or status
            link = _clean(status.get("url")) or _clean(status.get("uri"))
            if not link or link in seen:
                continue
            seen.add(link)

            body = post_text(status.get("content"))
            warning = _clean(status.get("spoiler_text"))
            text = f"{warning}\n\n{body}" if warning and body else (body or warning or "")
            account = status.get("account") if isinstance(status.get("account"), dict) else {}

            out.extend(
                _reports(
                    text,
                    source="mastodon",
                    source_url=link,
                    author=_clean(account.get("acct")),
                    language=_clean(status.get("language")),
                    reported_at=_parse_time(status.get("created_at")),
                )
            )

    log.info("mastodon reports fetched", reports=len(out), posts=len(seen))
    return out


async def fetch_reddit_reports(
    client: httpx.AsyncClient, *, subreddits: list[str]
) -> list[ScamReport]:
    """Read each subreddit's newest posts from its public RSS feed."""
    out: list[ScamReport] = []
    posts = 0

    for name in subreddits:
        url = f"https://www.reddit.com/r/{name.strip().removeprefix('r/')}/new/.rss"
        try:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("reddit feed fetch failed", subreddit=name, error=str(exc))
            continue

        for entry in root.findall("a:entry", _ATOM):
            link_el = entry.find("a:link", _ATOM)
            link = link_el.get("href") if link_el is not None else None
            if not link:
                continue
            posts += 1

            title = (entry.findtext("a:title", default="", namespaces=_ATOM) or "").strip()
            body = post_text(entry.findtext("a:content", default="", namespaces=_ATOM))
            # Reddit's RSS appends "submitted by /u/x [link] [comments]" to every body.
            body = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", body or "", flags=re.S).strip()
            text = f"{title}\n\n{body}" if body else title
            author = entry.findtext("a:author/a:name", default=None, namespaces=_ATOM)

            out.extend(
                _reports(
                    text,
                    source="reddit",
                    source_url=link,
                    author=(author or "").strip() or None,
                    language=None,
                    reported_at=_parse_time(
                        entry.findtext("a:published", default=None, namespaces=_ATOM)
                        or entry.findtext("a:updated", default=None, namespaces=_ATOM)
                    ),
                )
            )

    log.info("reddit reports fetched", reports=len(out), posts=posts)
    return out


def _reports(
    text: str,
    *,
    source: str,
    source_url: str,
    author: str | None,
    language: str | None,
    reported_at: datetime | None,
) -> list[ScamReport]:
    """Every validated number in one post, each as a report carrying the whole post."""
    text = text.strip()
    if not text or not has_phone_context(text):
        return []

    numbers = find_numbers(text, language=language)
    if not numbers:
        return []

    threats = classify_threats(text)
    audience = classify_audience(text)
    return [
        ScamReport(
            number=number,
            threat_types=threats,
            target_audience=audience,
            details=text,
            source=source,
            source_url=source_url,
            author=author,
            language=language,
            reported_at=reported_at,
        )
        for number in numbers
    ]


_BLOCK_BREAK = re.compile(r"</(?:p|div|li|blockquote|h[1-6])\s*>", re.I)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def post_text(markup: object) -> str | None:
    """A post's HTML as plain text, keeping its line and paragraph breaks.

    Inline tags are removed without leaving a space, because Mastodon wraps hashtags and
    links in spans — `#<span>scam</span>` must come out as `#scam`, not `# scam`.
    """
    if not isinstance(markup, str) or not markup.strip():
        return None
    text = _BLOCK_BREAK.sub("\n\n", markup)
    text = _LINE_BREAK.sub("\n", text)
    text = html.unescape(_TAG.sub("", text))
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def _parse_time(raw: object) -> datetime | None:
    text = _clean(raw)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
