"""Deterministic extractor. No model, no download, no GPU.

This is the default so the pipeline works the moment it is installed, and so the linker and
storage layers can be tested without an ML dependency. It is genuinely useful, not a stub:
leak-site listings are highly templated, and dates, sizes and domains are regular enough
that patterns catch most of them.

Where it is weak is exactly where a model earns its place: identifying an organisation name
in prose that carries no domain. Run `GlinerExtractor` for that.
"""

from __future__ import annotations

import re

from .gazetteer import COUNTRY_PATTERN, SECTOR_PATTERN, is_country_name
from .linker import Label, Span
from .normalize import _DOMAIN_DENYLIST  # noqa: PLC2701 - shared denylist, single source

_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b"),
    re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*,?\s+\d{4}\b",
        re.I,
    ),
    re.compile(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
        re.I,
    ),
)

_SIZE_PATTERN = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:[kmgtp]i?b)\b", re.I)

# Deliberately wider than the set of phrases `normalize.resolve_status` weighs: this pattern
# only decides what text is worth capturing as a status span, and the weighing happens later.
# An explicit "Status: x" field is matched as one span so the field form keeps its weight.
_STATUS_PATTERN = re.compile(
    r"\b(status\s*[:\-]\s*\w+|published|leaked|leak|disclosed|released|full\s+dump|"
    r"sold|purchased|buyer\s+found|countdown|deadline|time\s+left|days?\s+left|"
    r"expires?\s+in|removed|deleted|taken\s+down|withdrawn|negotiat\w*|"
    r"in\s+talks|payment\s+pending|paid)\b",
    re.I,
)

_DOMAIN_PATTERN = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})\b",
    re.I,
)

# Title Case runs of 1-5 words, optionally followed by a company suffix. Catches
# "Northwind Logistics" and "Contoso Manufacturing Ltd" without catching sentence starts,
# because a suffix or a nearby domain is required (see `extract`).
#
# The inter-word separator is `[ \t]+`, NOT `\s+`: `\s` matches newlines, so a page header
# followed by the first company name ran together into one span
# ("LOCKBIT LEAKED DATA\n\nNorthwind Logistics"). Company names do not span lines.
_ORG_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.-]*(?:[ \t]+[A-Z][A-Za-z0-9&'’.-]*){0,4}"
    r"(?:[ \t]+(?:Inc|LLC|Ltd|Limited|GmbH|SA|SAS|BV|NV|AB|AG|Corp|Corporation|Group|"
    r"Holdings|Industries|Technologies|Solutions|Systems|Services|Partners|"
    r"Associates|Manufacturing|Logistics|Health|Medical|Financial|Bank)\b\.?)?)",
)

_ORG_SUFFIX = re.compile(
    r"\b(Inc|LLC|Ltd|Limited|GmbH|SA|SAS|BV|NV|AB|AG|Corp|Corporation|Group|Holdings|"
    r"Industries|Technologies|Solutions|Systems|Services|Partners|Associates|"
    r"Manufacturing|Logistics|Health|Medical|Financial|Bank)\b",
    re.I,
)


# How far from a victim or a domain a country or sector mention may sit and still be read
# as belonging to that listing.
#
# The window is the whole point. `COUNTRY_PATTERN` and `SECTOR_PATTERN` are alternations
# over several hundred words, so sweeping a page with them unguarded matches the footer
# ("© 2026 … United States"), the navigation and the crew's own boilerplate, and the linker
# — which attributes by reading order — would hand every one of those to whichever victim
# happened to precede it. Requiring the mention to sit near an actual victim or domain span
# is what separates "this listing is German" from "this page mentions Germany".
_TAG_WINDOW_CHARS = 250


class RulesExtractor:
    """Pattern-based extraction. Deterministic and fast."""

    name = "rules"

    def extract(self, text: str) -> list[Span]:
        spans: list[Span] = []

        for pattern in _DATE_PATTERNS:
            for match in pattern.finditer(text):
                spans.append(
                    Span(Label.DATE, match.group(0), match.start(), match.end(), 0.9)
                )

        for match in _SIZE_PATTERN.finditer(text):
            spans.append(Span(Label.SIZE, match.group(0), match.start(), match.end(), 0.95))

        for match in _STATUS_PATTERN.finditer(text):
            spans.append(
                Span(Label.STATUS, match.group(0), match.start(), match.end(), 0.8)
            )

        for match in _DOMAIN_PATTERN.finditer(text):
            domain = match.group(1).lower()
            if domain.endswith(".onion") or domain in _DOMAIN_DENYLIST:
                continue
            if any(domain.endswith("." + blocked) for blocked in _DOMAIN_DENYLIST):
                continue
            spans.append(
                Span(Label.VICTIM_URL, match.group(1), match.start(), match.end(), 0.85)
            )

        for match in _ORG_PATTERN.finditer(text):
            candidate = match.group(1).strip()
            if len(candidate) < 3 or (" " not in candidate and not _ORG_SUFFIX.search(candidate)):
                continue
            if not _is_plausible_org(candidate):
                continue
            # Require some corroboration: a legal suffix, or a domain within 120 characters.
            # Without this every capitalised sentence opener becomes a victim.
            window = text[max(0, match.start() - 120) : match.end() + 120]
            if not _ORG_SUFFIX.search(candidate) and not _DOMAIN_PATTERN.search(window):
                continue
            spans.append(
                Span(Label.VICTIM, candidate, match.start(), match.end(), 0.6)
            )

        # Country and sector are matched last, because whether a mention counts depends on
        # where the victim and domain spans landed.
        anchors = [
            (span.start, span.end)
            for span in spans
            if span.label in (Label.VICTIM, Label.VICTIM_URL)
        ]
        for match in COUNTRY_PATTERN.finditer(text):
            if _near_an_anchor(match.start(), match.end(), anchors):
                spans.append(
                    Span(Label.LOCATION, match.group(1), match.start(), match.end(), 0.7)
                )
        for match in SECTOR_PATTERN.finditer(text):
            if _near_an_anchor(match.start(), match.end(), anchors):
                spans.append(
                    Span(Label.SECTOR, match.group(1), match.start(), match.end(), 0.6)
                )

        # The linker is order-sensitive by design — restore document order.
        spans.sort(key=lambda span: (span.start, span.end))
        return _dedupe_overlaps(spans)


# Words that appear in page furniture but never inside a victim's registered name.
#
# The additions below came from the first real crawl: a live LockBit mirror yielded
# "How To Buy Bitcoin", "File Name" and "Affiliate Rules" as victims — navigation links and
# a table header. Every term here was observed on an actual page, not guessed.
_NOT_AN_ORG = frozenset(
    {
        # disclosure vocabulary
        "leaked", "leak", "leaks", "published", "disclosed", "released",
        "sold", "countdown", "deadline", "ransomware", "dump", "dumps",
        # generic nouns that show up in table headers
        "data", "files", "file", "name", "size", "status", "date", "time",
        "download", "downloads", "upload", "uploads", "link", "links",
        "victim", "victims", "company", "companies", "description", "info",
        "information", "details", "price", "total", "count", "page", "pages",
        # site navigation
        "news", "blog", "contact", "about", "home", "archive", "disclosures",
        "rules", "affiliate", "affiliates", "how", "faq", "help", "support",
        "search", "login", "register", "menu", "index", "list", "all", "full",
        "terms", "policy", "privacy", "mirror", "mirrors", "onion", "tor",
        # payment / negotiation chrome
        "bitcoin", "btc", "monero", "xmr", "buy", "payment", "pay", "wallet",
        "escrow", "negotiation", "negotiations", "chat", "decrypt", "decryptor",
    }
)


def _is_plausible_org(candidate: str) -> bool:
    """Reject page furniture that happens to be capitalised.

    Two cheap filters that between them remove most false positives:

    * ALL-CAPS with no legal suffix is a banner ("LOCKBIT LEAKED DATA"), not a company.
      Listings render real company names in Title Case.
    * A candidate that is exactly a country name is the listing's location column.
    * Any word that only ever appears in site chrome disqualifies the whole candidate —
      no registered company is called "Leaked Data".
    """
    words = candidate.split()

    if candidate.isupper() and not _ORG_SUFFIX.search(candidate):
        return False

    # A country name on its own is a location, not a company. It reads as a perfectly good
    # organisation to every other test here — Title Case, multi-word, sitting beside a
    # domain — and once accepted it opens a record that steals the real victim's domain.
    if is_country_name(candidate):
        return False

    return all(word.strip(".,").lower() not in _NOT_AN_ORG for word in words)


def _near_an_anchor(
    start: int, end: int, anchors: list[tuple[int, int]], window: int = _TAG_WINDOW_CHARS
) -> bool:
    """True when this match sits within `window` characters of a victim or domain span."""
    return any(
        start - anchor_end <= window and anchor_start - end <= window
        for anchor_start, anchor_end in anchors
    )


def _dedupe_overlaps(spans: list[Span]) -> list[Span]:
    """Drop spans fully contained inside an earlier span of the same label."""
    kept: list[Span] = []
    for span in spans:
        if any(
            other.label == span.label and other.start <= span.start and other.end >= span.end
            for other in kept
        ):
            continue
        kept.append(span)
    return kept
