"""Turn what a leak site prints into what the database can query.

This module is the fix for the single most consequential defect in the old system: dates were
stored as whatever text the site used ("10 Feb, 2025"), so `$gte` against a real date matched
nothing and the weekly chart silently rendered empty for months.

Everything here is pure and total — no I/O, no exceptions on bad input. Unparseable input
returns None and the raw text is kept alongside for audit.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# Order matters: the most specific formats first. %d/%m before %m/%d is a deliberate choice —
# these sites are overwhelmingly European/Russian in origin, so 03/04/2026 is 3 April.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b, %Y",
    "%d %B, %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%m/%d/%Y",
)

_ISO_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_date(raw: str | None) -> datetime | None:
    """Best-effort parse of a publication date. Returns None rather than guessing wildly.

    Always returns a timezone-aware UTC datetime: the column is `timestamptz`, and a naive
    datetime would be interpreted in the server's zone, silently shifting every date.
    """
    if not raw:
        return None

    text = raw.strip()
    if not text:
        return None

    # Strip common decoration: "Published: ", trailing "(UTC)", surrounding brackets.
    text = re.sub(r"^\s*(published|updated|date|added)\s*[:\-]\s*", "", text, flags=re.I)
    text = re.sub(r"\s*\((utc|gmt)\)\s*$", "", text, flags=re.I)
    text = text.strip("[](){} \t\n\r")

    if _ISO_LIKE.match(text):
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=UTC)
        except ValueError:
            pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue

    return None


_SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "k": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "m": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "g": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "t": 1024**4,
    "tib": 1024**4,
    "pb": 1024**5,
    "pib": 1024**5,
}

_SIZE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[kmgtp]?i?b|[kmgt])\b",
    re.IGNORECASE,
)


def parse_size(raw: str | None) -> int | None:
    """"1.2 TB" -> bytes. Returns None when no size is stated, which is common."""
    if not raw:
        return None

    match = _SIZE_RE.search(raw)
    if not match:
        return None

    # "1,5 GB" is decimal-comma in a lot of European listings, not a thousands separator:
    # the regex only captures one group of digits after the separator.
    value = float(match.group("value").replace(",", "."))
    unit = match.group("unit").lower()

    multiplier = _SIZE_UNITS.get(unit)
    if multiplier is None:
        return None

    return int(value * multiplier)


_STATUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(sold|purchased|buyer\s+found)\b", re.I), "sold"),
    (re.compile(r"\b(published|leaked|disclosed|full\s+dump|released)\b", re.I), "published"),
    (
        re.compile(r"\b(countdown|deadline|time\s+left|expires?\s+in|days?\s+left)\b", re.I),
        "countdown",
    ),
    (re.compile(r"\b(removed|deleted|paid|negotiat)\w*\b", re.I), "removed"),
)


def parse_status(raw: str | None) -> str:
    """Map free text to the `leak_status` enum. Unknown is a legitimate answer."""
    if not raw:
        return "unknown"
    for pattern, status in _STATUS_PATTERNS:
        if pattern.search(raw):
            return status
    return "unknown"


_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})\b",
    re.IGNORECASE,
)

# Leak sites are full of their own infrastructure and boilerplate links; those are not victims.
_DOMAIN_DENYLIST = frozenset(
    {
        "onion",
        "tor",
        "torproject.org",
        "protonmail.com",
        "proton.me",
        "tutanota.com",
        "telegram.org",
        "t.me",
        "qtox.github.io",
        "session.org",
        "bitcoin.org",
        "example.com",
    }
)


def extract_domain(text: str | None) -> str | None:
    """Pull the first plausible victim domain out of a blob of text."""
    if not text:
        return None

    for match in _DOMAIN_RE.finditer(text):
        domain = match.group(1).lower().rstrip(".")
        # Strip www. here, not only in the Pydantic validator. Both paths must agree or
        # "www.acme.example" and "acme.example" produce two different dedupe hashes for
        # one victim.
        if domain.startswith("www."):
            domain = domain[4:]
        if domain.endswith(".onion"):
            continue
        if domain in _DOMAIN_DENYLIST:
            continue
        if any(domain.endswith("." + blocked) for blocked in _DOMAIN_DENYLIST):
            continue
        return domain

    return None
