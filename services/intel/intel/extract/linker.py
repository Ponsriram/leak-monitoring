"""Turn a flat list of entity spans into discrete leak records.

This is the most important piece of business logic in the project. Two decisions keep it
simple:

1. **Extraction is per page, not per corpus.** Per page, a victim span and the date next to
   it are unambiguously related; across a blob of every site concatenated together they are
   not.

2. **The actor group comes from the source, not from the text.** We know which site we
   crawled — guessing the group from prose was always redundant. A group span in the text
   can still override it (some sites republish other crews' listings), but the default is
   simply known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ExtractedLeak, ExtractionMeta, LeakStatus
from .describe import classify_incident, clean_summary, listing_window
from .gazetteer import resolve_country, resolve_sector
from .normalize import extract_domain, parse_date, parse_size, resolve_status


class Label:
    """Entity labels. Whatever extractor is in use must emit these strings."""

    VICTIM = "victim_org"
    VICTIM_URL = "victim_url"
    GROUP = "ransomware_group"
    DATE = "date"
    SIZE = "leak_size"
    STATUS = "status"
    # Where the victim is and what it does — the labels that fill `victim_country` and
    # `victim_sector`.
    LOCATION = "location"
    SECTOR = "sector"


@dataclass(slots=True)
class Span:
    label: str
    text: str
    start: int = 0
    end: int = 0
    confidence: float | None = None


@dataclass(slots=True)
class _Record:
    """A leak under construction."""

    victim_name: str | None = None
    victim_url: str | None = None
    date_raw: str | None = None
    size_raw: str | None = None
    # Every status phrase belonging to this listing, not just the first. A listing typically
    # states its state more than once, and `resolve_status` weighs the whole set — keeping
    # only the first meant a stray word in the description outranked the status field below
    # it purely because it appeared earlier on the page.
    status_raws: list[str] = field(default_factory=list)
    # Collected rather than first-wins, for the same reason as `status_raws`: a listing that
    # names its victim's country twice is stronger evidence than one that mentions a country
    # in passing, and `resolve_*` weighs the whole set.
    location_raws: list[str] = field(default_factory=list)
    sector_raws: list[str] = field(default_factory=list)
    group_override: str | None = None
    confidences: list[float] = field(default_factory=list)
    # Where the record's identifying spans sit on the page, so its description can be cut
    # from the text around them.
    victim_pos: tuple[int, int] | None = None
    url_pos: tuple[int, int] | None = None
    summary: str | None = None


def link_spans(
    spans: list[Span],
    *,
    source_group: str,
    source_url: str | None = None,
    page_no: int = 1,
    method: str = "rules",
    model_version: str | None = None,
    page_text: str | None = None,
) -> list[ExtractedLeak]:
    """Group spans into leaks.

    A victim span opens a new record; attribute spans that follow attach to it. Attributes
    seen before any victim are held and applied to the first record, which covers sites that
    print the date above the company name.

    The victim's own URL is the exception: it binds to the *nearest* victim span, before or
    after. Reading order alone is wrong for it, because plenty of sites print the link above
    the company it belongs to:

        https://affiniahealthcare.org/
        Affinia Healthcare
        https://www.jdyoung.com/
        JD Young

    Under "attributes follow the victim", jdyoung.com attached to Affinia Healthcare and
    every listing on the page ended up with the next listing's domain. That is not a
    cosmetic error: `victim_domain` is what `dedupe_hash` is built from and what the
    enrichment joins on, so it silently filed each victim under another company's identity.

    Dates, sizes and statuses keep the reading-order rule, because those genuinely do follow
    the name — including trailing prose ("…have been released.") that sits closer to the
    *next* victim than to its own, which is exactly the case nearest-span would get wrong.

    When `page_text` is passed, each record also gets its summary: the prose between its
    anchor and the next listing's (see `_attach_summaries`).
    """
    records: list[_Record] = []
    pending = _Record()  # attributes seen before the first victim span
    current: _Record | None = None
    # A group span applies to every record after it on the page, including ones already
    # opened but not yet closed — leak sites print the crew name once, in the header.
    page_group: str | None = None

    # Where each victim span sits, so a URL can be matched to the closest one.
    victim_positions = [
        (span.start, span.end) for span in spans if span.label == Label.VICTIM
    ]
    # Which record index each URL span belongs to, keyed by the span's start offset.
    url_owner = _assign_urls_to_nearest_victim(spans, victim_positions)
    attach = _attach

    # URL spans are applied after every record exists, since one may belong to a victim that
    # has not been read yet.
    deferred_urls: list[Span] = []

    for span in spans:
        text = span.text.strip()
        if not text:
            continue

        if span.label == Label.GROUP:
            page_group = text
            continue

        if span.label == Label.VICTIM:
            current = _Record(
                victim_name=text,
                group_override=page_group,
                victim_pos=(span.start, span.end),
            )
            # Fold in anything that appeared before the first victim on this page.
            if not records and pending is not None:
                current.date_raw = pending.date_raw
                current.size_raw = pending.size_raw
                current.status_raws.extend(pending.status_raws)
                current.location_raws.extend(pending.location_raws)
                current.sector_raws.extend(pending.sector_raws)
                current.confidences.extend(pending.confidences)
                pending = _Record(victim_url=pending.victim_url, url_pos=pending.url_pos)
            records.append(current)
            continue

        if span.label == Label.VICTIM_URL:
            deferred_urls.append(span)
            continue

        attach(current if current is not None else pending, span)

    for span in deferred_urls:
        owner = url_owner.get(span.start)
        if owner is None or owner >= len(records):
            # No victim on the page at all — hold it, so a bare URL can still become a
            # listing below.
            attach(pending, span)
            continue
        attach(records[owner], span)

    records = _merge_split_listings(records)

    # A page can carry a bare URL with no company name — still a real listing.
    if not records and (pending.victim_url or pending.date_raw):
        pending.group_override = page_group
        records.append(pending)

    if page_text:
        _attach_summaries(records, spans, page_text)

    leaks: list[ExtractedLeak] = []
    for record in records:
        leak = _to_leak(
            record,
            source_group=record.group_override or page_group or source_group,
            source_url=source_url,
            page_no=page_no,
            method=method,
            model_version=model_version,
        )
        # Silently dropping unusable records would hide extractor regressions, but keeping
        # them would poison dedupe (every nameless record hashes identically). Callers get
        # the count via the pipeline's logging.
        if leak.is_usable:
            leaks.append(leak)

    return leaks


def _attach(record: _Record, span: Span) -> None:
    """Fold one attribute span into a record."""
    if span.confidence is not None:
        record.confidences.append(span.confidence)

    match span.label:
        case Label.VICTIM_URL:
            # Don't let a second URL clobber the first — the first is the victim's own
            # site; later ones are usually mirrors or the leak download link.
            if record.victim_url is None:
                record.victim_url = span.text
                record.url_pos = (span.start, span.end)
        case Label.DATE:
            record.date_raw = record.date_raw or span.text
        case Label.SIZE:
            record.size_raw = record.size_raw or span.text
        case Label.STATUS:
            record.status_raws.append(span.text)
        case Label.LOCATION:
            record.location_raws.append(span.text)
        case Label.SECTOR:
            record.sector_raws.append(span.text)


def leak_from_fields(
    *,
    victim_name: str | None,
    victim_url: str | None,
    date_raw: str | None,
    location_raws: list[str],
    summary: str | None,
    source_group: str,
    source_url: str | None,
    page_no: int,
    mode: str,
    method: str = "rules",
) -> ExtractedLeak | None:
    """A leak from fields a source already gives one by one (see `extract.json_items`).

    Normalized exactly as a linked record is — domain, country and sector, date, incident
    types — so a structured record and a scraped one cannot disagree about the same victim.
    """
    record = _Record(
        victim_name=victim_name,
        victim_url=victim_url,
        date_raw=date_raw,
        location_raws=location_raws,
        summary=summary,
    )
    leak = _to_leak(
        record,
        source_group=source_group,
        source_url=source_url,
        page_no=page_no,
        method=method,
        model_version=None,
        mode=mode,
    )
    return leak if leak.is_usable else None


def link_block(
    spans: list[Span],
    *,
    block_text: str,
    source_group: str,
    source_url: str | None = None,
    page_no: int = 1,
    method: str = "rules",
    model_version: str | None = None,
    mode: str = "tile",
    drop: tuple[str, ...] = (),
) -> ExtractedLeak | None:
    """One listing block (a tile, a card, a row) -> at most one leak.

    The block boundary is the page's own: everything inside belongs to the one victim it
    shows, so there is no reading-order attribution to get wrong. The first victim span names
    the record and every other span is its attribute; a second victim span is ignored rather
    than opening a record of its own. The summary is the block's own text with its chrome
    removed, so nothing of the next tile can bleed into it.

    `mode` is recorded in `extraction.mode`: "tile", or "detail" for a victim's own page read
    the same way (see `pipeline.extract_detail`). `drop` are more lines to keep out of the
    summary, as the name and domain are.
    """
    record = _Record()
    group: str | None = None
    for span in spans:
        text = span.text.strip()
        if not text:
            continue
        if span.label == Label.GROUP:
            group = group or text
        elif span.label == Label.VICTIM:
            if record.victim_name is None:
                record.victim_name = text
                record.victim_pos = (span.start, span.end)
                if span.confidence is not None:
                    record.confidences.append(span.confidence)
        else:
            _attach(record, span)

    if record.victim_name is None and record.victim_url is None:
        return None

    record.summary = clean_summary(
        block_text,
        drop=(record.victim_name, record.victim_url, extract_domain(record.victim_url), *drop),
        tile=True,
    )
    leak = _to_leak(
        record,
        source_group=group or source_group,
        source_url=source_url,
        page_no=page_no,
        method=method,
        model_version=model_version,
        mode=mode,
    )
    return leak if leak.is_usable else None


def _attach_summaries(records: list[_Record], spans: list[Span], text: str) -> None:
    """Give each record the prose between its anchor and the next listing's.

    The anchor is the record's link where it has one, its name otherwise. Links are the
    best-placed span on these pages — they sit at the top of a listing — while a name span
    can be a capitalised phrase lifted out of the previous listing's prose, which would
    start the summary in the wrong listing.

    For the same reason a name span only ends another listing's window when it is that
    listing's anchor. Every link does, attached or not: an unextracted listing still ends
    the one above it.
    """
    boundaries = sorted(
        {anchor for record in records if (anchor := record.url_pos or record.victim_pos)}
        | {(span.start, span.end) for span in spans if span.label == Label.VICTIM_URL}
    )
    for record in records:
        anchor = record.url_pos or record.victim_pos
        if anchor is None:
            continue
        # The record's other span is skipped as a boundary, and if it sits on a line of its
        # own `clean_summary` drops it — so a name printed under its link neither ends the
        # description nor opens it.
        own = {pos for pos in (record.victim_pos, record.url_pos) if pos is not None}
        window = listing_window(text, anchor=anchor, own=own, boundaries=boundaries)
        record.summary = clean_summary(
            window,
            drop=(record.victim_name, record.victim_url, extract_domain(record.victim_url)),
        )


# Words that are a company-name *part*, never a company name. A listing rendered as
# "Acme Holdings Ltd" can leave the extractor holding just "Ltd"; a table section headed
# "Financial" becomes the name of every victim under it.
_NAME_FRAGMENTS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "sa",
        "sas",
        "bv",
        "nv",
        "ab",
        "ag",
        "corp",
        "corporation",
        "group",
        "holdings",
        "industries",
        "technologies",
        "solutions",
        "systems",
        "services",
        "partners",
        "associates",
        "manufacturing",
        "logistics",
        "health",
        "medical",
        "financial",
        "bank",
        # Section headings seen standing in for a victim name on real listing pages.
        "confidential",
        "documentation",
        "documents",
        "personal",
        "internal",
        "customer",
        "employee",
        "database",
        "backup",
        "archive",
        "sample",
        "samples",
        "proof",
        "part",
    }
)


def _is_not_a_company_name(candidate: str) -> bool:
    """True when every word is a name fragment, so the whole thing names no one."""
    words = [word.strip(".,").lower() for word in candidate.split()]
    return bool(words) and all(word in _NAME_FRAGMENTS for word in words)


def _merge_split_listings(records: list[_Record]) -> list[_Record]:
    """Fold a record into the one before it when both are halves of one listing.

    Cards that print the name as a heading and again in a "Company: <name>" field (direwolf)
    produce two records for one victim: the heading keeps the date, the field keeps the
    domain. The halves are adjacent, share a name, and at most one of them has a domain.
    Two same-named records that *each* have a domain are left alone — that is a byline
    repeated on every card ("by Alexander Pushkin", arcus), and merging them would throw
    one victim's domain away.
    """
    merged: list[_Record] = []
    for record in records:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and record.victim_name
            and (previous.victim_name or "").casefold() == record.victim_name.casefold()
            and not (previous.victim_url and record.victim_url)
        ):
            if previous.victim_url is None:
                previous.victim_url, previous.url_pos = record.victim_url, record.url_pos
            previous.date_raw = previous.date_raw or record.date_raw
            previous.size_raw = previous.size_raw or record.size_raw
            previous.status_raws.extend(record.status_raws)
            previous.location_raws.extend(record.location_raws)
            previous.sector_raws.extend(record.sector_raws)
            previous.confidences.extend(record.confidences)
            continue
        merged.append(record)
    return merged


def _assign_urls_to_nearest_victim(
    spans: list[Span], victim_positions: list[tuple[int, int]]
) -> dict[int, int]:
    """Map each URL span's start offset to the index of the victim span nearest it.

    Distance is measured as the gap between spans, so "the link directly above this name"
    and "the link directly below this name" are both one character away and neither layout
    is privileged. A tie goes to the victim that comes *after* the URL: sites that print the
    link first are the reason this function exists, and no site prints a listing's link
    equidistant between two names by accident.
    """
    if not victim_positions:
        return {}

    owners: dict[int, int] = {}

    for span in spans:
        if span.label != Label.VICTIM_URL:
            continue

        best_index: int | None = None
        best_gap: int | None = None

        for index, (start, end) in enumerate(victim_positions):
            if end <= span.start:
                gap = span.start - end  # victim above the URL
            elif start >= span.end:
                gap = start - span.end  # victim below the URL
            else:
                gap = 0  # overlapping: the URL is inside the name
            # `<` rather than `<=` on a victim below, `<=` on one above, is what makes ties
            # fall to the later victim: positions are visited in document order.
            if best_gap is None or gap < best_gap or (gap == best_gap and start > span.end):
                best_index, best_gap = index, gap

        if best_index is not None:
            owners[span.start] = best_index

    return owners


def _to_leak(
    record: _Record,
    *,
    source_group: str,
    source_url: str | None,
    page_no: int,
    method: str,
    model_version: str | None,
    mode: str | None = None,
) -> ExtractedLeak:
    domain = extract_domain(record.victim_url) or extract_domain(record.victim_name)

    victim_name = record.victim_name
    if victim_name and domain and _is_not_a_company_name(victim_name):
        # The extractor sometimes captures a legal suffix or a section heading on its own —
        # "Ltd", "Financial", "Confidential Documentation" — leaving eight different
        # companies all displayed as "Financial". Suppress the label rather than the record:
        # the domain identifies the victim perfectly well, and dropping the span instead
        # would hand that domain to whichever victim happened to sit nearest, which is a
        # worse error than a missing name.
        victim_name = None

    confidence = sum(record.confidences) / len(record.confidences) if record.confidences else None

    # The victim's own name is the single most reliable sector evidence there is —
    # "Northwind Medical Group" and "Fairview Unified School District" say what they do —
    # so it is weighed alongside whatever the extractor labelled as a sector. The suppressed
    # name is used deliberately: a record left displaying only "Financial" is a section
    # heading the extractor mistook for a victim, and reading a sector off it would turn one
    # bad span into a second bad field.
    sector = resolve_sector([victim_name or "", *record.sector_raws])
    # Explicit text first, the domain's ccTLD as the fallback. Most listings name no country
    # at all, so the ccTLD is what actually fills this column.
    country = resolve_country(record.location_raws, domain=domain)

    status = LeakStatus(resolve_status(record.status_raws))
    size = parse_size(record.size_raw)

    return ExtractedLeak(
        victim_name=victim_name,
        victim_domain=domain,
        victim_country=country,
        victim_sector=sector,
        actor_group=source_group,
        source_url=source_url,
        source_page_no=page_no,
        published_at=parse_date(record.date_raw),
        published_at_raw=record.date_raw,
        status=status,
        leak_size_bytes=size,
        summary=record.summary,
        # `leak_type` is the model's default — every source is a ransomware leak site.
        incident_types=classify_incident(
            summary=record.summary,
            leak_type=ExtractedLeak.model_fields["leak_type"].default,
            status=status.value,
            leak_size_bytes=size,
        ),
        extraction=ExtractionMeta(
            method=method,  # type: ignore[arg-type]
            model_version=model_version,
            confidence=confidence,
            # Which path produced the row: "tile" (one block, one victim). Absent for the
            # whole-page linker, whose rows are stored exactly as they always were.
            **({"mode": mode} if mode else {}),
        ),
    )
