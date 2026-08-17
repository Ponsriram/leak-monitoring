"""Turn what a leak site prints into what the database can query.

This module is the fix for the single most consequential defect in the old system: dates were
stored as whatever text the site used ("10 Feb, 2025"), so `$gte` against a real date matched
nothing and the weekly chart silently rendered empty for months.

Everything here is pure and total — no I/O, no exceptions on bad input. Unparseable input
returns None and the raw text is kept alongside for audit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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


# Status wording, weighted by how much it actually tells you.
#
# The previous version was an ordered tuple where the first pattern to match won outright.
# Two things made that wrong in practice:
#
#   * Order became precedence by accident. `sold` was checked first, so a listing whose
#     description merely used the word "purchased" outranked its own explicit
#     "Status: published" line.
#   * A banner word counted as much as a status field. Leak sites print "LEAKED DATA" across
#     the top of every page, so the single word "leaked" — the weakest possible evidence —
#     could decide the status of the first listing on the page.
#
# So each phrase now carries a weight, every phrase found is counted, and the status with the
# most evidence wins. Weight 3 is an unambiguous declaration, 2 is standard listing wording,
# 1 is a word that also appears in page furniture.
_STATUS_SIGNALS: tuple[tuple[re.Pattern[str], str, int], ...] = (
    # Explicit "Status: x" fields — the strongest signal a page can give.
    (re.compile(r"\bstatus\s*[:\-]\s*(published|leaked|disclosed)\b", re.I), "published", 5),
    (re.compile(r"\bstatus\s*[:\-]\s*(sold|purchased)\b", re.I), "sold", 5),
    (re.compile(r"\bstatus\s*[:\-]\s*(removed|deleted)\b", re.I), "removed", 5),
    # Unambiguous declarations.
    (re.compile(r"\b(sold|purchased|buyer\s+found)\b", re.I), "sold", 3),
    (re.compile(r"\b(removed|deleted|taken\s+down|withdrawn)\b", re.I), "removed", 3),
    # Split out of `removed`, which it contradicted: a listing under negotiation is still up.
    (re.compile(r"\b(negotiat\w*|in\s+talks|payment\s+pending)\b", re.I), "negotiating", 3),
    (re.compile(r"\b(paid|ransom\s+paid)\b", re.I), "negotiating", 2),
    # Standard listing wording.
    (re.compile(r"\b(full\s+dump|disclosed|released|published)\b", re.I), "published", 2),
    (
        re.compile(r"\b(countdown|deadline|time\s+left|expires?\s+in|days?\s+left)\b", re.I),
        "countdown",
        2,
    ),
    # Also appears in page headers, so it cannot outvote anything on its own.
    (re.compile(r"\b(leaked|leak)\b", re.I), "published", 1),
)

# Tie-break order, most decisive first. A listing that is both "sold" and "published" is
# reported as sold: publication is the default outcome, a sale is the specific event.
_STATUS_PRECEDENCE = ("removed", "sold", "negotiating", "published", "countdown")


def resolve_status(candidates: Iterable[str | None]) -> str:
    """Weigh every status phrase found for one listing and return the best-supported one.

    Takes all of a record's status text rather than the first fragment, because a listing
    usually states its state more than once ("Status: published" in the field, "released" in
    the prose) and the repetition is exactly what distinguishes a real status from a stray
    word in a sentence.
    """
    scores: dict[str, int] = {}

    for raw in candidates:
        if not raw:
            continue
        for pattern, status, weight in _STATUS_SIGNALS:
            # Every occurrence counts: three mentions of "sold" is stronger evidence than one.
            hits = len(pattern.findall(raw))
            if hits:
                scores[status] = scores.get(status, 0) + weight * hits

    if not scores:
        return "unknown"

    best = max(scores.values())
    tied = [status for status, score in scores.items() if score == best]
    if len(tied) == 1:
        return tied[0]

    for status in _STATUS_PRECEDENCE:
        if status in tied:
            return status
    return "unknown"  # pragma: no cover - every scored status is in the precedence list


def parse_status(raw: str | None) -> str:
    """Map a single blob of free text to the `leak_status` enum.

    Kept as the single-string entry point; `resolve_status` is what the linker uses once it
    has collected every status phrase belonging to a listing.
    """
    return resolve_status([raw])


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
