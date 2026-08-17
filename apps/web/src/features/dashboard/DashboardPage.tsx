import { lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { StatTile } from "../../components/StatTile";
import { EmptyState, ErrorState } from "../../components/states";
import { formatRelative } from "../../lib/format";
import { useAlertEvents, useLeaksPerDay, useLeaksPerGroup, useSummary } from "../../lib/queries";

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
  const perDay = useLeaksPerDay(30);
  const perGroup = useLeaksPerGroup(8);
  const events = useAlertEvents();

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="page-sub">
            Ransomware leak activity across all monitored sources. Refreshes every minute.
          </p>
        </div>
      </div>

      {/* Every tile below is a real query. The old dashboard hardcoded "Mentions: 6" and
          read its alert counter from a collection nothing ever wrote to. */}
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
        <StatTile
          label="Alerts fired"
          value={summary.data?.alertsTriggered}
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

      <div className="grid-charts">
        <section className="card">
          <div className="card-head">
            <h2>Leaks per day — last 30 days</h2>
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

      <section className="card">
        <div className="card-head">
          <h2>Recent alert activity</h2>
          <Link to="/dashboard/alerts">Manage alerts</Link>
        </div>

        {events.isPending ? (
          <div className="card-body">
            <div className="skeleton" style={{ height: 80 }} />
          </div>
        ) : events.isError ? (
          <ErrorState error={events.error} onRetry={events.refetch} />
        ) : events.data.data.length === 0 ? (
          <EmptyState title="No alerts have fired yet">
            <p>
              Alert deliveries appear here once the collection pipeline is running and a
              rule matches a new leak.
            </p>
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Alert</th>
                  <th>Victim</th>
                  <th>Group</th>
                  <th>Matched on</th>
                  <th>Status</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {events.data.data.slice(0, 8).map((event) => (
                  <tr key={event.id}>
                    <td className="strong">{event.alertName}</td>
                    <td>{event.victimName ?? "—"}</td>
                    <td className="mono">{event.actorGroup}</td>
                    <td className="mono">{event.matchedOn}</td>
                    <td>{event.status}</td>
                    <td className="num">{formatRelative(event.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
