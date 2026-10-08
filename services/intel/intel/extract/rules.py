"""Deterministic extractor. No model, no download, no GPU.

Leak-site listings are highly templated, and dates, sizes and domains are regular enough
that patterns catch most of them.

Where it is weak is identifying an organisation name in prose that carries no domain: a
name needs a legal suffix or a nearby domain before it is accepted (see `extract`).
"""

from __future__ import annotations

import re

from .describe import FIELD_LABEL, is_chrome_line
from .gazetteer import CCTLD_COUNTRY, COUNTRY_PATTERN, SECTOR_PATTERN, is_country_name
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


# A field label whose value is never the victim's name. Listings that label every field
# ("Company: | Aztec Software | Industry: | Engineering Software") otherwise hand the
# industry value to the org pattern as a second, Title Case, domain-adjacent victim.
_NON_VICTIM_LABEL = re.compile(
    r"\b(?:industry|sector|type|category|country|geo|location|revenue|status|gdpr)\s*:",
    re.I,
)


def _follows_non_victim_label(text: str, start: int) -> bool:
    """Is the line holding `start` the value of a non-victim field?

    Judged from the start of the line, not the match: an org match can begin mid-value
    ("Industry: Spa and Salon Management" yields "Salon Management").
    """
    line_start = text.rfind("\n", 0, start) + 1
    # "Industry: Business Services" on one line.
    if _NON_VICTIM_LABEL.match(text, line_start):
        return True
    # "Industry:" on the line above, as `to_text` renders separate elements.
    previous = text[max(0, line_start - 40) : line_start].rstrip()
    label = _NON_VICTIM_LABEL.search(previous)
    return label is not None and label.end() == len(previous)

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
            if _follows_non_victim_label(text, match.start()):
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

    def extract_block(self, text: str) -> list[Span]:
        """Spans for one listing block — a tile, a card, a table row — holding one victim.

        Inside a block the page has already said which text belongs together, so the two
        guards `extract` needs on a whole page are dropped. The victim is the block's first
        line that reads as a name (`block_victim_name`), with no legal suffix or nearby
        domain required; and a country or sector anywhere in the block is the victim's, with
        no distance window. Dates, sizes, statuses and domains are matched as on a page.

        A block whose name line cannot be found falls back to whatever `extract` accepted as a
        victim. At most one victim span is returned.
        """
        page_spans = self.extract(text)
        spans = [
            span
            for span in page_spans
            if span.label not in (Label.VICTIM, Label.LOCATION, Label.SECTOR)
            # The domain of an email address is a mailbox, not the victim's site: on a
            # notice card it is the crew's own contact ("…@onionmail.org").
            and not (span.label == Label.VICTIM_URL and text[max(span.start - 1, 0)] == "@")
        ]

        named = block_victim_name(text)
        if named is not None:
            name, start, end = named
            spans.append(Span(Label.VICTIM, name, start, end, 0.6))
        else:
            fallback = next((s for s in page_spans if s.label == Label.VICTIM), None)
            if fallback is not None:
                spans.append(fallback)
        name_at = next(((s.start, s.end) for s in spans if s.label == Label.VICTIM), None)
        taken = [(s.start, s.end) for s in spans]

        def outside_name(match: re.Match[str]) -> bool:
            # "Jordan Electric" is not in Jordan; the name's own words already feed the sector.
            return name_at is None or match.end() <= name_at[0] or match.start() >= name_at[1]

        def free(match: re.Match[str]) -> bool:
            # A country inside another span is part of it: the "GB" of "61 GB", the "us" of
            # a domain. Read as countries they outrank the domain's ccTLD.
            return all(match.end() <= start or match.start() >= end for start, end in taken)

        for match in COUNTRY_PATTERN.finditer(text):
            if free(match):
                spans.append(
                    Span(Label.LOCATION, match.group(1), match.start(), match.end(), 0.7)
                )
        for match in _FLAG_EMOJI.finditer(text):
            country = CCTLD_COUNTRY.get(_flag_code(match.group(0)))
            if country:
                spans.append(Span(Label.LOCATION, country, match.start(), match.end(), 0.8))
        for match in SECTOR_PATTERN.finditer(text):
            if outside_name(match):
                spans.append(
                    Span(Label.SECTOR, match.group(1), match.start(), match.end(), 0.6)
                )

        spans.sort(key=lambda span: (span.start, span.end))
        return _dedupe_overlaps(spans)


# A field whose value is the victim's name: "Company: Acme", "Victim - Acme".
_NAME_FIELD = re.compile(
    r"^(?:company|victim|target|organi[sz]ation|name)\s*[:\-–]\s*(?P<value>\S.*)$", re.I
)

# A name is a short line. A long one, or a multi-word sentence, is the tile's description —
# which means this block has no name line of its own.
_MAX_NAME_CHARS = 80
_MAX_NAME_WORDS = 10

# A label alone on its line, its value on the next ("Revenue:" / "$100 million"); and the
# labels whose value is the victim's name.
_LABEL_ONLY = FIELD_LABEL
_NAME_LABEL = re.compile(r"(?:company(?: name)?|victim|target|organi[sz]ation|name)\s*:", re.I)

# A money amount or a bare figure is a field's value, never a name: "$170 million", "€585 m",
# "431.6 million". A figure only counts when it is the whole line — "3M Company" and
# "7-Eleven" are names.
_AMOUNT = re.compile(
    r"^(?:[$€£¥₹]|(?:usd|eur|gbp)\b)"
    r"|^[\d.,\s]+(?:million|billion|thousand|mln|bn|[kmb])?\s*(?:usd|eur|gbp|[$€£])?$",
    re.I,
)

# Site notices on rhysida-style card grids talk to the reader: "How you can buy BTC", "We
# will post news about our company here". Company names do not say "we" or "you".
_ADDRESSES_A_READER = re.compile(r"\b(?:we|you|your|we'll|you'll|we're|you're)\b", re.I)

# A flag emoji is two regional-indicator letters spelling an ISO country code.
_FLAG_EMOJI = re.compile("[\U0001f1e6-\U0001f1ff]{2}")


def _flag_code(flag: str) -> str:
    return "".join(chr(ord(char) - 0x1F1E6 + ord("a")) for char in flag)


def block_victim_name(text: str) -> tuple[str, int, int] | None:
    """The victim's name in one listing block: (name, start, end), or None.

    The first line that is not chrome is the name. Skipped on the way: counters, dates,
    sizes, money amounts, status words, icon labels (`describe.is_chrome_line`), lines made
    only of words in `_NOT_AN_ORG`, countries, field labels such as "Revenue: $5M", and bare
    domains — those are the victim's site, and a tile that prints the link above the name
    still has a name.

    Fields come in two shapes and both are read: "Company: Acme" on one line yields "Acme";
    a label alone on its line ("Name:") makes the next line its value — the name, even a
    bare domain, after a name label; something to skip ("$170 million" after "Revenue:")
    after any other.

    A first content line that reads as a sentence — too long, a question or an exclamation,
    or speaking as "we" or "you" — means the block is a notice, not a victim, and None is
    returned rather than a sentence.
    """
    lines: list[tuple[str, int]] = []
    offset = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if line:
            lines.append((line, offset + raw.index(line)))
        offset += len(raw) + 1

    index = 0
    while index < len(lines):
        line, start = lines[index]
        index += 1

        field = _NAME_FIELD.match(line)
        if field is not None:
            value = field.group("value").strip()
            at = start + field.start("value")
            return value, at, at + len(value)

        if _LABEL_ONLY.fullmatch(line):
            if index < len(lines) and not _LABEL_ONLY.fullmatch(lines[index][0]):
                value, at = lines[index]
                index += 1
                if _NAME_LABEL.fullmatch(line) and _is_name_like(value):
                    return value, at, at + len(value)
            continue

        if _is_chrome_or_field(line):
            continue
        if not _is_name_like(line):
            return None
        return line, start, start + len(line)
    return None


def _is_name_like(line: str) -> bool:
    """Short, and not a sentence: the shape of a company's name."""
    words = line.split()
    if len(line) > _MAX_NAME_CHARS or len(words) > _MAX_NAME_WORDS:
        return False
    if "?" in line or "!" in line or _ADDRESSES_A_READER.search(line):
        return False
    if _AMOUNT.match(line):
        return False
    return not (line.endswith(".") and len(words) > 4)


def _is_chrome_or_field(line: str) -> bool:
    if is_chrome_line(line) or is_country_name(line):
        return True
    if any(pattern.fullmatch(line) for pattern in _DATE_PATTERNS):
        return True
    if _SIZE_PATTERN.fullmatch(line) or _STATUS_PATTERN.fullmatch(line):
        return True
    if _AMOUNT.match(line):
        return True
    if _NON_VICTIM_LABEL.match(line) or _FIELD_LINE.match(line):
        return True
    bare = line.removeprefix("https://").removeprefix("http://").rstrip("/")
    if _DOMAIN_PATTERN.fullmatch(bare):
        return True
    words = [word.strip(".,:;").lower() for word in line.split()]
    return all(word in _NOT_AN_ORG for word in words if word)


# A field of the tile, not its name: "Revenue: $5M", "Files: 120", or a label printed on its
# own line above its value ("Description:").
_FIELD_LINE = re.compile(r"^[^\W\d][\w .'&/-]{0,30}:(?:\s|$)")


# Words that appear in page furniture but never inside a victim's registered name.
#
# Navigation links and table headers such as "How To Buy Bitcoin", "File Name" and
# "Affiliate Rules" read as victims otherwise. Every term here was observed on an actual
# page, not guessed.
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
        "more",  # "Read More", "Learn More", "Show More" buttons on every card
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
