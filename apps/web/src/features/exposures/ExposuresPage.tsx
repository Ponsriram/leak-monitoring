import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ChevronLeft, ChevronRight } from "../../components/icons";
import { EmptyState, ErrorState } from "../../components/states";
import { apiFetch, qs } from "../../lib/api";
import { formatDate, formatNumber, formatRelative } from "../../lib/format";
import type { Pagination } from "../../lib/queries";

/**
 * Exposures — credentials, keys and card numbers found inside crawled pages.
 *
 * Nothing on this page is a secret. The worker stores a masked preview and a keyed hash, never
 * the value (`services/intel/intel/extract/secrets.py`), so what is shown is all there is: enough
 * to recognise a finding and see whose it is, not enough to use it.
 */

type Kind = "credential_pair" | "password_hash" | "api_key" | "payment_card" | "private_key";

type ExposureRow = {
  id: number;
  kind: Kind;
  detector: string;
  preview: string;
  emailDomain: string | null;
  confidence: number;
  sourceSlug: string | null;
  sourceUrl: string | null;
  firstSeenAt: string;
  lastSeenAt: string;
  watchCount: number;
};

type Facet = { value: string; total: number }[];
type Facets = { kinds: Facet; detectors: Facet };

type Filters = {
  page: number;
  limit: number;
  kind?: string;
  minConfidence?: number;
  watched?: string;
  q?: string;
};

const KIND_LABEL: Record<Kind, string> = {
  credential_pair: "Credential",
  password_hash: "Password hash",
  api_key: "API key",
  payment_card: "Payment card",
  private_key: "Private key",
};

const PAGE_SIZE = 20;

/** The score is a ranking from the rule that matched, so it is banded, not shown as a percent. */
function band(confidence: number): { label: string; className: string } {
  if (confidence >= 80) return { label: "High", className: "chip chip-threat" };
  if (confidence >= 60) return { label: "Medium", className: "chip chip-tag" };
  return { label: "Low", className: "chip chip-subtle" };
}

export function ExposuresPage() {
  const [filters, setFilters] = useState<Filters>({ page: 1, limit: PAGE_SIZE });
  const filtered = Boolean(
    filters.q || filters.kind || filters.minConfidence || filters.watched,
  );

  const query = useQuery({
    queryKey: ["exposures", filters],
    queryFn: () =>
      apiFetch<{ data: ExposureRow[]; pagination: Pagination }>(`/api/exposures${qs(filters)}`),
    placeholderData: (previous) => previous,
    refetchInterval: 60_000,
  });

  const facets = useQuery({
    queryKey: ["exposure-facets"],
    queryFn: () => apiFetch<Facets>("/api/exposures/facets"),
    staleTime: 5 * 60_000,
  });

  const rows = query.data?.data ?? [];
  const pagination = query.data?.pagination;

  function update(patch: Partial<Filters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  return (
    <div className="page page-flow">
      <div className="page-head">
        <div>
          <h1>Bulk Intelligence · Exposures</h1>
          <p className="page-sub">
            Credentials, keys and card numbers found in crawled pages — stored masked, never in
            the clear
          </p>
        </div>
      </div>

      <div className="filter-bar exposure-filters">
        <input
          type="search"
          className="filter-input"
          placeholder="Search a domain, detector or preview…"
          value={filters.q ?? ""}
          onChange={(event) => update({ q: event.target.value || undefined })}
          aria-label="Search exposures"
        />

        <select
          className="filter-select"
          value={filters.kind ?? ""}
          onChange={(event) => update({ kind: event.target.value || undefined })}
          aria-label="Filter by kind"
        >
          <option value="">All kinds</option>
          {(facets.data?.kinds ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {KIND_LABEL[row.value as Kind] ?? row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.minConfidence ?? ""}
          onChange={(event) =>
            update({ minConfidence: event.target.value ? Number(event.target.value) : undefined })
          }
          aria-label="Filter by confidence"
        >
          <option value="">Any confidence</option>
          <option value="80">High (80+)</option>
          <option value="60">Medium or higher (60+)</option>
        </select>

        <select
          className="filter-select"
          value={filters.watched ?? ""}
          onChange={(event) => update({ watched: event.target.value || undefined })}
          aria-label="Filter by watchlist"
        >
          <option value="">Watched or not</option>
          <option value="true">On the watchlist</option>
          <option value="false">Not on the watchlist</option>
        </select>

        {filtered && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setFilters({ page: 1, limit: PAGE_SIZE })}
          >
            Clear
          </button>
        )}
      </div>

      <section className="card table-card">
        <div className="card-body no-pad">
          {query.isError ? (
            <ErrorState error={query.error} onRetry={query.refetch} />
          ) : query.isPending && rows.length === 0 ? (
            <div className="skeleton chart-box" />
          ) : rows.length === 0 ? (
            <EmptyState
              title={
                filtered
                  ? "No exposures match these filters"
                  : "No exposures found yet — the crawled pages have not contained any"
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table incident-table stack-table">
                <thead>
                  <tr>
                    <th>First seen</th>
                    <th>Finding</th>
                    <th>Organisation</th>
                    <th>Confidence</th>
                    <th>Source</th>
                    <th>Watchlist</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <ExposureRowView key={row.id} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {pagination && pagination.total > 0 && (
          <div className="card-foot">
            <span className="muted">
              Showing {(pagination.page - 1) * pagination.limit + 1}–
              {Math.min(pagination.page * pagination.limit, pagination.total)} of{" "}
              {formatNumber(pagination.total)} findings
            </span>
            <div className="pager">
              <button
                type="button"
                className="btn btn-sm"
                disabled={pagination.page <= 1}
                onClick={() => setFilters((current) => ({ ...current, page: current.page - 1 }))}
                aria-label="Previous page"
              >
                <ChevronLeft size={15} />
              </button>
              <span className="muted">
                {pagination.page} / {formatNumber(pagination.totalPages)}
              </span>
              <button
                type="button"
                className="btn btn-sm"
                disabled={pagination.page >= pagination.totalPages}
                onClick={() => setFilters((current) => ({ ...current, page: current.page + 1 }))}
                aria-label="Next page"
              >
                <ChevronRight size={15} />
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function ExposureRowView({ row }: { row: ExposureRow }) {
  const confidence = band(row.confidence);

  return (
    <tr>
      <td className="nowrap mono" data-label="First seen" title={row.firstSeenAt}>
        {formatDate(row.firstSeenAt)}
        <div className="muted small">{formatRelative(row.firstSeenAt)}</div>
      </td>

      <td data-label="Finding">
        <code className="ioc-value">{row.preview}</code>
        <div className="chips">
          <span className="chip chip-tag">{KIND_LABEL[row.kind] ?? row.kind}</span>
          <span className="chip chip-subtle">{row.detector}</span>
        </div>
      </td>

      <td className="cell-org" data-label="Organisation">
        {row.emailDomain ?? <span className="muted">—</span>}
      </td>

      <td className="nowrap" data-label="Confidence">
        <span className={confidence.className} title={`Score ${row.confidence} of 100`}>
          {confidence.label}
        </span>
        <div className="muted small">{row.confidence}</div>
      </td>

      <td className="nowrap" data-label="Source">
        {row.sourceSlug ? (
          <span className="chip chip-subtle" title={row.sourceUrl ?? undefined}>
            {row.sourceSlug}
          </span>
        ) : (
          <span className="muted">file scan</span>
        )}
      </td>

      <td className="nowrap" data-label="Watchlist">
        {row.watchCount > 0 ? (
          <span className="chip chip-count" title="Watchlist entries that matched this finding">
            Watched · {row.watchCount}
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  );
}
