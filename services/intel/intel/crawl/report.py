"""Judging a finished cycle: per-source verdicts, the overall status, and the report.

Pure functions over plain rows, with no I/O, so every status rule can be tested exhaustively.

The rules, and why they are what they are:

**A source run fails exactly when its page 1 fails.** That is what the legacy crawler meant by
a failed source (unreachable, gated, empty), and it is what the Sources page's health
(`consecutive_failures`) has always meant. A detail page that 404s, or a deep listing page
that times out, is not a failing source; counting it would make a healthy site read as
degraded because one victim page was removed.

**A cycle fails only when nothing worked.** If every attempted source failed, or URLs were
attempted and none succeeded, the cycle is `failed`. Anything less — a source down while
others succeed, some URLs failed, a listing that ended early — is `completed`, with the
failures named in the report rather than hidden in the status.

**An empty cycle is `completed`.** Having nothing due is not a failure.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

Row = Mapping[str, Any]

_MAX_ERRORS = 5
_ERROR_CHARS = 100
_DIGITS = re.compile(r"\d{4,}")


@dataclass(slots=True)
class SourceVerdict:
    source_id: int
    slug: str
    status: str  # 'succeeded' | 'failed' — the existing crawl_runs statuses
    error: str | None
    # Whether this run should move `sources.consecutive_failures` / `last_success_at`. A run
    # that never reached page 1 (only detail pages were recrawled) says nothing about health.
    update_health: bool
    deep: bool
    urls: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    links: int = 0
    pages_fetched: int = 0
    pages_changed: int = 0
    leaks_found: int = 0
    leaks_updated: int = 0


def judge_source(source_id: int, slug: str, rows: Sequence[Row]) -> SourceVerdict:
    """The outcome of one source in one cycle, from its URLs."""
    listing = [r for r in rows if r["kind"] == "listing"]
    page_one = next((r for r in listing if (r["page_no"] or 1) == 1), None)

    if page_one is None:
        status, error, health = "succeeded", None, False
    elif page_one["status"] == "failed":
        status, health = "failed", True
        error = page_one["last_error"] or "page 1 failed"
    elif page_one["status"] == "skipped":
        # Skipped before it ran: the source was disabled mid-cycle. Not the site's fault.
        status, error, health = "failed", "skipped: source disabled during the cycle", False
    else:
        status, error, health = "succeeded", None, True

    def count(**where: str) -> int:
        return sum(all(r[k] == v for k, v in where.items()) for r in rows)

    return SourceVerdict(
        source_id=source_id,
        slug=slug,
        status=status,
        error=error,
        update_health=health,
        deep=any((r["page_no"] or 1) > 1 or r["kind"] == "link" for r in rows),
        urls=len(rows),
        succeeded=count(status="succeeded"),
        failed=count(status="failed"),
        skipped=count(status="skipped"),
        new=count(result="new"),
        changed=count(result="changed"),
        unchanged=count(result="unchanged"),
        links=count(kind="link"),
        pages_fetched=count(status="succeeded"),
        pages_changed=count(result="new") + count(result="changed"),
        leaks_found=sum(r["leaks_found"] for r in rows),
        leaks_updated=sum(r["leaks_updated"] for r in rows),
    )


@dataclass(slots=True)
class CycleSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    empty: int = 0
    retried_attempts: int = 0
    links_discovered: int = 0
    links_recrawled: int = 0
    leaks_found: int = 0
    leaks_updated: int = 0
    http: dict[str, int] = field(default_factory=dict)
    errors: list[tuple[str, int]] = field(default_factory=list)
    sources: list[SourceVerdict] = field(default_factory=list)

    @property
    def failed_sources(self) -> list[str]:
        return [v.slug for v in self.sources if v.status == "failed"]

    @property
    def status(self) -> str:
        """`failed` only when nothing worked; see the module docstring."""
        if self.sources and len(self.failed_sources) == len(self.sources):
            return "failed"
        if self.total > 0 and self.succeeded == 0 and self.failed > 0:
            return "failed"
        return "completed"

    @property
    def error(self) -> str | None:
        failed = self.failed_sources
        if not failed:
            return None
        shown = ", ".join(failed[:5]) + (f" +{len(failed) - 5} more" if len(failed) > 5 else "")
        return f"{len(failed)} of {len(self.sources)} sources failed: {shown}"

    def to_json(self) -> str:
        return json.dumps(
            {
                "sources": {
                    "attempted": len(self.sources),
                    "failed": self.failed_sources,
                },
                "urls": {
                    "total": self.total,
                    "succeeded": self.succeeded,
                    "failed": self.failed,
                    "skipped": self.skipped,
                    "new": self.new,
                    "changed": self.changed,
                    "unchanged": self.unchanged,
                    "empty": self.empty,
                    "retried_attempts": self.retried_attempts,
                },
                "links": {"discovered": self.links_discovered, "recrawled": self.links_recrawled},
                "leaks": {"new": self.leaks_found, "seen_again": self.leaks_updated},
                "http": self.http,
                "errors": [{"error": e, "count": n} for e, n in self.errors],
                "per_source": {
                    v.slug: {
                        "status": v.status,
                        "urls": v.urls,
                        "succeeded": v.succeeded,
                        "failed": v.failed,
                        "skipped": v.skipped,
                        "changed": v.new + v.changed,
                        "links": v.links,
                        **({"error": v.error} if v.error else {}),
                    }
                    for v in self.sources
                },
            }
        )


def _normalise_error(error: str) -> str:
    """Group errors that differ only by a long number (a byte count, a port)."""
    return _DIGITS.sub("N", error.strip())[:_ERROR_CHARS]


def build_summary(
    rows: Sequence[Row],
    verdicts: Sequence[SourceVerdict],
    *,
    cycle_started_at: datetime,
) -> CycleSummary:
    """Everything the report says, from the cycle's URLs and the per-source verdicts."""
    s = CycleSummary(sources=list(verdicts), total=len(rows))
    http: Counter[str] = Counter()
    errors: Counter[str] = Counter()

    for r in rows:
        status = r["status"]
        if status == "succeeded":
            s.succeeded += 1
        elif status == "failed":
            s.failed += 1
        elif status == "skipped":
            s.skipped += 1

        result = r["result"]
        if status == "succeeded":
            if result == "new":
                s.new += 1
            elif result == "changed":
                s.changed += 1
            elif result == "unchanged":
                s.unchanged += 1
            elif result == "empty":
                s.empty += 1

        s.retried_attempts += max((r["attempt"] or 0) - 1, 0)
        s.leaks_found += r["leaks_found"]
        s.leaks_updated += r["leaks_updated"]

        if r["kind"] == "link":
            if r["discovered_at"] >= cycle_started_at:
                s.links_discovered += 1
            else:
                s.links_recrawled += 1

        if status in ("succeeded", "failed"):
            http[str(r["http_status"]) if r["http_status"] is not None else "none"] += 1
        if status == "failed" and r["last_error"]:
            errors[_normalise_error(r["last_error"])] += 1

    s.http = dict(http.most_common())
    s.errors = errors.most_common(_MAX_ERRORS)
    return s


def format_report(
    cycle_id: int, trigger: str, summary: CycleSummary, *, status: str, duration_ms: int
) -> str:
    """The end-of-cycle block for the log. Counts and error classes only — never page content."""
    failed = summary.failed_sources
    sources = f"{len(summary.sources)} attempted"
    if failed:
        sources += f", {len(failed)} failed ({', '.join(failed[:5])})"
    http = ", ".join(f"{k}x{v}" for k, v in list(summary.http.items())[:8]) or "-"
    lines = [
        f"Crawl Cycle {cycle_id} ({trigger}) - {status}",
        "-----------------------",
        f"Sources:       {sources}",
        f"Total URLs:    {summary.total:>6}",
        f"Successful:    {summary.succeeded:>6}   (new {summary.new}, changed {summary.changed},"
        f" unchanged {summary.unchanged}, empty {summary.empty})",
        f"Failed:        {summary.failed:>6}",
        f"Skipped:       {summary.skipped:>6}",
        f"Retried:       {summary.retried_attempts:>6}   (extra attempts)",
        f"Links:         {summary.links_discovered} discovered,"
        f" {summary.links_recrawled} recrawled",
        f"Leaks found:   {summary.leaks_found} new, {summary.leaks_updated} seen again",
        f"HTTP:          {http}",
    ]
    for error, n in summary.errors:
        lines.append(f"Error x{n}:     {error}")
    lines.append(f"Duration:      {duration_ms / 1000:.1f} sec")
    return "\n".join(lines)


@dataclass(slots=True)
class CycleReport:
    """A finished cycle, as returned by `CrawlQueue.finalize_cycle`."""

    cycle_id: int
    trigger: str
    status: str  # completed | failed | abandoned
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    summary: CycleSummary
    # True when an empty scheduled cycle was removed rather than recorded.
    discarded: bool = False

    def text(self) -> str:
        return format_report(
            self.cycle_id,
            self.trigger,
            self.summary,
            status=self.status,
            duration_ms=self.duration_ms,
        )
