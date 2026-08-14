import { useEffect, useState } from "react";
import { LeakStatusChip } from "../../components/StatusChip";
import { EmptyState, ErrorState, TableSkeleton } from "../../components/states";
import { formatBytes, formatDate, formatNumber, formatRelative } from "../../lib/format";
import { useLeaksPerGroup, useLeaks, type LeakFilters } from "../../lib/queries";

const PAGE_SIZE = 25;

/**
 * The leaks table.
 *
 * Everything — search, filtering, sorting, paging — happens in Postgres. The old page
 * fetched the entire collection on mount and then filtered it in JavaScript, so the browser
 * held every leak in memory and the "search" scanned all of them on each keystroke.
 */
export function LeaksPage() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [group, setGroup] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<LeakFilters["sort"]>("first_seen_at");
  const [order, setOrder] = useState<"asc" | "desc">("desc");

  // Debounce so typing doesn't fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(search);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const filters: LeakFilters = {
    page,
    limit: PAGE_SIZE,
    sort,
    order,
    ...(debounced ? { q: debounced } : {}),
    ...(group ? { group } : {}),
    ...(status ? { status } : {}),
  };

  const query = useLeaks(filters);
  const groups = useLeaksPerGroup(50);

  function toggleSort(column: NonNullable<LeakFilters["sort"]>) {
    if (sort === column) {
      setOrder(order === "asc" ? "desc" : "asc");
    } else {
      setSort(column);
      setOrder("desc");
    }
    setPage(1);
  }

  const pagination = query.data?.pagination;
  const rows = query.data?.data ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Leaks</h1>
          <p className="page-sub">
            {pagination
              ? `${formatNumber(pagination.total)} records`
              : "Every recorded victim listing."}
          </p>
        </div>
      </div>

      <section className="card">
        <div className="card-head">
          <div className="controls">
            <input
              type="search"
              placeholder="Search victim or domain…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search leaks"
              style={{ minWidth: 220 }}
            />

            <select
              value={group}
              onChange={(e) => {
                setGroup(e.target.value);
                setPage(1);
              }}
              aria-label="Filter by ransomware group"
            >
              <option value="">All groups</option>
              {groups.data?.data.map((row) => (
                <option key={row.group} value={row.group}>
                  {row.group}
                </option>
              ))}
            </select>

            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
              aria-label="Filter by status"
            >
              <option value="">Any status</option>
              <option value="published">published</option>
              <option value="countdown">countdown</option>
              <option value="sold">sold</option>
              <option value="removed">removed</option>
              <option value="unknown">unknown</option>
            </select>

            {(search || group || status) && (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setSearch("");
                  setGroup("");
                  setStatus("");
                  setPage(1);
                }}
              >
                Clear
              </button>
            )}
          </div>

          {query.isFetching && !query.isPending && (
            <span style={{ color: "var(--muted)", fontSize: 12 }}>Updating…</span>
          )}
        </div>

        {query.isPending ? (
          <TableSkeleton rows={8} cols={6} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={query.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState title="No leaks match those filters">
            <p>Try clearing the search or widening the group filter.</p>
          </EmptyState>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th
                      className="sortable"
                      onClick={() => toggleSort("victim_name")}
                      aria-sort={
                        sort === "victim_name"
                          ? order === "asc"
                            ? "ascending"
                            : "descending"
                          : "none"
                      }
                    >
                      Victim {sort === "victim_name" && (order === "asc" ? "▲" : "▼")}
                    </th>
                    <th>Group</th>
                    <th>Status</th>
                    <th>Size</th>
                    <th
                      className="sortable"
                      onClick={() => toggleSort("published_at")}
                      aria-sort={
                        sort === "published_at"
                          ? order === "asc"
                            ? "ascending"
                            : "descending"
                          : "none"
                      }
                    >
                      Published {sort === "published_at" && (order === "asc" ? "▲" : "▼")}
                    </th>
                    <th
                      className="sortable"
                      onClick={() => toggleSort("first_seen_at")}
                      aria-sort={
                        sort === "first_seen_at"
                          ? order === "asc"
                            ? "ascending"
                            : "descending"
                          : "none"
                      }
                    >
                      First seen {sort === "first_seen_at" && (order === "asc" ? "▲" : "▼")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((leak) => (
                    <tr key={leak.id}>
                      <td className="strong">
                        {leak.victimName ?? "—"}
                        {leak.victimDomain && (
                          <div style={{ color: "var(--muted)", fontSize: 12 }}>
                            {leak.victimDomain}
                          </div>
                        )}
                      </td>
                      <td className="mono">{leak.actorGroup}</td>
                      <td>
                        <LeakStatusChip status={leak.status} />
                      </td>
                      <td className="num">{formatBytes(leak.leakSizeBytes)}</td>
                      <td className="num">{formatDate(leak.publishedAt)}</td>
                      <td className="num">{formatRelative(leak.firstSeenAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {pagination && (
              <div className="pager">
                <div className="pager-info">
                  {formatNumber((pagination.page - 1) * pagination.limit + 1)}–
                  {formatNumber(
                    Math.min(pagination.page * pagination.limit, pagination.total),
                  )}{" "}
                  of {formatNumber(pagination.total)}
                </div>
                <div className="pager-buttons">
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={pagination.page <= 1}
                  >
                    Previous
                  </button>
                  <span className="pager-info">
                    Page {pagination.page} of {pagination.totalPages || 1}
                  </span>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => setPage((p) => p + 1)}
                    disabled={pagination.page >= pagination.totalPages}
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
