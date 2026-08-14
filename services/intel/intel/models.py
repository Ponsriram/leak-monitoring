"""The one schema everything in the pipeline validates against.

The old code passed untyped dicts between five divergent copies of the mapping loop, which
is how the field names drifted apart from what the API and database expected. Every
extractor here returns `ExtractedLeak`, and nothing reaches the database without passing
through it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LeakStatus(StrEnum):
    """Mirrors the `leak_status` enum in the database. Keep the two in step."""

    PUBLISHED = "published"
    COUNTDOWN = "countdown"
    SOLD = "sold"
    REMOVED = "removed"
    UNKNOWN = "unknown"


class ExtractionMethod(StrEnum):
    RULES = "rules"
    GLINER = "gliner"
    LLM = "llm"
    MANUAL = "manual"
    MIGRATED = "migrated"


class ExtractionMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    method: ExtractionMethod
    model_version: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ExtractedLeak(BaseModel):
    """A single victim listing, normalized and ready to upsert."""

    model_config = ConfigDict(str_strip_whitespace=True)

    victim_name: str | None = None
    victim_domain: str | None = None
    victim_country: str | None = None
    victim_sector: str | None = None

    actor_group: str

    source_url: str | None = None
    source_page_no: int = 1

    published_at: datetime | None = None
    published_at_raw: str | None = None

    status: LeakStatus = LeakStatus.UNKNOWN
    leak_type: str = "ransomware"
    leak_size_bytes: int | None = Field(default=None, ge=0)

    extraction: ExtractionMeta

    @field_validator("actor_group")
    @classmethod
    def _slugify_group(cls, value: str) -> str:
        """Groups are stored as a normalized slug.

        The same actor appears as "LockBit", "lockbit3.0" and "LOCKBIT" across sites; without
        normalizing, the dashboard's group filter shows the same actor three times.
        """
        return value.strip().lower().replace(" ", "-")

    @field_validator("victim_domain")
    @classmethod
    def _clean_domain(cls, value: str | None) -> str | None:
        if not value:
            return None
        domain = value.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if domain.startswith(prefix):
                domain = domain[len(prefix) :]
        return domain.split("/")[0].rstrip(".") or None

    @property
    def dedupe_hash(self) -> str:
        """Stable identity for this listing.

        Deliberately excludes anything wall-clock derived. An early version of the seed
        script folded a `now()`-based timestamp into its hash and so re-inserted the whole
        dataset on every run — the exact bug this key exists to prevent.

        Identity is (group, victim). `published_at` is supporting evidence: the same victim
        listed by the same group is the same leak even if the site later edits the date.
        """
        victim = (self.victim_domain or self.victim_name or "").strip().lower()
        return hashlib.sha256(f"{self.actor_group}|{victim}".encode()).hexdigest()

    @property
    def is_usable(self) -> bool:
        """A leak with no victim identity at all cannot be deduplicated or shown."""
        return bool(self.victim_name or self.victim_domain)
