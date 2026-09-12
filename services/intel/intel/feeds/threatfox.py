"""ThreatFox — indicators tied to malware families, from abuse.ch.

Where URLhaus answers "is this URL malicious", ThreatFox answers "which malware is this
indicator part of" — every record carries a family name and a confidence score, and its
indicators span URLs, domains, addresses and file hashes rather than URLs alone.

That family name is the reason to carry both feeds: it is what turns a list of strings into
something an analyst can pivot on.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from .base import FeedIoc, classify_value, host_of, parse_feed_time, split_tags

log = structlog.get_logger(__name__)

__all__ = ["FEED_NAME", "fetch_threatfox"]

FEED_NAME = "threatfox"
_URL = "https://threatfox.abuse.ch/export/json/recent/"

_MAX_ENTRIES = 4000


async def fetch_threatfox(
    client: httpx.AsyncClient, *, limit: int = _MAX_ENTRIES
) -> list[FeedIoc]:
    """Fetch and normalize the recent-IOC dump."""
    response = await client.get(_URL, headers={"Accept": "application/json"}, timeout=60.0)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        log.warning("threatfox: unexpected payload shape", got=type(payload).__name__)
        return []

    out: list[FeedIoc] = []

    for key in sorted(payload.keys(), key=_as_int, reverse=True):
        if len(out) >= limit:
            break
        entries = payload[key]
        if not isinstance(entries, list) or not entries:
            continue
        entry = entries[0]
        if not isinstance(entry, dict):
            continue

        value = entry.get("ioc_value")
        if not isinstance(value, str) or not value:
            continue

        # The feed's declared type wins where we recognise it: `ip:port` and a bare domain
        # can look alike, and ThreatFox knows which it meant.
        ioc_type = classify_value(value, entry.get("ioc_type"))
        if ioc_type is None:
            continue

        # The printable name is the human one ("Remus"); the raw one is a platform-prefixed
        # slug ("win.remus"). Prefer the readable one, keep the slug as a tag so the raw
        # identifier is still searchable.
        family = _clean(entry.get("malware_printable"))
        raw_family = _clean(entry.get("malware"))
        threat_type = _clean(entry.get("threat_type"))

        tags = split_tags(entry.get("tags"))
        for extra in (raw_family, threat_type):
            if extra and extra not in tags:
                tags.append(extra)

        confidence = entry.get("confidence_level")
        confidence = confidence if isinstance(confidence, int) else None

        note_parts = []
        if threat_type:
            note_parts.append(threat_type.replace("_", " "))
        if entry.get("is_compromised"):
            # A compromised host is a victim as well as an indicator — blocking it and
            # reporting it are different actions, and the distinction is worth surfacing.
            note_parts.append("host appears compromised rather than attacker-owned")
        note = "; ".join(note_parts).capitalize() or None

        out.append(
            FeedIoc(
                value=value,
                ioc_type=ioc_type,
                feed=FEED_NAME,
                host=host_of(value),
                tags=tags,
                threat=family or raw_family,
                note=note,
                confidence=confidence,
                feed_ref=_clean(entry.get("reference")),
                reporter=_clean(entry.get("reporter")),
                reported_at=parse_feed_time(entry.get("first_seen_utc")),
            )
        )

    log.info("threatfox fetched", indicators=len(out))
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
