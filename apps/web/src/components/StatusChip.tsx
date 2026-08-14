import type { SourceRow } from "../lib/queries";

/**
 * Status is never carried by color alone.
 *
 * Every chip renders its text label next to the dot — required here because two of the
 * status colors sit below 3:1 contrast on a light surface by design, and necessary anyway
 * for colorblind readers.
 */

const HEALTH_TONE: Record<SourceRow["health"], string> = {
  healthy: "good",
  degraded: "warning",
  failing: "critical",
  disabled: "neutral",
};

export function HealthChip({ health }: { health: SourceRow["health"] }) {
  return (
    <span className={`chip ${HEALTH_TONE[health]}`}>
      <span className="chip-dot" style={{ background: "currentColor" }} />
      {health}
    </span>
  );
}

const LEAK_TONE: Record<string, string> = {
  published: "critical",
  countdown: "warning",
  sold: "serious",
  removed: "neutral",
  unknown: "neutral",
};

export function LeakStatusChip({ status }: { status: string }) {
  return (
    <span className={`chip ${LEAK_TONE[status] ?? "neutral"}`}>
      <span className="chip-dot" style={{ background: "currentColor" }} />
      {status}
    </span>
  );
}
