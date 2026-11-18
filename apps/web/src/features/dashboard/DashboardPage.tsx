import { lazy, Suspense, useState } from "react";
import { StatTile } from "../../components/StatTile";
import { EmptyState, ErrorState } from "../../components/states";
import { formatRelative } from "../../lib/format";
import { useLeaksPerDay, useLeaksPerGroup, useSummary } from "../../lib/queries";
import { RecentLeaksPanel, SourceHealthPanel, TagPanel } from "./panels";

/**
 * Recharts is ~600 kB of the bundle and only this page uses it, so it loads on demand.
 * The stat tiles — the numbers an analyst checks first — render without waiting for it.
 */
const LeaksPerDayChart = lazy(() =>
  import("./charts").then((m) => ({ default: m.LeaksPerDayChart })),
);
const LeaksPerGroupChart = lazy(() =>
  import("./charts").then((m) => ({ default: m.LeaksPerGroupChart })),
);

const ChartFallback = () => <div className="skeleton chart-box" />;

/**
 * The windows the daily-volume chart can be read over.
 *
 * Trailing windows rather than calendar months: `/api/stats/leaks-per-day` counts back from
 * today in the browser's zone and zero-fills, so every option is one `days` value and the
 * cache key already carries it. 90 is included because a 30-day window cannot show whether
 * a quiet fortnight is unusual for a group.
 */
const DAY_RANGES = [7, 30, 90] as const;

/** Collection is scheduled hourly, so anything past ~2 hours means runs are being missed. */
const STALE_AFTER_MS = 2 * 60 * 60 * 1000;
const DEAD_AFTER_MS = 6 * 60 * 60 * 1000;

function collectionLabel(at: string | null | undefined): string {
  if (!at) return "never";
  return formatRelative(at);
}

function collectionTone(
  at: string | null | undefined,
): "good" | "warning" | "critical" | undefined {
  if (!at) return "critical";
  const age = Date.now() - new Date(at).getTime();
  if (age > DEAD_AFTER_MS) return "critical";
  if (age > STALE_AFTER_MS) return "warning";
  return "good";
}

export function DashboardPage() {
  const summary = useSummary();
  // Local state only: the window is a reading choice, not something to persist or route on.
  const [rangeDays, setRangeDays] = useState<number>(30);
  const perDay = useLeaksPerDay(rangeDays);
  const perGroup = useLeaksPerGroup(8);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="page-sub">
            Ransomware leak activity across all monitored sources. Refreshes every minute.
          </p>
        </div>
      </div>

      {/* Every tile below is a real query. */}
      <div className="grid-tiles">
        <StatTile
          label="Total leaks"
          value={summary.data?.totalLeaks}
          loading={summary.isPending}
        />
        <StatTile
          label="Last 7 days"
          value={summary.data?.leaksLast7Days}
          loading={summary.isPending}
        />
        <StatTile
          label="Last 30 days"
          value={summary.data?.leaksLast30Days}
          loading={summary.isPending}
        />
        <StatTile
          label="Groups tracked"
          value={summary.data?.trackedGroups}
          loading={summary.isPending}
        />
        <StatTile
          label="Active sources"
          value={summary.data?.activeSources}
          loading={summary.isPending}
        />
        {/*
          The only tile that answers "is collection still running?".
          Every other tile counts leaks, so a working crawler that finds nothing new is
          indistinguishable from one that has died — and since these groups publish in
          bursts, "nothing new" is the normal state for hours at a time. This one advances
          on every successful crawl whether or not anything was found.
        */}
        <StatTile
          label="Last collection"
          text={collectionLabel(summary.data?.lastCollectionAt)}
          note={
            summary.data?.failingSources
              ? `${summary.data.failingSources} source(s) failing`
              : "Runs automatically every hour"
          }
          tone={collectionTone(summary.data?.lastCollectionAt)}
          loading={summary.isPending}
        />
      </div>

      {summary.isError && <ErrorState error={summary.error} onRetry={summary.refetch} />}

      <div className="grid-charts grid-trend">
        <section className="card">
          <div className="card-head">
            <h2>Leaks per day — last {rangeDays} days</h2>
            {/*
              Same segmented control as the Live Threat Map, so the two time pickers in the
              app look and behave alike. TanStack Query keys on `days`, so a window already
              looked at comes back from cache instead of refetching.
            */}
            <div className="segmented" role="group" aria-label="Chart time window">
              {DAY_RANGES.map((value) => (
                <button
                  key={value}
                  type="button"
                  className={rangeDays === value ? "active" : undefined}
                  onClick={() => setRangeDays(value)}
                  aria-pressed={rangeDays === value}
                >
                  {value}d
                </button>
              ))}
            </div>
          </div>
          <div className="card-body">
            {perDay.isPending ? (
              <div className="skeleton chart-box" />
            ) : perDay.isError ? (
              <ErrorState error={perDay.error} onRetry={perDay.refetch} />
            ) : (
              <Suspense fallback={<ChartFallback />}>
                <LeaksPerDayChart data={perDay.data.data} />
              </Suspense>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Leaks by ransomware group</h2>
          </div>
          <div className="card-body">
            {perGroup.isPending ? (
              <div className="skeleton chart-box" />
            ) : perGroup.isError ? (
              <ErrorState error={perGroup.error} onRetry={perGroup.refetch} />
            ) : perGroup.data.data.length === 0 ? (
              <EmptyState title="No leaks recorded yet" />
            ) : (
              <Suspense fallback={<ChartFallback />}>
                <LeaksPerGroupChart data={perGroup.data.data} />
              </Suspense>
            )}
          </div>
        </section>
      </div>

      <div className="grid-charts grid-thirds">
        <TagPanel tag="country" title="Top victim countries" />
        <TagPanel tag="sector" title="Top targeted sectors" />
        <TagPanel tag="type" title="Incident types" />
      </div>

      <div className="grid-charts">
        <RecentLeaksPanel />
        <SourceHealthPanel />
      </div>
    </div>
  );
}
