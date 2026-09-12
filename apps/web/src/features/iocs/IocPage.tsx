import { CopyButton } from "../../components/CopyButton";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { EmptyState, ErrorState } from "../../components/states";
import { apiFetch, qs } from "../../lib/api";
import { formatDate, formatNumber, formatRelative } from "../../lib/format";
import type { Pagination, WhoisContacts } from "../../lib/queries";

/**
 * Bulk Intelligence · Indicators of Compromise.
 *
 * Everything here came from a public feed, and the provenance column says which one. That is
 * not decoration: these feeds are community-reported and contain false positives, so an
 * analyst acting on a row needs to be able to reach the original report — which is what the
 * feed link is for.
 */

type IocRow = {
  id: number;
  value: string;
  iocType: "ip" | "domain" | "url" | "file_hash" | "email";
  host: string | null;
  tags: string[] | null;
  threat: string | null;
  note: string | null;
  confidence: number | null;
  feed: string;
  feedRef: string | null;
  reporter: string | null;
  reportedAt: string | null;
  firstSeenAt: string;
  lastSeenAt: string;
  registrar: string | null;
  whoisContacts: WhoisContacts | null;
  siteStatus: string | null;
};

type Facets = {
  types: { value: string; total: number }[];
  feeds: { value: string; total: number }[];
  tags: { value: string; total: number }[];
};

type Filters = {
  page: number;
  limit: number;
  type?: string;
  feed?: string;
  tag?: string;
  q?: string;
};

const TYPE_LABEL: Record<string, string> = {
  ip: "IP Address",
  domain: "Domain",
  url: "URL",
  file_hash: "File Hash",
  email: "Email",
};

const WHOIS_ROLE_LABEL: Record<string, string> = {
  registrarAbuse: "Registrar abuse",
  registrant: "Registrant",
  admin: "Admin",
  tech: "Tech",
  billing: "Billing",
};

const PAGE_SIZE = 20;

export function IocPage() {
  const [filters, setFilters] = useState<Filters>({ page: 1, limit: PAGE_SIZE });

  const query = useQuery({
    queryKey: ["iocs", filters],
    queryFn: () =>
      apiFetch<{ data: IocRow[]; pagination: Pagination }>(`/api/iocs${qs(filters)}`),
    placeholderData: (previous) => previous,
    refetchInterval: 60_000,
  });

  const facets = useQuery({
    queryKey: ["ioc-facets"],
    queryFn: () => apiFetch<Facets>("/api/iocs/facets"),
    // The facet lists change when a feed first reports a new tag, which is not a per-minute
    // event — refetching them as often as the table would be a query per minute for a
    // dropdown that is almost always identical.
    staleTime: 5 * 60_000,
  });

  const rows = query.data?.data ?? [];
  const pagination = query.data?.pagination;

  function update(patch: Partial<Filters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Bulk Intelligence · Indicators of Compromise</h1>
          <p className="page-sub">
            Curated indicators (IP, domain, URL, file hash) from public threat feeds
          </p>
        </div>
      </div>

      <div className="filter-bar">
        <input
          type="search"
          className="filter-input"
          placeholder="Search indicators — IP, domain, URL, hash…"
          value={filters.q ?? ""}
          onChange={(event) => update({ q: event.target.value || undefined })}
          aria-label="Search indicators"
        />

        <select
          className="filter-select"
          value={filters.type ?? ""}
          onChange={(event) => update({ type: event.target.value || undefined })}
          aria-label="Filter by indicator type"
        >
          <option value="">All types</option>
          {(facets.data?.types ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {TYPE_LABEL[row.value] ?? row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.tag ?? ""}
          onChange={(event) => update({ tag: event.target.value || undefined })}
          aria-label="Filter by tag"
        >
          <option value="">All tags</option>
          {(facets.data?.tags ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.feed ?? ""}
          onChange={(event) => update({ feed: event.target.value || undefined })}
          aria-label="Filter by feed"
        >
          <option value="">All feeds</option>
          {(facets.data?.feeds ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({formatNumber(row.total)})
            </option>
          ))}
        </select>

        {(filters.q || filters.type || filters.tag || filters.feed) && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setFilters({ page: 1, limit: PAGE_SIZE })}
          >
            Clear
          </button>
        )}
      </div>

      <section className="card">
        <div className="card-body no-pad">
          {query.isError ? (
            <ErrorState error={query.error} onRetry={query.refetch} />
          ) : query.isPending && rows.length === 0 ? (
            <div className="skeleton chart-box" />
          ) : rows.length === 0 ? (
            <EmptyState title="No indicators match these filters" />
          ) : (
            <div className="table-wrap">
              <table className="table incident-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Indicator of compromise</th>
                    <th>Type</th>
                    <th>Tags</th>
                    <th>Note</th>
                    <th>WHOIS</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <IocRowView key={row.id} row={row} />
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
              {formatNumber(pagination.total)} indicators
            </span>
            <div className="pager">
              <button
                type="button"
                className="btn btn-sm"
                disabled={pagination.page <= 1}
                onClick={() =>
                  setFilters((current) => ({ ...current, page: current.page - 1 }))
                }
              >
                ‹
              </button>
              <span className="muted">
                {pagination.page} / {formatNumber(pagination.totalPages)}
              </span>
              <button
                type="button"
                className="btn btn-sm"
                disabled={pagination.page >= pagination.totalPages}
                onClick={() =>
                  setFilters((current) => ({ ...current, page: current.page + 1 }))
                }
              >
                ›
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

function IocRowView({ row }: { row: IocRow }) {
  const contacts = Object.entries(row.whoisContacts ?? {}).filter(([, v]) => v?.length);

  return (
    <tr>
      <td className="nowrap mono" title={row.reportedAt ?? row.firstSeenAt}>
        {formatDate(row.reportedAt ?? row.firstSeenAt)}
        <div className="muted small">{formatRelative(row.reportedAt ?? row.firstSeenAt)}</div>
      </td>

      <td>
        {/*
          Rendered as plain text, never as a link. These are live malware-delivery and
          phishing URLs; a console records them so they can be blocked, and one accidental
          click is the entire risk this column carries. The feed's own report page is the
          safe destination, and that is what the provenance chip below links to.

          The copy button is the affordance instead: an indicator is copied into a blocklist
          far more often than it is read, and selecting a long hash out of a table cell by
          hand is how the wrong half of one ends up pasted.
        */}
        <span className="ioc-value-row">
          <code className="ioc-value">{row.value}</code>
          <CopyButton value={row.value} label="Copy indicator" />
        </span>
        {row.threat && <div className="muted small">{row.threat}</div>}
      </td>

      <td>
        <span className={`chip ioc-type-${row.iocType}`}>
          {TYPE_LABEL[row.iocType] ?? row.iocType}
        </span>
      </td>

      <td>
        {/*
          Every tag, not the first three. A feed's tags are the malware family and the
          campaign — the two things a row is actually filtered and triaged by — so a "+4"
          hiding them defeats the column. The cell wraps and grows instead.
        */}
        <div className="chips">
          {(row.tags ?? []).map((tag) => (
            <span key={tag} className="chip chip-tag">
              {tag}
            </span>
          ))}
          {(row.tags?.length ?? 0) === 0 && <span className="muted">—</span>}
        </div>
      </td>

      <td className="cell-note">
        {row.note ? <span className="muted small">{row.note}</span> : <span className="muted">N/A</span>}
        <div className="chips">
          {row.feedRef ? (
            <a
              className="chip chip-subtle"
              href={row.feedRef}
              target="_blank"
              rel="noopener noreferrer nofollow"
              title="Open the original report at the feed that asserted this indicator."
            >
              {row.feed} ↗
            </a>
          ) : (
            <span className="chip chip-subtle">{row.feed}</span>
          )}
          {row.confidence != null && (
            <span className="chip chip-subtle" title="Confidence reported by the feed.">
              {row.confidence}%
            </span>
          )}
        </div>
      </td>

      {/*
        Every contact and every address. This used to show the first two roles and the first
        address of each, with no "+N" saying so — a cell that quietly drops an abuse address
        is worse than one that admits it is truncating.
      */}
      <td>
        {contacts.length === 0 ? (
          <span className="muted">—</span>
        ) : (
          <div className="chips">
            {contacts.map(([role, emails]) =>
              (emails ?? []).map((email) => (
                <span key={`${role}-${email}`} className="chip chip-whois mono">
                  <span className="chip-role">{WHOIS_ROLE_LABEL[role] ?? role}</span>
                  {email}
                </span>
              )),
            )}
          </div>
        )}
        {row.registrar && <div className="muted small">{row.registrar}</div>}
      </td>
    </tr>
  );
}
