"""Extraction: page text in, validated leaks out."""

from __future__ import annotations

from .base import DEFAULT_LABELS, Extractor
from .linker import Label, Span, link_spans
from .normalize import extract_domain, parse_date, parse_size, parse_status
from .rules import RulesExtractor

__all__ = [
    "DEFAULT_LABELS",
    "Extractor",
    "Label",
    "RulesExtractor",
    "Span",
    "extract_domain",
    "get_extractor",
    "link_spans",
    "parse_date",
    "parse_size",
    "parse_status",
]


def get_extractor(name: str = "rules", **kwargs: object) -> Extractor:
    """Build an extractor by name.

    `gliner` is imported here rather than at module top level so that torch is only loaded
    when it is actually asked for.
    """
    if name == "rules":
        return RulesExtractor()

    if name == "gliner":
        from .gliner_extractor import GlinerExtractor  # noqa: PLC0415 - lazy, see docstring

        return GlinerExtractor(**kwargs)  # type: ignore[arg-type]

    raise ValueError(f"Unknown extractor {name!r}. Available: rules, gliner")
