import { useState } from "react";
import { ChevronDown } from "../../components/icons";
import { formatBytes, formatRelative } from "../../lib/format";
import { useCrawlStatus, useLeaks, type Leak } from "../../lib/queries";
import { LeakStatusChip } from "../../components/StatusChip";

/**
 * The newest listings, by when we first saw them.
 *
 * Deliberately ordered on `first_seen_at` and nothing else, and deliberately not affected by
 * the filters below it. `published_at` is whatever date the site printed — sites backdate,
 * omit it, and edit it — so a strip ordered on it can show a "new" listing that arrived
 * weeks ago. `first_seen_at` is written once, by us, on the insert that created the row: it
 * is the only column that answers "what turned up since I last looked", which is the entire
 * premise of a monitoring console.
 *
 * A card is marked new when it arrived during the most recent completed sync — derived from
 * that sync's own start time rather than a wall-clock window, so "new" means "this sync
 * found it" and not "less than a day old".
 *
 * One row of compact cards that scrolls sideways, never a wrapping grid: this sits above the
 * leaks table on a page whose table takes the remaining height, and a strip that wraps to a
 * second row takes that height straight out of the table. It also collapses to its header,
 * and remembers that, for anyone who would rather give the table everything.
 */

const CARD_COUNT = 8;
const COLLAPSED_KEY = "lm.arrivals.collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    // Site data blocked — the strip simply starts open.
    return false;
  }
}

export function LatestArrivals() {
  const query = useLeaks({
    page: 1,
    limit: CARD_COUNT,
    sort: "first_seen_at",
    order: "desc",
  });
  const status = useCrawlStatus();
  const [collapsed, setCollapsed] = useState(readCollapsed);

  function toggle() {
    setCollapsed((current) => {
      const next = !current;
      try {
        localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // A preference that cannot be persisted still applies for this visit.
      }
      return next;
    });
  }

  const rows = query.data?.data ?? [];
  if (query.isPending || rows.length === 0) return null;

  const latest = status.data?.latest;
  // Only a settled sync defines a boundary. While one is running, its `started_at` would
  // mark rows new the moment they land, and every card would flip to "new" mid-crawl.
  const arrivedAfter =
    latest?.status === "succeeded" && latest.startedAt
      ? new Date(latest.startedAt).getTime()
      : null;

  return (
    <section className={`card arrivals-card${collapsed ? " is-collapsed" : ""}`}>
      <div className="arrivals-head">
        <h2>Latest arrivals</h2>
        <span className="arrivals-sub">Newest listings by first sighting</span>
        <button
          type="button"
          className="btn btn-sm arrivals-toggle"
          onClick={toggle}
          aria-expanded={!collapsed}
          aria-controls="latest-arrivals"
        >
          <ChevronDown size={14} className={collapsed ? "nav-chevron closed" : "nav-chevron"} />
          {collapsed ? "Show" : "Hide"}
        </button>
      </div>

      <div className="arrivals" id="latest-arrivals" hidden={collapsed}>
        {rows.map((leak) => (
          <ArrivalCard
            key={leak.id}
            leak={leak}
            isNew={
              arrivedAfter !== null && new Date(leak.firstSeenAt).getTime() >= arrivedAfter
            }
          />
        ))}
      </div>
    </section>
  );
}

function ArrivalCard({ leak, isNew }: { leak: Leak; isNew: boolean }) {
  const title = leak.victimName ?? leak.victimDomain ?? "Unnamed listing";

  return (
    <article className={`arrival${isNew ? " is-new" : ""}`}>
      <div className="arrival-top">
        {/*
          A monogram, not an image. The only picture of a victim available here would have
          to be fetched from the victim's own site, which would announce to that site — and
          to anything watching it — exactly which companies this console is monitoring.
        */}
        <span className="arrival-mark" aria-hidden="true">
          {title.slice(0, 2).toUpperCase()}
        </span>
        <div className="arrival-ident">
          <div className="arrival-name" title={title}>
            {title}
          </div>
          <div className="arrival-group mono">{leak.actorGroup}</div>
        </div>
        {isNew && <span className="arrival-new">new</span>}
      </div>

      <div className="arrival-foot">
        {/* Status only: a compact card has room for one chip, and a clipped "Ger" is worse
            than none — country and sector are in the table directly below. */}
        <div className="arrival-tags">
          <LeakStatusChip status={leak.status} />
        </div>
        <span className="arrival-when">
          {formatRelative(leak.firstSeenAt)}
          {leak.leakSizeBytes != null && ` · ${formatBytes(leak.leakSizeBytes)}`}
        </span>
      </div>
    </article>
  );
}
