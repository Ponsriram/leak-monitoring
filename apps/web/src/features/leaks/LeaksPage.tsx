import { useEffect, useState } from "react";
import { LeakStatusChip } from "../../components/StatusChip";
import { TagChip } from "../../components/TagChip";
import { EmptyState, ErrorState, TableSkeleton } from "../../components/states";
import { formatBytes, formatDate, formatNumber, formatRelative } from "../../lib/format";
import {
  useLeaksPerGroup,
  useLeaksPerTag,
  useLeaks,
  type LeakFilters,
} from "../../lib/queries";
import { LatestArrivals } from "./LatestArrivals";
import { SyncButton } from "./SyncButton";

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
  const [country, setCountry] = useState("");
  const [sector, setSector] = useState("");
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
    ...(country ? { country } : {}),
    ...(sector ? { sector } : {}),
  };

  const query = useLeaks(filters);
  const groups = useLeaksPerGroup(50);
  const countries = useLeaksPerTag("country");
  const sectors = useLeaksPerTag("sector");

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
  const hasFilters = Boolean(search || group || status || country || sector);

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
        {/*
          The sync control sits with the page title rather than inside the table card,
          because it does not refresh the table — it asks the collection worker to go and
          fetch the sources, which affects every view in the app.
        */}
        <SyncButton />
      </div>

      <LatestArrivals />

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
              <option value="negotiating">negotiating</option>
              <option value="sold">sold</option>
              <option value="removed">removed</option>
              <option value="unknown">unknown</option>
            </select>

            {/*
              The counts are in the option labels on purpose. These values are extracted, not
              declared by the source, so most rows carry neither — and a filter that silently
              returns three results out of nine hundred reads as a broken filter unless the
              dropdown already said there were three.
            */}
            <select
              value={country}
              onChange={(e) => {
                setCountry(e.target.value);
                setPage(1);
              }}
              aria-label="Filter by location"
            >
              <option value="">Any location</option>
              {countries.data?.data.map((row) => (
                <option key={row.value} value={row.value}>
                  {row.value} ({row.total})
                </option>
              ))}
            </select>

            <select
              value={sector}
              onChange={(e) => {
                setSector(e.target.value);
                setPage(1);
              }}
              aria-label="Filter by sector"
            >
              <option value="">Any sector</option>
              {sectors.data?.data.map((row) => (
                <option key={row.value} value={row.value}>
                  {row.value} ({row.total})
                </option>
              ))}
            </select>

            {hasFilters && (
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setSearch("");
                  setGroup("");
                  setStatus("");
                  setCountry("");
                  setSector("");
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
          <TableSkeleton rows={8} cols={8} />
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
                    {/*
                      Location and sector in one column. They are the two tags the extractor
                      infers rather than reads, they are null together far more often than
                      not, and two mostly-empty columns cost more table width than they
                      return.
                    */}
                    <th title="Location and industry, inferred by the extractor">Tags</th>
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
                    {/*
                      The column that answers "is this listing still up?". `status` cannot:
                      it only ever reports what the page said in words, and `removed` means
                      the site printed the word "removed", not that the listing vanished.
                      A last-seen time that stops advancing is what a delisting looks like.
                    */}
                    <th title="Last crawl that still saw this listing on the site">
                      Last seen
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
                        <div className="tag-cell">
                          {leak.victimCountry && (
                            <TagChip kind="country" value={leak.victimCountry} />
                          )}
                          {leak.victimSector && (
                            <TagChip kind="sector" value={leak.victimSector} />
                          )}
                          {!leak.victimCountry && !leak.victimSector && (
                            <span style={{ color: "var(--muted)" }}>—</span>
                          )}
                        </div>
                      </td>
                      <td>
                        <LeakStatusChip status={leak.status} />
                      </td>
                      <td className="num">{formatBytes(leak.leakSizeBytes)}</td>
                      <td className="num">{formatDate(leak.publishedAt)}</td>
                      <td className="num">{formatRelative(leak.firstSeenAt)}</td>
                      <td className="num">{formatRelative(leak.lastSeenAt)}</td>
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
