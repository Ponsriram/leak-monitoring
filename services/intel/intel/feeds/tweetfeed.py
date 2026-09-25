"""TweetFeed — indicators the security community posts on X, collected by tweetfeed.live.

TweetFeed watches researchers' posts on X and publishes every URL, domain, address and hash
they share, with the hashtags they used and a link back to the post. It is the feed behind
the reference console's IOC section (`created_by: "tweetfeed"` on every row there).

Free and keyless: one GET of the last seven days, a few hundred KB. The week rather than
`today` because the fetch is hourly and `today` resets at midnight UTC — the last run of a
day would otherwise never see what was posted after it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from .base import FeedIoc, classify_value, host_of

log = structlog.get_logger(__name__)

__all__ = ["FEED_NAME", "fetch_tweetfeed"]

FEED_NAME = "tweetfeed"
_URL = "https://api.tweetfeed.live/v1/week"

_MAX_ENTRIES = 4000

# TweetFeed's own type names. Both hash algorithms are one `file_hash` type here, as they are
# for the other feeds; the algorithm is recoverable from the length.
_TYPES = {
    "url": "url",
    "domain": "domain",
    "ip": "ip",
    "md5": "file_hash",
    "sha256": "file_hash",
    "sha1": "file_hash",
}


async def fetch_tweetfeed(client: httpx.AsyncClient, *, limit: int = _MAX_ENTRIES) -> list[FeedIoc]:
    """Fetch and normalize the last week of posted indicators, newest first."""
    response = await client.get(_URL, headers={"Accept": "application/json"}, timeout=60.0)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        log.warning("tweetfeed: unexpected payload shape", got=type(payload).__name__)
        return []

    entries = [entry for entry in payload if isinstance(entry, dict)]
    entries.sort(key=lambda entry: str(entry.get("date") or ""), reverse=True)

    out: list[FeedIoc] = []
    for entry in entries:
        if len(out) >= limit:
            break

        value = _clean(entry.get("value"))
        if not value:
            continue

        declared = _TYPES.get((_clean(entry.get("type")) or "").lower())
        ioc_type = declared or classify_value(value)
        if ioc_type is None:
            continue

        # Hashtags arrive as "#phishing"; the other feeds' tags have no sigil, and a chip
        # filter that matched "#phishing" but not "phishing" would split one tag in two.
        tags = []
        for raw in entry.get("tags") or []:
            tag = str(raw).strip().lstrip("#")
            if tag and tag not in tags:
                tags.append(tag)

        user = _clean(entry.get("user"))
        tweet = _clean(entry.get("tweet"))

        out.append(
            FeedIoc(
                value=value,
                ioc_type=ioc_type,
                feed=FEED_NAME,
                host=host_of(value) if ioc_type in ("url", "domain") else None,
                tags=tags,
                # The first tag is the poster's headline for the indicator ("phishing",
                # "Kimsuky"), which is what the other feeds put in `threat`.
                threat=tags[0] if tags else None,
                note=f"Posted on X by @{user}." if user else None,
                feed_ref=tweet,
                reporter=user,
                reported_at=_parse_time(entry.get("date")),
            )
        )

    log.info("tweetfeed fetched", indicators=len(out))
    return out


def _parse_time(raw: object) -> datetime | None:
    """TweetFeed stamps are `YYYY-MM-DD HH:MM:SS` in UTC, with no offset."""
    text = _clean(raw)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
