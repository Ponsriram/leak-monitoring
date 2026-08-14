import { HealthChip } from "../../components/StatusChip";
import { EmptyState, ErrorState, TableSkeleton } from "../../components/states";
import { formatNumber, formatRelative } from "../../lib/format";
import { useSources } from "../../lib/queries";

/**
 * Monitored sources and their crawl health.
 *
 * This page previously rendered ten hardcoded rows with invented values ("last seen 2 hours
 * ago") that never changed. Every column here comes from the database.
 */
export function SourcesPage() {
  const query = useSources();
  const rows = query.data?.data ?? [];
  const failing = rows.filter((row) => row.health === "failing").length;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Sources</h1>
          <p className="page-sub">
            Sites the collector monitors, with real crawl health.
            {failing > 0 && ` ${failing} currently failing.`}
          </p>
        </div>
      </div>

      <section className="card">
        {query.isPending ? (
          <TableSkeleton rows={6} cols={6} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={query.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState title="No sources configured">
            <p>
              Sources are defined in <code>services/intel/sources.yaml</code> and synced into
              the database by the collection pipeline.
            </p>
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Health</th>
                  <th>Collector</th>
                  <th>Leaks</th>
                  <th>Last success</th>
                  <th>Failures</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((source) => (
                  <tr key={source.id}>
                    <td className="strong">
                      {source.name}
                      <div className="mono" style={{ color: "var(--muted)", fontSize: 12 }}>
                        {source.slug}
                      </div>
                    </td>
                    <td>
                      <HealthChip health={source.health} />
                    </td>
                    <td className="mono">{source.collector}</td>
                    <td className="num">{formatNumber(source.leakCount)}</td>
                    <td className="num">{formatRelative(source.lastSuccessAt)}</td>
                    <td className="num">{source.consecutiveFailures}</td>
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
