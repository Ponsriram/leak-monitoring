"""ransomware.live — victims posted on ransomware leak sites, as a clearnet JSON feed.

ransomware.live runs its own crawler across every leak site it tracks and publishes the
victims it finds. It is what the reference console's Ransomware section is built on (every
row there carries `"source": "ransomware.live"`), and it covers groups our own crawler does
not reach — sites behind CAPTCHAs, access queues, or addresses that rotated.

This is a feed, not a crawl: one HTTPS GET of `recentvictims`, no key, no Tor. The free API
is published for non-commercial use; a commercial deployment needs their paid PRO key.

Rows land in `leaks` beside the crawled ones, tagged `extraction.feed = "ransomware.live"` so
the Source column can say where each came from. Where the group is one we also crawl, the
alias table maps it onto our slug so the same victim is one row, not two.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from ..extract.describe import classify_incident
from ..extract.gazetteer import CCTLD_COUNTRY
from ..extract.normalize import parse_size
from ..models import ExtractedLeak, ExtractionMeta, ExtractionMethod

log = structlog.get_logger(__name__)

__all__ = ["FEED_NAME", "fetch_ransomware_live", "unwrap_description"]

FEED_NAME = "ransomware.live"
_URL = "https://api.ransomware.live/v2/recentvictims"

# ransomware.live's group name -> the slug our crawler files the same group under. Only
# groups we crawl ourselves need an entry, and only where the two spellings differ; every
# other name is stored as ransomware.live spells it (slugified by `ExtractedLeak`).
_GROUP_ALIASES = {
    "incransom": "inc-ransom",
    "lockbit3": "lockbit",
    "lockbit5": "lockbit",
    "arcusmedia": "arcus",
}

# Values the feed uses for "we do not know". Stored as null, not as a sector called
# "Not Found".
_UNKNOWN = {"", "n/a", "not found", "unknown", "none"}


async def fetch_ransomware_live(client: httpx.AsyncClient) -> list[ExtractedLeak]:
    """Fetch the most recent victims and normalize them into leak records."""
    response = await client.get(_URL, headers={"Accept": "application/json"}, timeout=60.0)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        log.warning("ransomware.live: unexpected payload shape", got=type(payload).__name__)
        return []

    out: list[ExtractedLeak] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        leak = _to_leak(entry)
        if leak is not None and leak.is_usable:
            out.append(leak)

    log.info("ransomware.live fetched", victims=len(out))
    return out


def _to_leak(entry: dict[str, Any]) -> ExtractedLeak | None:
    group = _clean(entry.get("group"))
    if not group:
        return None
    slug = group.strip().lower().replace(" ", "-")
    actor = _GROUP_ALIASES.get(slug, slug)

    country_code = _clean(entry.get("country"))
    size = parse_size(_clean(entry.get("data_size")))
    summary = _compose_summary(entry)

    discovered = _parse_time(entry.get("discovered"))
    attackdate_raw = _clean(entry.get("attackdate"))

    return ExtractedLeak(
        victim_name=_clean(entry.get("victim")),
        victim_domain=_clean(entry.get("domain")),
        victim_country=_country(country_code),
        victim_sector=_known(entry.get("activity")),
        actor_group=actor,
        # The ransomware.live permalink, not the onion claim URL: it is reachable without
        # Tor, and it links on to the claim page for anyone who needs it.
        source_url=_clean(entry.get("url")) or _clean(entry.get("claim_url")),
        # `discovered` is when the listing appeared on the leak site, which is what our
        # `published_at` means. `attackdate` is the group's own claim about when it broke in,
        # and is sometimes a future date — kept verbatim in the raw column instead.
        published_at=discovered,
        published_at_raw=attackdate_raw,
        leak_size_bytes=size,
        leak_type="ransomware",
        summary=summary,
        incident_types=classify_incident(
            summary=summary, leak_type="ransomware", status=None, leak_size_bytes=size
        ),
        extraction=ExtractionMeta(
            method=ExtractionMethod.FEED,
            feed=FEED_NAME,
            group=group,
            claim_url=_clean(entry.get("claim_url")),
            screenshot=_clean(entry.get("screenshot")),
            country_code=country_code,
        ),
    )


def _compose_summary(entry: dict[str, Any]) -> str | None:
    """Everything the feed says about the victim, in full, as paragraphs.

    The description is the leak site's own wording (or ransomware.live's company profile,
    which it labels "[AI generated]"). Press coverage, the ransom demand and the infostealer
    exposure figures are separate fields in the feed and are added as their own paragraphs,
    so nothing the source holds is dropped.
    """
    parts: list[str] = []

    description = unwrap_description(entry.get("description"))
    if description:
        parts.append(description)

    press = entry.get("press")
    if isinstance(press, dict):
        press_summary = _clean(press.get("summary"))
        press_source = _clean(press.get("source"))
        if press_summary or press_source:
            line = f"Press coverage: {press_summary}" if press_summary else "Press coverage"
            parts.append(f"{line} ({press_source})" if press_source else line)

    ransom = _clean(entry.get("ransom"))
    if ransom:
        parts.append(f"Ransom demand: {ransom}")

    size = _clean(entry.get("data_size"))
    if size:
        parts.append(f"Claimed data size: {size}")

    stealer = _infostealer_sentence(entry.get("infostealer"))
    if stealer:
        parts.append(stealer)

    text = "\n\n".join(parts).strip()
    return text or None


def unwrap_description(raw: object) -> str | None:
    """Undo the fixed-width hard wrap ransomware.live applies to descriptions.

    Descriptions arrive broken every 64 characters regardless of words — "sol\\nutions",
    "billi\\nng" — so rendering them as-is splits words in half. A line exactly as long as
    the block's wrap width was wrapped by the feed and is joined to the next with nothing in
    between (the original space, where there was one, is still at the end of the line). A
    shorter line ended where the author ended it, and its newline is kept.

    A description that answers "I don't have any reliable information" is the feed's AI
    profile saying it knows nothing; it carries no information and is dropped.
    """
    text = _clean(raw)
    if not text:
        return None
    if text.startswith("[AI generated] N/A"):
        return None

    lines = text.replace("\r\n", "\n").split("\n")
    lengths = [len(line) for line in lines if line]
    width = max(lengths, default=0)
    wrapped = width >= 40 and sum(1 for n in lengths if n == width) >= 2

    out: list[str] = []
    buffer = ""
    for line in lines:
        buffer += line
        if wrapped and len(line) == width:
            continue
        out.append(buffer.rstrip())
        buffer = ""
    if buffer:
        out.append(buffer.rstrip())

    joined = "\n".join(out)
    joined = re.sub(r"\n{3,}", "\n\n", joined).strip()
    return joined or None


def _infostealer_sentence(raw: object) -> str | None:
    """The Hudson Rock infostealer figures ransomware.live attaches, as one sentence."""
    if not isinstance(raw, dict):
        return None
    employees = raw.get("employees") or 0
    users = raw.get("users") or 0
    thirdparties = raw.get("thirdparties") or 0
    if not (employees or users or thirdparties):
        return None

    bits = []
    if employees:
        bits.append(f"{employees} employee{'s' if employees != 1 else ''}")
    if users:
        bits.append(f"{users} user{'s' if users != 1 else ''}")
    if thirdparties:
        bits.append(f"{thirdparties} third-party credential{'s' if thirdparties != 1 else ''}")
    sentence = f"Infostealer exposure (Hudson Rock): {', '.join(bits)} compromised"

    families = raw.get("infostealer_stats")
    if isinstance(families, dict) and families:
        named = ", ".join(f"{name} ({count})" for name, count in families.items())
        sentence += f"; stealer families: {named}"
    return sentence + "."


def _country(code: str | None) -> str | None:
    """ISO 3166 alpha-2 -> the country name the rest of the pipeline writes.

    An unmapped code is kept as the code rather than dropped: it is still what the source
    said, and a two-letter value in the column is better than a blank one.
    """
    if not code:
        return None
    return CCTLD_COUNTRY.get(code.lower(), code.upper())


def _known(value: object) -> str | None:
    text = _clean(value)
    if text is None or text.lower() in _UNKNOWN:
        return None
    return text


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
