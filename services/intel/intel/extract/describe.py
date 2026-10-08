"""A listing's own words, and what kind of incident they describe.

Two things the tables need that no single span carries:

* **A summary.** Leak sites print a paragraph under each victim — "Company Description:
  …", what was taken, what the crew is threatening. That paragraph is already in the page
  text; it just has no label, because it is prose rather than a field. It is recovered here
  as the text between a listing's anchor and the next listing's, with the page furniture
  (update stamps, view counters, bare dates) stripped out.

* **Incident types.** A listing is rarely one thing. A ransomware post that has published its
  files is also a data leak; one that says the data is for sale is also a sale. Types are
  multi-valued for that reason, and every one is derived from evidence we hold — the listing
  status, a stated size, or words in the listing's own text. Nothing is inferred from the
  crew's reputation, and a listing that says nothing specific stays tagged with only what
  its source guarantees.
"""

from __future__ import annotations

import re

from .gazetteer import is_country_name

# The canonical order. Stored and rendered in this order so the chips in a column line up
# row to row instead of shuffling with whichever keyword happened to match first.
INCIDENT_TYPES: tuple[str, ...] = (
    "ransomware",
    "data_breach",
    "data_leak",
    "hacked",
    "sale",
    "extortion",
    "credential_leak",
    "social_engineering",
    "ddos",
    "defacement",
    "dark_web",
)

# Word evidence, per type. Each pattern is matched against the listing's own text only —
# never the whole page — because a page header saying "LEAKED DATA" says nothing about
# which of the forty victims below it was leaked.
_TEXT_EVIDENCE: dict[str, re.Pattern[str]] = {
    "ransomware": re.compile(r"\bransom\w*|\bencrypt(?:ed|ion)\b", re.I),
    "data_breach": re.compile(
        r"\bbreach\w*|\bexfiltrat\w*|\bstole(?:n)?\b|\bunauthori[sz]ed access\b|"
        r"\b(?:we|they) (?:have )?(?:downloaded|obtained|took|taken)\b",
        re.I,
    ),
    "data_leak": re.compile(
        r"\bleak(?:ed|s)?\b|\bdump(?:ed)?\b|\bfull data\b|\bdata (?:is|was|has been|will be) "
        r"(?:published|released|disclosed)|\bpublish(?:ed|ing)\b|\breleased?\b",
        re.I,
    ),
    "hacked": re.compile(
        r"\bhack(?:ed|ers?)?\b|\bpwned\b|\bintrusion\b|\bcompromised\b|"
        r"\b(?:gained|got|obtained|have|full|admin|root|domain|network) access\b",
        re.I,
    ),
    "sale": re.compile(
        r"\bfor sale\b|\bselling\b|\bsells?\b|\bauction\w*|\bstarting (?:bid|price)\b|"
        r"\bprice\s*[:\-]|\bbuyer\b|\bpurchas\w*",
        re.I,
    ),
    "extortion": re.compile(
        r"\bextort\w*|\bdeadline\b|\bcountdown\b|\bdays? left\b|\bnegotiat\w*|"
        r"\bpay(?:ment)?\b.{0,40}\bor\b|\bif (?:they|you) (?:don'?t|do not|refuse to) pay\b",
        re.I,
    ),
    "credential_leak": re.compile(
        r"\bcredentials?\b|\bpasswords?\b|\bcombolists?\b|\blogins?\b|\busernames?\b", re.I
    ),
    "social_engineering": re.compile(
        r"\bphish\w*|\bsocial engineering\b|\bimpersonat\w*|\bpretext\w*", re.I
    ),
    "ddos": re.compile(r"\bddos\b|\bdenial[- ]of[- ]service\b", re.I),
    "defacement": re.compile(r"\bdefac\w*", re.I),
    "dark_web": re.compile(r"\bdark ?web\b|\bdarknet\b", re.I),
}


def classify_fields(
    *, leak_type: str | None, status: str | None, leak_size_bytes: int | None
) -> list[str]:
    """Types the structured fields alone guarantee.

    * The source is a ransomware leak site, so the listing is a ransomware incident.
    * A stated size is a claim that data was taken — a breach, whatever else it is.
    * `published` means the files are out; `sold` means a buyer took them; `countdown` and
      `negotiating` are an extortion still in progress.
    """
    found: set[str] = set()
    if leak_type:
        normalized = leak_type.strip().lower().replace(" ", "_").replace("-", "_")
        if normalized:
            found.add(normalized)
    if leak_size_bytes:
        found.add("data_breach")
    if status == "published":
        found.add("data_leak")
    elif status == "sold":
        found.add("sale")
    elif status in ("countdown", "negotiating"):
        found.add("extortion")
    return ordered(found)


def classify_text(text: str | None) -> list[str]:
    """Types the listing's own words give evidence for."""
    if not text:
        return []
    return ordered(name for name, pattern in _TEXT_EVIDENCE.items() if pattern.search(text))


def classify_incident(
    *,
    summary: str | None,
    leak_type: str | None,
    status: str | None,
    leak_size_bytes: int | None,
) -> list[str]:
    return ordered(
        {
            *classify_fields(leak_type=leak_type, status=status, leak_size_bytes=leak_size_bytes),
            *classify_text(summary),
        }
    )


def ordered(types: object) -> list[str]:
    """Deduplicate into the canonical order; anything unrecognised sorts after, by name."""
    unique = set(types)  # type: ignore[call-overload]
    rank = {name: index for index, name in enumerate(INCIDENT_TYPES)}
    return sorted(unique, key=lambda name: (rank.get(name, len(rank)), name))


# --- summary ---------------------------------------------------------------------------------

# How far past a victim the listing is allowed to run when no next listing is recognised.
# This is a boundary backstop, not a length limit on the summary: without it, a page whose
# next listing the extractor missed hands one victim the rest of the page. It is set well
# above any real listing description, and the summary itself is never shortened.
_MAX_WINDOW_CHARS = 8000
# Shorter than this is a label or a stray fragment, not something worth a column.
_MIN_SUMMARY_CHARS = 30

# A short all-caps line is a badge ("PUBLISHED FULL", "NEW"), not a sentence.
_BADGE = re.compile(r"^[^a-z]{2,24}$")

# Lines that are page furniture rather than prose: update stamps, bare dates, view and
# download counters, countdown clocks, a lone size or status word.
_FURNITURE = (
    re.compile(
        r"^\W*(?:updated|published|added|date|posted|last update|publication date|"
        r"date of publication|deadline|time left)\s*[:\-]",
        re.I,
    ),
    re.compile(r"^[\d\s.,:/\-]+(?:utc|gmt)?$", re.I),
    re.compile(r"^\W*\d+\s*(?:views?|visits?|downloads?)$", re.I),
    re.compile(r"^\W*(?:views?|visits?|downloads?)\s*[:\-]?\s*[\d.,]+[km]?$", re.I),
    _BADGE,
    re.compile(r"^\d+\s*[dD]\s*\d+\s*[hH](?:\s*\d+\s*[mM])?(?:\s*\d+\s*[sS])?$"),
    re.compile(r"^[\d.,]+\s*[kmgtp]i?b$", re.I),
    re.compile(
        r"^(?:published|leaked|sold|countdown|removed|negotiating|in progress|new|"
        r"download|more|read more|details|view|open)$",
        re.I,
    ),
)

_WHITESPACE = re.compile(r"\s+")

# Icon labels, badges and buttons that tile listings print on every tile. On inc-ransom each
# tile carries "Encrypted", "Proof" and "Views" beside its view counter; flattened into one
# text they became the summary of whichever victim came first.
_TILE_CHROME_WORDS = frozenset(
    {
        "encrypted", "proof", "proofs", "view", "views", "visits", "new", "hot", "top",
        "featured", "pinned", "verified", "updated", "read", "show", "open", "click", "here",
    }
)
_COUNTER = re.compile(r"^\W*[\d.,]+\s*[km]?\+?\W*$", re.I)
_WORD = re.compile(r"[^\W\d_]+")


def is_chrome_line(line: str) -> bool:
    """A line of a listing tile that is chrome — a counter, a date, an icon label — not content.

    The all-caps badge rule of the summary filter is left out on purpose: a tile can print its
    victim's name in capitals, and this test decides what may be a name.
    """
    text = line.strip(" \t|•·-–—")
    if not any(char.isalpha() for char in text) or _COUNTER.match(text):
        return True
    if any(pattern.match(text) for pattern in _FURNITURE if pattern is not _BADGE):
        return True
    words = _WORD.findall(text.lower())
    return bool(words) and all(word in _TILE_CHROME_WORDS for word in words)


def listing_window(
    text: str,
    *,
    anchor: tuple[int, int],
    own: set[tuple[int, int]],
    boundaries: list[tuple[int, int]],
) -> str:
    """The text between a listing's anchor and the next listing's.

    `boundaries` are the positions of every victim and domain span on the page, including
    ones that did not become a record: a listing the extractor missed still ends the one
    before it. `own` are this listing's spans, which must not end it — on a site that prints
    the link above the name, the name is the very next span after the anchor.
    """
    start = anchor[1]
    end = min(len(text), start + _MAX_WINDOW_CHARS)
    for boundary in boundaries:
        if boundary in own or boundary[0] < start:
            continue
        end = min(end, boundary[0])
        break
    return text[start:end]


def clean_summary(
    window: str, *, drop: tuple[str | None, ...] = (), tile: bool = False
) -> str | None:
    """Reduce a listing window to its prose.

    `drop` are values the row already shows in their own columns — the victim's name and
    domain — which some sites repeat on their own line inside the listing. `tile` also drops
    the icon labels and counters a listing tile prints (`is_chrome_line`).
    """
    dropped = {value.strip().lower() for value in drop if value}
    kept: list[str] = []
    for raw in window.splitlines():
        line = raw.strip(" \t|•·-–—")
        if len(line) < 3:
            continue
        if tile and is_chrome_line(line):
            continue
        lowered = line.lower()
        if lowered in dropped or lowered.rstrip("/") in dropped:
            continue
        if any(pattern.match(line) for pattern in _FURNITURE):
            continue
        # A country on its own line is the listing's location column, already shown in its
        # own column.
        if is_country_name(line):
            continue
        kept.append(line)

    summary = _WHITESPACE.sub(" ", " ".join(kept)).strip()
    if len(summary) < _MIN_SUMMARY_CHARS:
        return None
    # Kept whole, however long: the full description is the point of the column.
    return summary
