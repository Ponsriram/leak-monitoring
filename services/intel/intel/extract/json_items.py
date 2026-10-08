"""Leaks straight from the JSON a JavaScript leak site loads, per a mapping in sources.yaml.

A site that renders its listing from an XHR/fetch response already holds every victim as a
structured record. When a source declares where those records are (`json_items`), the
browser collector's captured responses are read with that mapping instead of parsing the
rendered text. Nothing here guesses: no endpoint and no field name is assumed for any site,
and a source without a mapping, or a page whose responses do not match it, is extracted from
its text exactly as before.

    json_items:
      match: "/api/disclosures"     # optional: a substring of the response URL
      path: "data.items"            # dotted path to the array; omit if the body IS the array
      name: "title"                 # dotted paths inside one item; name or domain is required
      domain: "website"
      country: "country"
      revenue: "revenue"
      description: "description"
      date: "createdAt"
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ..models import ExtractedLeak
from .gazetteer import CCTLD_COUNTRY
from .linker import leak_from_fields

log = structlog.get_logger(__name__)

_FIELDS = ("name", "domain", "country", "revenue", "description", "date")

# Above this a number is an epoch in milliseconds, not seconds (year 2286 in seconds).
_EPOCH_MS_FROM = 10_000_000_000


@dataclass(slots=True, frozen=True)
class JsonMapping:
    """Where a source's victim records are in the JSON its pages load."""

    path: str
    fields: dict[str, str]
    match: str | None = None


def parse_mapping(raw: object) -> JsonMapping | None:
    """A `json_items` value (a dict, or the jsonb text a database row returns) -> mapping.

    None for no mapping, and for one too incomplete to use — logged, since a mapping that
    silently does nothing would look exactly like a site with nothing new.
    """
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            log.warning("json_items is not valid JSON, ignored", value=raw[:200])
            return None
    if not isinstance(raw, dict):
        log.warning("json_items must be a mapping, ignored", value=str(raw)[:200])
        return None
    fields = {key: str(raw[key]) for key in _FIELDS if raw.get(key)}
    if "name" not in fields and "domain" not in fields:
        log.warning("json_items names neither a name nor a domain field, ignored")
        return None
    match = raw.get("match")
    return JsonMapping(
        path=str(raw.get("path") or ""), fields=fields, match=str(match) if match else None
    )


def match_items(responses: list[Any] | None, mapping: JsonMapping | None) -> list[dict] | None:
    """The records from the first captured response the mapping fits, or None.

    A response fits when its URL contains `match` (if given) and `path` leads to a list in
    its body. Records that are not objects are skipped.
    """
    if mapping is None or not responses:
        return None
    for response in responses:
        if mapping.match and mapping.match not in response.url:
            continue
        try:
            body = json.loads(response.body)
        except ValueError:
            continue
        items = _get(body, mapping.path)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return None


def items_text(items: list[dict], mapping: JsonMapping) -> str:
    """The mapped fields of every record, one line each, for the page's stored text.

    Appended to the text that is hashed and stored, so a change that shows only in the JSON
    (a description the page reveals on click) still reads as a changed page. Only mapped
    fields: the rest of the JSON (view counters, server clocks) would change every fetch.
    """
    lines = []
    for item in items:
        values = [
            _text(_get(item, mapping.fields[key])) for key in _FIELDS if key in mapping.fields
        ]
        lines.append(" | ".join(value or "-" for value in values))
    return "\n".join(lines)


def leaks_from_items(
    items: list[dict],
    mapping: JsonMapping,
    *,
    source_group: str,
    source_url: str | None,
    page_no: int,
) -> list[ExtractedLeak]:
    """One leak per record that names a victim, normalized like every other leak."""
    leaks: list[ExtractedLeak] = []
    for item in items:
        value = {key: _text(_get(item, path)) for key, path in mapping.fields.items()}
        summary_parts = []
        if value.get("revenue"):
            summary_parts.append(f"Revenue: {value['revenue']}")
        if value.get("description"):
            summary_parts.append(value["description"])
        leak = leak_from_fields(
            victim_name=value.get("name"),
            victim_url=value.get("domain"),
            date_raw=_date_text(_get(item, mapping.fields["date"])) if "date" in value else None,
            location_raws=[_country(value["country"])] if value.get("country") else [],
            summary=" ".join(" ".join(summary_parts).split()) or None,
            source_group=source_group,
            source_url=source_url,
            page_no=page_no,
            mode="json",
        )
        if leak is not None:
            leaks.append(leak)
    return leaks


def _get(value: Any, path: str) -> Any:
    for part in (p for p in path.split(".") if p):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _date_text(value: Any) -> str | None:
    """A date field as text `parse_date` reads; numbers are taken as a Unix epoch."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value >= _EPOCH_MS_FROM else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    return _text(value)


def _country(value: str) -> str:
    """A two-letter code becomes its country's name; anything else is left to the gazetteer."""
    return CCTLD_COUNTRY.get(value.strip().lower(), value) if len(value.strip()) == 2 else value
