"""The shape every feed normalizes into, and the parsing shared between them."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..enrich.domains import normalize_domain

__all__ = ["FeedIoc", "classify_value", "host_of", "parse_feed_time", "split_tags"]

# Feeds report `1.2.3.4:8080` as an IP indicator. The port is part of the observation and is
# kept in `value`; the address on its own is what has to be recognised as an IP.
_IP_PORT = re.compile(r"^(?P<addr>[0-9a-f:.]+):(?P<port>\d{1,5})$", re.I)
_HASH = re.compile(r"^[a-f0-9]{32}$|^[a-f0-9]{40}$|^[a-f0-9]{64}$", re.I)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


@dataclass(slots=True)
class FeedIoc:
    """One indicator, normalized. Mirrors the `iocs` table."""

    value: str
    ioc_type: str
    feed: str
    host: str | None = None
    tags: list[str] = field(default_factory=list)
    threat: str | None = None
    note: str | None = None
    confidence: int | None = None
    feed_ref: str | None = None
    reporter: str | None = None
    reported_at: datetime | None = None


def parse_feed_time(raw: str | None) -> datetime | None:
    """Parse the timestamp formats abuse.ch actually emits.

    Both feeds use a space-separated `YYYY-MM-DD HH:MM:SS`, URLhaus with a trailing ` UTC`
    and ThreatFox without — and neither carries an offset, so the timezone has to be applied
    rather than parsed. They are UTC; a naive datetime here would be silently reinterpreted
    as local time and shift every indicator by the host's offset.
    """
    if not raw:
        return None
    text = raw.strip().removesuffix(" UTC").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def split_tags(raw: object) -> list[str]:
    """Feeds send tags as a list, a comma-joined string, or null. Normalize all three."""
    if isinstance(raw, list):
        values = [str(tag).strip() for tag in raw]
    elif isinstance(raw, str):
        values = [tag.strip() for tag in raw.split(",")]
    else:
        return []

    seen: list[str] = []
    for tag in values:
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _as_ip(value: str) -> str | None:
    """The address in `value`, whether bare or carrying a port. None if it is not one.

    The whole string is tried as an address *before* any port splitting, because a bare IPv6
    address is nothing but colons — splitting `2001:db8::1` on the first colon yields `2001`,
    which is not an address, and the indicator silently falls through to being classified as
    a domain. IPv6 with a port is conventionally bracketed (`[2001:db8::1]:443`), so there is
    no ambiguity to resolve here, only an ordering to get right.
    """
    candidate = value.strip().strip("[]")
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass

    match = _IP_PORT.match(candidate)
    if match:
        addr = match.group("addr").strip("[]")
        try:
            ipaddress.ip_address(addr)
            return addr
        except ValueError:
            return None
    return None


def host_of(value: str) -> str | None:
    """The hostname inside an indicator, if it has one.

    An IP literal is deliberately not a host: `domain_enrichment` is keyed by domain and
    joining an address to it would either miss or, worse, collide with a domain that happens
    to be spelled like one.
    """
    candidate = value
    if _URL.match(candidate):
        candidate = _URL.sub("", candidate)
        for separator in ("/", "?", "#"):
            candidate = candidate.split(separator, 1)[0]
        candidate = candidate.rsplit("@", 1)[-1]

    if _as_ip(candidate) is not None:
        return None

    # Only now is it safe to treat a colon as a port separator: anything colon-bearing that
    # was an address has already been ruled out above.
    candidate = candidate.split(":", 1)[0]
    return normalize_domain(candidate)


def classify_value(value: str, declared: str | None = None) -> str | None:
    """Map a feed's own type name, or the value's shape, onto our enum.

    `declared` wins when we recognise it, because the feed knows things the string does not —
    ThreatFox distinguishes `ip:port` from a bare domain, and both can look alike. Falling
    back to shape is what keeps a feed adding a new type name from dropping the indicator
    entirely.
    """
    if declared:
        name = declared.strip().lower()
        if name in {"url"}:
            return "url"
        if name in {"domain", "hostname", "domain_name"}:
            return "domain"
        if name in {"ip", "ip:port", "ipv4", "ipv6", "ip_address"}:
            return "ip"
        if name in {"md5_hash", "sha1_hash", "sha256_hash", "hash", "file_hash"}:
            return "file_hash"
        if name in {"email", "email_address"}:
            return "email"

    if _URL.match(value):
        return "url"
    if _HASH.match(value):
        return "file_hash"
    if "@" in value:
        return "email"

    if _as_ip(value) is not None:
        return "ip"

    return "domain" if normalize_domain(value.split(":", 1)[0]) else None
