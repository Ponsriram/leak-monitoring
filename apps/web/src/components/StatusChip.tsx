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
  negotiating: "warning",
  sold: "serious",
  removed: "neutral",
  unknown: "neutral",
};

/**
 * What each status actually means, on hover.
 *
 * Worth spelling out because two of them read as the opposite of what they are: `unknown`
 * is "the site printed no status", not "we lost track of it", and none of these describe
 * whether the listing is still up — that is what the Last seen column is for.
 */
const LEAK_STATUS_HELP: Record<string, string> = {
  published: "The site says the data has been published or released.",
  countdown: "A deadline is running before publication.",
  negotiating: "The listing mentions negotiation or payment. Still up.",
  sold: "The site says the data was sold to a buyer.",
  removed: "The site says the listing was removed or deleted.",
  unknown: "The listing stated no status. Most sites never do.",
};

export function LeakStatusChip({ status }: { status: string }) {
  return (
    <span className={`chip ${LEAK_TONE[status] ?? "neutral"}`} title={LEAK_STATUS_HELP[status]}>
      <span className="chip-dot" style={{ background: "currentColor" }} />
      {status}
    </span>
  );
}
