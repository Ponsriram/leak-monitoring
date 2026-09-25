import { HealthChip, LeakStatusChip } from "../../components/StatusChip";
import { EmptyState, ErrorState } from "../../components/states";
import { flagFor } from "../../lib/country-coords";
import { formatNumber, formatRelative } from "../../lib/format";
import { useLeaks, useLeaksPerTag, useSources, type TagKind } from "../../lib/queries";

/**
 * The dashboard panels that don't need Recharts.
 *
 * Ranked facets are drawn as HTML bars rather than an SVG chart: the labels are long
 * (sector names, country names with a flag), HTML wraps and truncates them properly, and
 * it keeps these panels out of the lazily-loaded chart bundle so they paint with the tiles.
 * Like the group chart, every bar is one hue — the length already encodes the rank.
 */

const Skeleton = () => <div className="skeleton rank-skeleton" />;

type RankRow = { label: string; total: number; prefix?: string };

function RankedBars({ rows, suffix }: { rows: RankRow[]; suffix: string }) {
  const max = Math.max(1, ...rows.map((row) => row.total));
  const sum = rows.reduce((acc, row) => acc + row.total, 0);
  return (
    <ol className="rank-list">
      {rows.map((row) => (
        <li
          key={row.label}
          className="rank-row"
          title={`${row.label}: ${formatNumber(row.total)} ${suffix}`}
        >
          <span className="rank-label">
            {row.prefix && <span className="rank-prefix">{row.prefix}</span>}
            {row.label}
          </span>
          <span className="rank-value">
            {formatNumber(row.total)}
            <span className="rank-share">
              {sum ? `${Math.round((row.total / sum) * 100)}%` : ""}
            </span>
          </span>
          <span className="rank-track" aria-hidden="true">
            <span className="rank-bar" style={{ width: `${(row.total / max) * 100}%` }} />
          </span>
        </li>
      ))}
    </ol>
  );
}

const TAG_LIMIT = 8;

/**
 * Top-N of one extracted tag. Share is of the rows shown, and says so in the card head,
 * because tag columns are null for many leaks and a share of "all leaks" would mislead.
 */
export function TagPanel({ tag, title }: { tag: TagKind; title: string }) {
  const query = useLeaksPerTag(tag);
  const rows = (query.data?.data ?? []).slice(0, TAG_LIMIT).map((row) => ({
    label: row.value,
    total: row.total,
    prefix: tag === "country" ? flagFor(row.value) || undefined : undefined,
  }));

  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        <span className="card-note">Top {TAG_LIMIT} · share of shown</span>
      </div>
      <div className="card-body">
        {query.isPending ? (
          <Skeleton />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={query.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing tagged yet" />
        ) : (
          <RankedBars rows={rows} suffix="leaks" />
        )}
      </div>
    </section>
  );
}

const RECENT_COUNT = 8;

/** Newest listings by first sighting — the same ordering the Leaks page's arrivals strip uses. */
export function RecentLeaksPanel() {
  const query = useLeaks({
    page: 1,
    limit: RECENT_COUNT,
    sort: "first_seen_at",
    order: "desc",
  });
  const rows = query.data?.data ?? [];

  return (
    <section className="card">
      <div className="card-head">
        <h2>Most recent leaks</h2>
        <span className="card-note">By first sighting</span>
      </div>
      <div className="card-body no-pad">
        {query.isPending ? (
          <div className="card-body">
            <Skeleton />
          </div>
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={query.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState title="No leaks recorded yet" />
        ) : (
          <ul className="feed-list">
            {rows.map((leak) => (
              <li key={leak.id} className="feed-row">
                <div className="feed-main">
                  <span className="feed-title">
                    {leak.victimName ?? leak.victimDomain ?? "Unnamed victim"}
                  </span>
                  <span className="feed-meta">
                    {leak.actorGroup}
                    {leak.victimCountry &&
                      ` · ${flagFor(leak.victimCountry)} ${leak.victimCountry}`}
                    {leak.victimSector && ` · ${leak.victimSector}`}
                  </span>
                </div>
                <div className="feed-side">
                  <LeakStatusChip status={leak.status} />
                  <span className="feed-time">{formatRelative(leak.firstSeenAt)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

const HEALTH_ORDER = { failing: 0, degraded: 1, healthy: 2, disabled: 3 } as const;

/** Every source's health, worst first, with how much each one has contributed. */
export function SourceHealthPanel() {
  const query = useSources();
  const rows = [...(query.data?.data ?? [])].sort(
    (a, b) =>
      HEALTH_ORDER[a.health] - HEALTH_ORDER[b.health] || b.leakCount - a.leakCount,
  );
  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    acc[row.health] = (acc[row.health] ?? 0) + 1;
    return acc;
  }, {});
  const max = Math.max(1, ...rows.map((row) => row.leakCount));

  return (
    <section className="card">
      <div className="card-head">
        <h2>Source health</h2>
        <span className="card-note">
          {counts.healthy ?? 0} healthy · {counts.degraded ?? 0} degraded ·{" "}
          {counts.failing ?? 0} failing
        </span>
      </div>
      <div className="card-body no-pad">
        {query.isPending ? (
          <div className="card-body">
            <Skeleton />
          </div>
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={query.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState title="No sources configured" />
        ) : (
          <ul className="feed-list feed-scroll">
            {rows.map((source) => (
              <li key={source.id} className="feed-row">
                <div className="feed-main">
                  <span className="feed-title">{source.name}</span>
                  <span className="feed-meta">
                    Last success {formatRelative(source.lastSuccessAt)}
                    {source.consecutiveFailures > 0 &&
                      ` · ${source.consecutiveFailures} failed run(s)`}
                  </span>
                  <span className="rank-track" aria-hidden="true">
                    <span
                      className="rank-bar"
                      style={{ width: `${(source.leakCount / max) * 100}%` }}
                    />
                  </span>
                </div>
                <div className="feed-side">
                  <HealthChip health={source.health} />
                  <span className="feed-time">{formatNumber(source.leakCount)} leaks</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
