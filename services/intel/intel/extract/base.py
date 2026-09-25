"""The extractor interface.

Every extractor emits the same `Span` list, so the linker and everything downstream never
depend on how the spans were found. `RulesExtractor` is the one implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .linker import Span


@runtime_checkable
class Extractor(Protocol):
    """Anything that turns page text into labelled spans."""

    name: str

    def extract(self, text: str) -> list[Span]:
        """Return spans found in `text`. Must not raise on messy input."""
        ...
