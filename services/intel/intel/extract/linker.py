"""Turn a flat list of entity spans into discrete leak records.

This is the module that replaces five divergent copies of the same loop (`demo.py`,
`demo1.py`, `DarkNer.ipynb`, `DarkNer-test.ipynb`, `Mapping.ipynb`). It is the most
important piece of business logic in the project and previously had no canonical home and
no test.

Two changes make it dramatically simpler than the original:

1. **Extraction is per page, not per corpus.** The old code ran one NER pass over a 1.2 MB
   blob of every site concatenated together, then tried to reassociate entities by their
   order in that blob. All the `orphan_entries` bookkeeping existed to cope with the
   ambiguity that created. Per page, a victim span and the date next to it are
   unambiguously related.

2. **The actor group comes from the source, not from the text.** We know which site we
   crawled — guessing the group from prose was always redundant. A group span in the text
   can still override it (some sites republish other crews' listings), but the default is
   simply known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ExtractedLeak, ExtractionMeta, LeakStatus
from .normalize import extract_domain, parse_date, parse_size, parse_status


class Label:
    """Entity labels. Whatever extractor is in use must emit these strings."""

    VICTIM = "victim_org"
    VICTIM_URL = "victim_url"
    GROUP = "ransomware_group"
    DATE = "date"
    SIZE = "leak_size"
    STATUS = "status"


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
    status_raw: str | None = None
    group_override: str | None = None
    confidences: list[float] = field(default_factory=list)


def link_spans(
    spans: list[Span],
    *,
    source_group: str,
    source_url: str | None = None,
    page_no: int = 1,
    method: str = "rules",
    model_version: str | None = None,
) -> list[ExtractedLeak]:
    """Group spans into leaks.

    A victim span opens a new record; attribute spans that follow attach to it. Attributes
    seen before any victim are held and applied to the first record, which covers sites that
    print the date above the company name.
    """
    records: list[_Record] = []
    pending = _Record()  # attributes seen before the first victim span
    current: _Record | None = None
    # A group span applies to every record after it on the page, including ones already
    # opened but not yet closed — leak sites print the crew name once, in the header.
    page_group: str | None = None

    def attach(record: _Record, span: Span) -> None:
        if span.confidence is not None:
            record.confidences.append(span.confidence)

        match span.label:
            case Label.VICTIM_URL:
                # Don't let a second URL clobber the first — the first is the victim's own
                # site; later ones are usually mirrors or the leak download link.
                record.victim_url = record.victim_url or span.text
            case Label.DATE:
                record.date_raw = record.date_raw or span.text
            case Label.SIZE:
                record.size_raw = record.size_raw or span.text
            case Label.STATUS:
                record.status_raw = record.status_raw or span.text

    for span in spans:
        text = span.text.strip()
        if not text:
            continue

        if span.label == Label.GROUP:
            page_group = text
            continue

        if span.label == Label.VICTIM:
            current = _Record(victim_name=text, group_override=page_group)
            # Fold in anything that appeared before the first victim on this page.
            if not records and pending is not None:
                current.victim_url = pending.victim_url
                current.date_raw = pending.date_raw
                current.size_raw = pending.size_raw
                current.status_raw = pending.status_raw
                current.confidences.extend(pending.confidences)
                pending = _Record()
            records.append(current)
            continue

        attach(current if current is not None else pending, span)

    # A page can carry a bare URL with no company name — still a real listing.
    if not records and (pending.victim_url or pending.date_raw):
        pending.group_override = page_group
        records.append(pending)

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


def _to_leak(
    record: _Record,
    *,
    source_group: str,
    source_url: str | None,
    page_no: int,
    method: str,
    model_version: str | None,
) -> ExtractedLeak:
    domain = extract_domain(record.victim_url) or extract_domain(record.victim_name)

    confidence = (
        sum(record.confidences) / len(record.confidences) if record.confidences else None
    )

    return ExtractedLeak(
        victim_name=record.victim_name,
        victim_domain=domain,
        actor_group=source_group,
        source_url=source_url,
        source_page_no=page_no,
        published_at=parse_date(record.date_raw),
        published_at_raw=record.date_raw,
        status=LeakStatus(parse_status(record.status_raw)),
        leak_size_bytes=parse_size(record.size_raw),
        extraction=ExtractionMeta(
            method=method,  # type: ignore[arg-type]
            model_version=model_version,
            confidence=confidence,
        ),
    )
