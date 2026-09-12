import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { formatNumber } from "../../lib/format";
import {
  useIncidents,
  type IncidentFilters,
  type IncidentSection,
  type IncidentSort,
  type SiteStatus,
} from "../../lib/incidents";
import { useLeaksPerGroup, useLeaksPerTag } from "../../lib/queries";
import { IncidentTable, incidentTypeLabel, type ColumnKey } from "./IncidentTable";

/**
 * The three World Incidents sections.
 *
 * One page component, configured per section. What differs between them is which columns
 * are worth showing, and that is the only thing `SECTIONS` encodes — the query, the filters
 * and the pagination are identical because the data is.
 */

type SectionConfig = {
  title: string;
  subtitle: string;
  columns: ColumnKey[];
  emptyTitle: string;
  /**
   * Whether rows open a detail panel.
   *
   * Dark Web is false: its seven columns are every field the section holds, so the panel
   * would restate the row it was opened from.
   */
  expandable: boolean;
  /** Whether the incident-type badge and its filter are shown. */
  typed?: boolean;
};

const SECTIONS: Record<IncidentSection, SectionConfig> = {
  general: {
    title: "World Incidents · General",
    subtitle: "Every incident collected across all monitored sources",
    // Type sits directly after the date, beside listing status: the two together are what
    // classify a row, and separating them puts the victim between two halves of one answer.
    columns: [
      "timestamp",
      "type",
      "status",
      "victim",
      "actor",
      "technologies",
      "country",
      "sector",
    ],
    emptyTitle: "No incidents collected yet",
    expandable: true,
    typed: true,
  },
  ransomware: {
    title: "World Incidents · Ransomware",
    subtitle:
      "Automatically collected ransomware leak-site activity — newly listed victims, updated continuously",
    columns: [
      "timestamp",
      "actor",
      "victim",
      "country",
      "domain",
      "technologies",
      "siteStatus",
      "whois",
      "sector",
    ],
    emptyTitle: "No ransomware listings collected yet",
    expandable: true,
  },
  darkweb: {
    title: "World Incidents · Dark Web",
    subtitle:
      "Exposed victim sites, enriched with web-technology and WHOIS data",
    columns: [
      "timestamp",
      "domain",
      "technologies",
      "siteStatus",
      "country",
      "whois",
      "actor",
    ],
    emptyTitle: "No exposed sites collected yet",
    expandable: false,
  },
};

const PAGE_SIZE = 20;

export function IncidentsPage({ section }: { section: IncidentSection }) {
  const config = SECTIONS[section];
  const navigate = useNavigate();

  // Newest first, which is what a monitoring table is for. Stated explicitly rather than
  // left to the API default so the sort arrow in the header has something to reflect on
  // first render.
  const [filters, setFilters] = useState<IncidentFilters>({
    page: 1,
    limit: PAGE_SIZE,
    sort: "first_seen_at",
    order: "desc",
  });

  const query = useIncidents(section, filters);
  const groups = useLeaksPerGroup(40);
  const countries = useLeaksPerTag("country");
  const types = useLeaksPerTag("type");

  const rows = query.data?.data ?? [];
  const pagination = query.data?.pagination;

  /** Any filter change resets to page 1 — staying on page 9 of a new filter shows nothing. */
  function update(patch: Partial<IncidentFilters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  /**
   * Clicking a header sorts by it; clicking the active one flips the direction.
   *
   * A new column starts descending rather than ascending. Every sortable column here is a
   * date or a name where the interesting end is the recent one, and landing on the oldest
   * twenty rows reads as the table having broken.
   */
  function sortBy(next: IncidentSort) {
    setFilters((current) => ({
      ...current,
      sort: next,
      order: current.sort === next && current.order === "desc" ? "asc" : "desc",
      page: 1,
    }));
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{config.title}</h1>
          <p className="page-sub">{config.subtitle}</p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => navigate("/dashboard/map")}
        >
          Live Map
        </button>
      </div>

      <div className="filter-bar">
        <input
          type="search"
          className="filter-input"
          placeholder="Search victim or domain…"
          value={filters.q ?? ""}
          onChange={(event) => update({ q: event.target.value || undefined })}
          aria-label="Search victim or domain"
        />

        <select
          className="filter-select"
          value={filters.group ?? ""}
          onChange={(event) => update({ group: event.target.value || undefined })}
          aria-label="Filter by threat actor"
        >
          <option value="">All actors</option>
          {(groups.data?.data ?? []).map((row) => (
            <option key={row.group} value={row.group}>
              {row.group} ({row.total})
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.country ?? ""}
          onChange={(event) => update({ country: event.target.value || undefined })}
          aria-label="Filter by victim country"
        >
          <option value="">All countries</option>
          {(countries.data?.data ?? []).map((row) => (
            <option key={row.value} value={row.value}>
              {row.value} ({row.total})
            </option>
          ))}
        </select>

        {/* Same rule as the site-status filter below: only where the column is shown. On
            Ransomware the section already pins leak_type, so the dropdown would offer one
            option that changes nothing. */}
        {config.typed && (
          <select
            className="filter-select"
            value={filters.type ?? ""}
            onChange={(event) => update({ type: event.target.value || undefined })}
            aria-label="Filter by incident type"
          >
            <option value="">All incident types</option>
            {(types.data?.data ?? []).map((row) => (
              <option key={row.value} value={row.value}>
                {incidentTypeLabel(row.value)} ({row.total})
              </option>
            ))}
          </select>
        )}

        {/* Only where the column is shown — a site-status filter on a table with no site
            status column filters invisibly, which reads as the table losing rows. */}
        {config.columns.includes("siteStatus") && (
          <select
            className="filter-select"
            value={filters.siteStatus ?? ""}
            onChange={(event) =>
              update({ siteStatus: (event.target.value || undefined) as SiteStatus | undefined })
            }
            aria-label="Filter by site status"
          >
            <option value="">Any site status</option>
            <option value="live">Live</option>
            <option value="down">Down</option>
            <option value="error">Probe failed</option>
            <option value="not_scanned">Not scanned</option>
          </select>
        )}

        {(filters.q ||
          filters.group ||
          filters.country ||
          filters.type ||
          filters.siteStatus) && (
          <button
            type="button"
            className="btn btn-sm"
            // Clears the filters, not the ordering. A sort the analyst chose is not a
            // filter, and resetting it here would undo a choice they did not ask about.
            onClick={() =>
              setFilters((current) => ({
                page: 1,
                limit: PAGE_SIZE,
                sort: current.sort,
                order: current.order,
              }))
            }
          >
            Clear
          </button>
        )}
      </div>

      <section className="card">
        <div className="card-body no-pad">
          <IncidentTable
            rows={rows}
            columns={config.columns}
            loading={query.isPending}
            error={query.isError ? query.error : null}
            onRetry={query.refetch}
            emptyTitle={config.emptyTitle}
            expandable={config.expandable}
            sort={filters.sort}
            order={filters.order}
            onSort={sortBy}
          />
        </div>

        {pagination && pagination.total > 0 && (
          <div className="card-foot">
            <span className="muted">
              Showing {(pagination.page - 1) * pagination.limit + 1}–
              {Math.min(pagination.page * pagination.limit, pagination.total)} of{" "}
              {formatNumber(pagination.total)}
            </span>
            <Pager
              page={pagination.page}
              totalPages={pagination.totalPages}
              onChange={(page) => setFilters((current) => ({ ...current, page }))}
            />
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * Pagination.
 *
 * Shows first, last and a window around the current page. With 748 pages a full list is
 * unusable, and "next/previous only" makes the end of the data unreachable without 700
 * clicks — the reference console solves it the same way, and so does every table that has
 * had to deal with this many rows.
 */
function Pager({
  page,
  totalPages,
  onChange,
}: {
  page: number;
  totalPages: number;
  onChange: (page: number) => void;
}) {
  if (totalPages <= 1) return null;

  const pages: (number | "gap")[] = [];
  const window = 1;
  for (let candidate = 1; candidate <= totalPages; candidate += 1) {
    const nearCurrent = Math.abs(candidate - page) <= window;
    const isEdge = candidate === 1 || candidate === totalPages;
    if (nearCurrent || isEdge) {
      pages.push(candidate);
    } else if (pages[pages.length - 1] !== "gap") {
      pages.push("gap");
    }
  }

  return (
    <nav className="pager" aria-label="Pagination">
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
        aria-label="Previous page"
      >
        ‹
      </button>
      {pages.map((entry, index) =>
        entry === "gap" ? (
          <span key={`gap-${index}`} className="pager-gap" aria-hidden="true">
            …
          </span>
        ) : (
          <button
            key={entry}
            type="button"
            className={`btn btn-sm${entry === page ? " btn-primary" : ""}`}
            onClick={() => onChange(entry)}
            aria-current={entry === page ? "page" : undefined}
          >
            {entry}
          </button>
        ),
      )}
      <button
        type="button"
        className="btn btn-sm"
        onClick={() => onChange(page + 1)}
        disabled={page >= totalPages}
        aria-label="Next page"
      >
        ›
      </button>
    </nav>
  );
}
