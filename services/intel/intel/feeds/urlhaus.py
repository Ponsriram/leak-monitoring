"""URLhaus — malicious URLs, from abuse.ch.

The recent dump is the last few weeks of reports, currently around 15,000 URLs and 7 MB. It
is published as a free download with no key, which is why it is here and not behind a
credential.

Each URL yields two indicators, not one. "This exact URL served malware" and "this host is
involved in malware delivery" are different assertions with different lifetimes and different
responses — the first is a proxy block, the second is a DNS block — and a console that only
stored the URL would make the second unanswerable.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from .base import FeedIoc, classify_value, host_of, parse_feed_time, split_tags

log = structlog.get_logger(__name__)

__all__ = ["FEED_NAME", "fetch_urlhaus"]

FEED_NAME = "urlhaus"
_URL = "https://urlhaus.abuse.ch/downloads/json_recent/"

# The dump is several megabytes and grows. Reading it all is fine; storing every historical
# entry on the first run is not, because it would swamp the table with weeks of indicators
# all stamped as arriving at once.
_MAX_ENTRIES = 4000


async def fetch_urlhaus(client: httpx.AsyncClient, *, limit: int = _MAX_ENTRIES) -> list[FeedIoc]:
    """Fetch and normalize the recent-URL dump."""
    response = await client.get(_URL, headers={"Accept": "application/json"}, timeout=60.0)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        log.warning("urlhaus: unexpected payload shape", got=type(payload).__name__)
        return []

    out: list[FeedIoc] = []
    hosts_seen: set[str] = set()

    # The dump is keyed by upstream id, each holding a single-element list. Sorted descending
    # so a `limit` keeps the newest rather than an arbitrary slice of the dict's ordering.
    for key in sorted(payload.keys(), key=_as_int, reverse=True):
        if len(out) >= limit:
            break
        entries = payload[key]
        if not isinstance(entries, list) or not entries:
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue

        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue

        reported_at = parse_feed_time(entry.get("dateadded"))
        tags = split_tags(entry.get("tags"))
        threat = _clean(entry.get("threat"))
        status = _clean(entry.get("url_status"))
        reporter = _clean(entry.get("reporter"))
        ref = _clean(entry.get("urlhaus_link"))

        # `url_status` is the feed's own liveness check, and it is genuinely useful context —
        # an offline URL is history, an online one is an active threat.
        note = f"URL is {status}." if status else None

        out.append(
            FeedIoc(
                value=url,
                ioc_type="url",
                feed=FEED_NAME,
                host=host_of(url),
                tags=tags,
                threat=threat,
                note=note,
                feed_ref=ref,
                reporter=reporter,
                reported_at=reported_at,
            )
        )

        # The host, once. A campaign posts hundreds of URLs on one domain, and emitting a
        # domain indicator per URL would upsert the same row hundreds of times per run.
        host = host_of(url)
        if host and host not in hosts_seen:
            hosts_seen.add(host)
            out.append(
                FeedIoc(
                    value=host,
                    ioc_type=classify_value(host) or "domain",
                    feed=FEED_NAME,
                    host=host,
                    tags=tags,
                    threat=threat,
                    note="Host of a URL reported to URLhaus.",
                    feed_ref=ref,
                    reporter=reporter,
                    reported_at=reported_at,
                )
            )

    log.info("urlhaus fetched", indicators=len(out))
    return out


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
