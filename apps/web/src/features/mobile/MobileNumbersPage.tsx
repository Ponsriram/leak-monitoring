import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { CopyButton } from "../../components/CopyButton";
import { ChevronLeft, ChevronRight } from "../../components/icons";
import { EmptyState, ErrorState } from "../../components/states";
import { apiFetch, qs } from "../../lib/api";
import { formatDate, formatNumber, formatRelative } from "../../lib/format";
import type { Pagination } from "../../lib/queries";

/**
 * Bulk Intelligence · Mobile Numbers.
 *
 * Phone numbers people publicly reported as used against them in scams. The worker reads
 * public Mastodon hashtag timelines and subreddit RSS, finds numbers in each post with a
 * regex, keeps the ones libphonenumber confirms are real, and classifies the post into threat
 * types and a target audience.
 *
 * Threat type, target audience and details are always present — the database refuses a row
 * without them. Details is the whole post, shown in full: no clamp, no "read more".
 */

type MobileRow = {
  id: number;
  number: string;
  numberDisplay: string;
  numberRaw: string;
  regionCode: string | null;
  country: string | null;
  lineType: string | null;
  threatTypes: string[];
  targetAudience: string[];
  details: string;
  source: string;
  sourceUrl: string;
  author: string | null;
  reportedAt: string | null;
  firstSeenAt: string;
  reportCount: number;
};

type Facet = { value: string; total: number }[];
type Facets = { threatTypes: Facet; audiences: Facet; sources: Facet };

type Filters = {
  page: number;
  limit: number;
  threatType?: string;
  audience?: string;
  source?: string;
  q?: string;
};

const LINE_TYPE_LABEL: Record<string, string> = {
  mobile: "Mobile",
  fixed_line: "Landline",
  fixed_line_or_mobile: "Mobile or landline",
  voip: "VoIP",
  toll_free: "Toll-free",
  premium_rate: "Premium rate",
  shared_cost: "Shared cost",
  personal: "Personal number",
  pager: "Pager",
  uan: "UAN",
};

const SOURCE_LABEL: Record<string, string> = {
  mastodon: "Mastodon",
  reddit: "Reddit",
};

const PAGE_SIZE = 20;

export function MobileNumbersPage() {
  const [filters, setFilters] = useState<Filters>({ page: 1, limit: PAGE_SIZE });

  const query = useQuery({
    queryKey: ["mobile-numbers", filters],
    queryFn: () =>
      apiFetch<{ data: MobileRow[]; pagination: Pagination }>(
        `/api/mobile-numbers${qs(filters)}`,
      ),
    placeholderData: (previous) => previous,
    refetchInterval: 60_000,
  });

  const facets = useQuery({
    queryKey: ["mobile-facets"],
    queryFn: () => apiFetch<Facets>("/api/mobile-numbers/facets"),
    staleTime: 5 * 60_000,
  });

  const rows = query.data?.data ?? [];
  const pagination = query.data?.pagination;

  function update(patch: Partial<Filters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  return (
    <div className="page page-fill">
      <div className="page-head">
        <div>
          <h1>Bulk Intelligence · Mobile Numbers</h1>
          <p className="page-sub">
            Phone numbers reported in public posts as used for scam calls, texts and messages
          </p>
        </div>
      </div>

      <div className="filter-bar">
        <input
          type="search"
          className="filter-input"
          placeholder="Search a number, or words in the report…"
          value={filters.q ?? ""}
          onChange={(event) => update({ q: event.target.value || undefined })}
          aria-label="Search mobile numbers"
        />

        <select
          className="filter-select"
          value={filters.threatType ?? ""}
          onChange={(event) => update({ threatType: event.target.value || undefined })}
          aria-label="Filter by threat type"
        >
          <option value="">All threat types</option>
          {(facets.data?.threatTypes ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.audience ?? ""}
          onChange={(event) => update({ audience: event.target.value || undefined })}
          aria-label="Filter by target audience"
        >
          <option value="">All target audiences</option>
          {(facets.data?.audiences ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.source ?? ""}
          onChange={(event) => update({ source: event.target.value || undefined })}
          aria-label="Filter by source"
        >
          <option value="">All sources</option>
          {(facets.data?.sources ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {SOURCE_LABEL[row.value] ?? row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        {(filters.q || filters.threatType || filters.audience || filters.source) && (
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
                filters.q || filters.threatType || filters.audience || filters.source
                  ? "No reports match these filters"
                  : "No scam-number reports collected yet"
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table incident-table mobile-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Mobile number</th>
                    <th>Threat type</th>
                    <th>Target audience</th>
                    <th>Details</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <MobileRowView key={row.id} row={row} />
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
              {formatNumber(pagination.total)} reports
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

function MobileRowView({ row }: { row: MobileRow }) {
  const when = row.reportedAt ?? row.firstSeenAt;

  return (
    <tr>
      <td className="nowrap mono" title={when}>
        {formatDate(when)}
        <div className="muted small">{formatRelative(when)}</div>
      </td>

      <td className="nowrap">
        <span className="ioc-value-row">
          <code className="ioc-value">{row.numberDisplay}</code>
          <CopyButton value={row.number} label="Copy number" />
        </span>
        {row.numberRaw !== row.numberDisplay && (
          <div className="muted small" title="As written in the report">
            Written as {row.numberRaw}
          </div>
        )}
        <div className="chips">
          {row.country && <span className="chip chip-subtle">{row.country}</span>}
          {row.lineType && (
            <span className="chip chip-subtle">{LINE_TYPE_LABEL[row.lineType] ?? row.lineType}</span>
          )}
          {row.reportCount > 1 && (
            <span
              className="chip chip-count"
              title="Reports in this table that name this same number"
            >
              {formatNumber(row.reportCount)} reports
            </span>
          )}
        </div>
      </td>

      <td>
        <div className="chips">
          {row.threatTypes.map((type) => (
            <span key={type} className="chip chip-threat">
              {type}
            </span>
          ))}
        </div>
      </td>

      <td>
        <div className="chips">
          {row.targetAudience.map((audience) => (
            <span key={audience} className="chip chip-tag">
              {audience}
            </span>
          ))}
        </div>
      </td>

      {/* The whole report. Line breaks are the author's and are kept. */}
      <td className="cell-details">{row.details}</td>

      <td className="nowrap">
        <a
          className="chip chip-subtle"
          href={row.sourceUrl}
          target="_blank"
          rel="noopener noreferrer nofollow"
          title="Open the original post"
        >
          {SOURCE_LABEL[row.source] ?? row.source} ↗
        </a>
        {row.author && <div className="muted small">{row.author}</div>}
      </td>
    </tr>
  );
}
