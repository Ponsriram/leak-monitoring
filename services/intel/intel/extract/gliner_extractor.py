"""Zero-shot NER via GLiNER.

Requires the `ml` extra:

    uv sync --extra ml

Why GLiNER rather than the old fine-tuned spaCy model: that model's training data
(`ransomwaredata.json`) was never committed, so it could not be retrained or reproduced, and
adding a new entity type meant relabelling a corpus that no longer exists. GLiNER is scored
against label strings supplied at call time — a new field costs one entry in a tuple.

The import is deliberately inside `__init__`, not at module scope, so that importing
`intel.extract` never drags in torch.
"""

from __future__ import annotations

from typing import Any

import structlog

from .base import DEFAULT_LABELS
from .linker import Span

log = structlog.get_logger(__name__)

# Model labels read better as natural language than as our snake_case field names, and the
# zero-shot scorer is sensitive to that phrasing.
_LABEL_PROMPTS: dict[str, str] = {
    "victim organization": "victim_org",
    "company website": "victim_url",
    "ransomware group": "ransomware_group",
    "publication date": "date",
    "data size": "leak_size",
    "leak status": "status",
}


class GlinerExtractor:
    """Wraps a GLiNER model behind the same interface as `RulesExtractor`."""

    name = "gliner"

    def __init__(
        self,
        model_name: str = "urchade/gliner_multi-v2.1",
        *,
        threshold: float = 0.45,
        labels: tuple[str, ...] = DEFAULT_LABELS,
    ) -> None:
        try:
            from gliner import GLiNER  # noqa: PLC0415 - lazy on purpose, see module docstring
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "GlinerExtractor needs the 'ml' extra. Install it with:\n"
                "    uv sync --extra ml\n"
                "Or use RulesExtractor, which needs no model."
            ) from exc

        self.model_name = model_name
        self.threshold = threshold
        self._prompts = [
            prompt for prompt, field in _LABEL_PROMPTS.items() if field in labels
        ]

        log.info("loading gliner model", model=model_name)
        self._model: Any = GLiNER.from_pretrained(model_name)

    @property
    def model_version(self) -> str:
        return self.model_name

    def extract(self, text: str) -> list[Span]:
        # Transformer context windows are small relative to a page; chunk on paragraph
        # boundaries so an entity is never split across a chunk edge.
        spans: list[Span] = []
        for offset, chunk in _chunk(text):
            try:
                entities = self._model.predict_entities(
                    chunk, self._prompts, threshold=self.threshold
                )
            except Exception:  # pragma: no cover - model runtime failure
                log.exception("gliner prediction failed", offset=offset)
                continue

            for entity in entities:
                field = _LABEL_PROMPTS.get(entity["label"])
                if field is None:
                    continue
                spans.append(
                    Span(
                        label=field,
                        text=entity["text"],
                        start=offset + entity["start"],
                        end=offset + entity["end"],
                        confidence=float(entity.get("score", 0.0)),
                    )
                )

        spans.sort(key=lambda span: (span.start, span.end))
        return spans


def _chunk(text: str, max_chars: int = 1500) -> list[tuple[int, str]]:
    """Split on blank lines, keeping each chunk under `max_chars`, tracking offsets."""
    chunks: list[tuple[int, str]] = []
    position = 0
    buffer: list[str] = []
    buffer_start = 0
    buffer_len = 0

    for paragraph in text.split("\n\n"):
        piece = paragraph + "\n\n"
        if buffer and buffer_len + len(piece) > max_chars:
            chunks.append((buffer_start, "".join(buffer)))
            buffer, buffer_len, buffer_start = [], 0, position
        if not buffer:
            buffer_start = position
        buffer.append(piece)
        buffer_len += len(piece)
        position += len(piece)

    if buffer:
        chunks.append((buffer_start, "".join(buffer)))

    return chunks
