import { formatBytes, formatRelative } from "../../lib/format";
import { useCrawlStatus, useLeaks, type Leak } from "../../lib/queries";
import { LeakStatusChip } from "../../components/StatusChip";
import { TagChip } from "../../components/TagChip";

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
 */

const CARD_COUNT = 6;

export function LatestArrivals() {
  const query = useLeaks({
    page: 1,
    limit: CARD_COUNT,
    sort: "first_seen_at",
    order: "desc",
  });
  const status = useCrawlStatus();

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
    <section className="card arrivals-card">
      <div className="card-head">
        <div>
          <h2>Latest arrivals</h2>
          <p className="page-sub">
            The most recent listings by first sighting, newest first.
          </p>
        </div>
      </div>

      <div className="arrivals">
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
        {isNew && <span className="arrival-new">new</span>}
      </div>

      <div className="arrival-name" title={title}>
        {title}
      </div>
      <div className="arrival-group mono">{leak.actorGroup}</div>

      <div className="arrival-tags">
        <LeakStatusChip status={leak.status} />
        {leak.victimCountry && <TagChip kind="country" value={leak.victimCountry} />}
        {leak.victimSector && <TagChip kind="sector" value={leak.victimSector} />}
      </div>

      <div className="arrival-foot">
        <span>{formatRelative(leak.firstSeenAt)}</span>
        {leak.leakSizeBytes != null && <span>{formatBytes(leak.leakSizeBytes)}</span>}
      </div>
    </article>
  );
}
