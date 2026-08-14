"""The extractor interface.

Extraction is pluggable on purpose. The zero-shot NER path (GLiNER) pulls torch and
transformers — roughly 2 GB — and hard-wiring that into the base install would mean the
pipeline could not run, and its tests could not execute, without an ML stack present.

So: `RulesExtractor` is the default and needs nothing. `GlinerExtractor` lives behind the
`ml` extra and is imported lazily. Both emit the same `Span` list, so the linker and
everything downstream is identical either way.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .linker import Span

# The labels a zero-shot model is asked to find. Adding a label here is the entire cost of
# extracting a new field — no retraining, which is the reason for choosing zero-shot NER
# over the old fine-tuned spaCy model whose training data was never committed.
DEFAULT_LABELS: tuple[str, ...] = (
    "victim_org",
    "victim_url",
    "ransomware_group",
    "date",
    "leak_size",
    "status",
)


@runtime_checkable
class Extractor(Protocol):
    """Anything that turns page text into labelled spans."""

    name: str

    def extract(self, text: str) -> list[Span]:
        """Return spans found in `text`. Must not raise on messy input."""
        ...
