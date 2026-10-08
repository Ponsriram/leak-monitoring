"""Extraction: page text in, validated leaks out."""

from __future__ import annotations

from .base import Extractor
from .gazetteer import country_from_domain, parse_country, parse_sector
from .linker import Label, Span, link_block, link_spans
from .normalize import extract_domain, parse_date, parse_size, parse_status, resolve_status
from .rules import RulesExtractor

__all__ = [
    "Extractor",
    "Label",
    "RulesExtractor",
    "Span",
    "country_from_domain",
    "extract_domain",
    "get_extractor",
    "link_block",
    "link_spans",
    "parse_country",
    "parse_date",
    "parse_sector",
    "parse_size",
    "parse_status",
    "resolve_status",
]


def get_extractor(name: str = "rules") -> Extractor:
    """Build an extractor by name."""
    if name == "rules":
        return RulesExtractor()

    raise ValueError(f"Unknown extractor {name!r}. Available: rules")
