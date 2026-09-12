import { useState } from "react";
import { CopyButton } from "../../components/CopyButton";
import { ExternalLink } from "../../components/ExternalLink";
import { LeakStatusChip } from "../../components/StatusChip";
import { TagChip } from "../../components/TagChip";
import { EmptyState, ErrorState } from "../../components/states";
import { formatBytes, formatDateTime, formatRelative } from "../../lib/format";
import type { IncidentRow, IncidentSort, SiteStatus } from "../../lib/incidents";

/**
 * The table every section renders.
 *
 * One component rather than three, because the sections differ only in which columns they
 * show. Three copies would drift the moment a column was added to one of them — which is
 * exactly how the enrichment columns would end up on Ransomware and not on Dark Web despite
 * both being backed by the same query.
 *
 * Rows expand rather than link away. An analyst scanning a table wants to see the WHOIS
 * contacts for one row without losing their place in the other nineteen, and the detail is
 * already in the response — a navigation would refetch it.
 *
 * Expansion is opt-out per section. Dark Web already carries every field it holds in the
 * collapsed row, so a chevron there opens a panel restating what is on screen.
 * `expandable={false}` drops the affordance rather than forking the component, so the two
 * sections cannot drift.
 */

export type ColumnKey =
  | "timestamp"
  | "type"
  | "actor"
  | "victim"
  | "domain"
  | "country"
  | "sector"
  | "technologies"
  | "siteStatus"
  | "whois"
  | "status"
  | "size"
  | "source";

const COLUMN_LABEL: Record<ColumnKey, string> = {
  timestamp: "First seen",
  type: "Incident type",
  actor: "Threat actor",
  victim: "Victim",
  domain: "Victim domain",
  country: "Victim country",
  sector: "Activity",
  technologies: "Web technologies",
  siteStatus: "Site status",
  whois: "WHOIS email",
  status: "Listing status",
  size: "Size",
  source: "Source",
};

/**
 * Which columns can be ordered, and by what.
 *
 * Only the ones the API can actually sort on. A header that looks clickable and reorders
 * nothing is worse than a header that does not look clickable at all.
 */
const COLUMN_SORT: Partial<Record<ColumnKey, IncidentSort>> = {
  timestamp: "first_seen_at",
  victim: "victim_name",
};

const SITE_STATUS_LABEL: Record<SiteStatus, string> = {
  live: "Live",
  down: "Down",
  error: "Probe failed",
  not_scanned: "Not scanned",
};

const WHOIS_ROLE_LABEL: Record<string, string> = {
  registrarAbuse: "Registrar abuse",
  registrant: "Registrant",
  admin: "Admin",
  tech: "Tech",
  billing: "Billing",
};

export function IncidentTable({
  rows,
  columns,
  loading,
  error,
  onRetry,
  emptyTitle = "Nothing collected yet",
  expandable = true,
  sort,
  order,
  onSort,
}: {
  rows: IncidentRow[];
  columns: ColumnKey[];
  loading: boolean;
  error: unknown;
  onRetry?: () => void;
  emptyTitle?: string;
  /** Off where every field is already in the collapsed row and the panel would restate it. */
  expandable?: boolean;
  sort?: IncidentSort;
  order?: "asc" | "desc";
  onSort?: (sort: IncidentSort) => void;
}) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  function toggle(id: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (loading && rows.length === 0) return <div className="skeleton chart-box" />;
  if (rows.length === 0) return <EmptyState title={emptyTitle} />;

  return (
    <div className="table-wrap">
      <table className="table incident-table">
        <thead>
          <tr>
            {expandable && <th className="col-expand" aria-label="Expand" />}
            {columns.map((column) => (
              <HeaderCell
                key={column}
                column={column}
                sort={sort}
                order={order}
                onSort={onSort}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <IncidentRowView
              key={row.id}
              row={row}
              columns={columns}
              expandable={expandable}
              expanded={expandable && expanded.has(row.id)}
              onToggle={() => toggle(row.id)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HeaderCell({
  column,
  sort,
  order,
  onSort,
}: {
  column: ColumnKey;
  sort?: IncidentSort;
  order?: "asc" | "desc";
  onSort?: (sort: IncidentSort) => void;
}) {
  const sortKey = COLUMN_SORT[column];
  if (!sortKey || !onSort) return <th>{COLUMN_LABEL[column]}</th>;

  const active = sort === sortKey;
  return (
    <th
      className="sortable"
      // The arrow glyph is decorative; aria-sort is what actually reports the ordering.
      aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}
    >
      <button type="button" className="th-sort" onClick={() => onSort(sortKey)}>
        {COLUMN_LABEL[column]}
        <span className="th-sort-arrow" aria-hidden="true">
          {active ? (order === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function IncidentRowView({
  row,
  columns,
  expandable,
  expanded,
  onToggle,
}: {
  row: IncidentRow;
  columns: ColumnKey[];
  expandable: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className={expanded ? "row-expanded" : undefined}>
        {expandable && (
          <td className="col-expand">
            <button
              type="button"
              className="expander"
              onClick={onToggle}
              aria-expanded={expanded}
              aria-label={expanded ? "Collapse row" : "Expand row"}
            >
              {expanded ? "▾" : "▸"}
            </button>
          </td>
        )}
        {columns.map((column) => (
          <td key={column} className={`cell-${column}`}>
            <Cell column={column} row={row} />
          </td>
        ))}
      </tr>
      {expanded && (
        <tr className="row-detail">
          <td colSpan={columns.length + 1}>
            <RowDetail row={row} />
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * How an incident was classified.
 *
 * The column carries a database default, but a row can still arrive with it empty, and
 * "Unclassified" says that honestly where a silent fallback to "Ransomware" would assert
 * something we never derived.
 */
const TYPE_LABEL: Record<string, string> = {
  ransomware: "Ransomware",
  data_leak: "Data Leak",
  credential_dump: "Credential Dump",
  darkweb_mention: "Darkweb Mention",
  defacement: "Defacement",
};

/**
 * The display name for a stored type, for use outside a chip — the filter dropdown.
 *
 * Falls back to title-casing the raw value rather than to "Unclassified": a type we have not
 * styled yet is still a type the pipeline asserted, and folding it into "Unclassified" would
 * misreport the row.
 */
export function incidentTypeLabel(value: string | null | undefined): string {
  const key = normalizeType(value);
  if (!key) return "Unclassified";
  return TYPE_LABEL[key] ?? titleCase(key);
}

export function IncidentTypeChip({ value }: { value: string | null | undefined }) {
  const key = normalizeType(value);
  if (!key) {
    return (
      <span
        className="chip chip-type type-unclassified"
        title="The source stated no incident type and none was derived."
      >
        Unclassified
      </span>
    );
  }
  return <span className={`chip chip-type type-${key}`}>{incidentTypeLabel(key)}</span>;
}

function normalizeType(value: string | null | undefined): string {
  return value?.trim().toLowerCase().replace(/[\s-]+/g, "_") ?? "";
}

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((word) => word[0]!.toUpperCase() + word.slice(1))
    .join(" ");
}

function Cell({ column, row }: { column: ColumnKey; row: IncidentRow }) {
  switch (column) {
    case "timestamp":
      return <TimestampCell value={row.firstSeenAt} />;

    case "type":
      return <IncidentTypeChip value={row.leakType} />;

    case "actor":
      return <span className="chip chip-actor">{row.actorGroup}</span>;

    case "victim":
      return (
        <div className="victim">
          {/*
            Plenty of listings name no company, and this column then falls back to the
            domain — which is the same value the Victim domain column links, so it gets the
            same treatment here rather than rendering as bare text in one column and a link
            in the other.
          */}
          {row.victimName ? (
            <span className="victim-name">{row.victimName}</span>
          ) : row.victimDomain ? (
            <ExternalLink value={row.victimDomain} className="mono" />
          ) : (
            <span className="muted">—</span>
          )}
          {row.pageTitle && row.pageTitle !== row.victimName && (
            <div className="muted small">{row.pageTitle}</div>
          )}
        </div>
      );

    case "domain":
      return row.victimDomain ? (
        <ExternalLink value={row.victimDomain} className="mono" />
      ) : (
        <span className="muted">—</span>
      );

    case "country":
      return row.victimCountry ? (
        <TagChip kind="country" value={row.victimCountry} />
      ) : (
        <span className="muted">N/A</span>
      );

    case "sector":
      return row.victimSector ? (
        <TagChip kind="sector" value={row.victimSector} />
      ) : (
        <span className="muted">—</span>
      );

    case "technologies":
      return <TechnologyCell row={row} />;

    case "siteStatus":
      return row.siteStatus ? (
        <span className={`chip site-${row.siteStatus}`} title={statusHelp(row)}>
          <span className="chip-dot" aria-hidden="true" />
          {SITE_STATUS_LABEL[row.siteStatus]}
        </span>
      ) : (
        // Distinct from "not_scanned": that means a probe ran and reported nothing useful,
        // this means no row exists yet because the sweep has not reached this domain.
        <span className="chip neutral" title="Not enriched yet — the background sweep has not reached this domain.">
          Queued
        </span>
      );

    case "whois":
      return <WhoisCell row={row} />;

    case "status":
      return <LeakStatusChip status={row.status} />;

    case "size":
      return <span className="mono">{formatBytes(row.leakSizeBytes)}</span>;

    case "source":
      return row.sourceSlug ? (
        <span className="chip chip-subtle">{row.sourceSlug}</span>
      ) : (
        <span className="muted">—</span>
      );

    default:
      return null;
  }
}

/**
 * When it happened, absolutely and relatively.
 *
 * Both, because they answer different questions: the absolute stamp is what goes into a
 * ticket, the relative one is what tells you at a glance whether this row is today's. The
 * absolute is primary with the relative muted beneath it, so scanning the column reads as
 * dates rather than as a list of "N days ago" that has to be resolved in your head.
 */
function TimestampCell({ value }: { value: string | null | undefined }) {
  const parsed = toDate(value);
  // An absent or unparseable stamp says so. `new Date(null)` is how a table ends up
  // showing "01 Jan 1970" and sorting a real row to the bottom.
  if (!parsed) return <span className="muted nowrap">Unknown</span>;

  return (
    <div className="stamp">
      <span className="stamp-abs mono nowrap">{formatDateTime(parsed)}</span>
      <span className="stamp-rel muted small nowrap">{formatRelative(parsed)}</span>
    </div>
  );
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function statusHelp(row: IncidentRow): string {
  const when = row.enrichedAt ? `Checked ${formatRelative(row.enrichedAt)}.` : "";
  const code = row.httpStatus ? ` HTTP ${row.httpStatus}.` : "";
  return `${when}${code}`.trim();
}

/**
 * Technology chips — every one of them.
 *
 * These used to cap at four with a "+N" chip carrying the rest. The count was the problem:
 * "+16" is not something an analyst can act on, and the cell it was protecting is the one
 * that answers "what is this victim running", which is the question the column exists for.
 * The container wraps instead, so a WordPress site is a tall row rather than a truncated one.
 */
function TechnologyCell({ row }: { row: IncidentRow }) {
  const technologies = row.technologies ?? [];
  if (technologies.length === 0) return <span className="muted">—</span>;

  return (
    <div className="chips">
      {technologies.map((tech) => (
        <span key={tech} className="chip chip-tech">
          {tech}
        </span>
      ))}
    </div>
  );
}

/**
 * Every contact, on the same reasoning as the technology chips.
 *
 * Abuse first: it is the address anyone acting on this row would actually write to.
 */
function WhoisCell({ row }: { row: IncidentRow }) {
  const contacts = orderedContacts(row);
  if (contacts.length === 0) return <span className="muted">—</span>;

  return (
    <div className="chips">
      {contacts.map(([role, emails]) =>
        emails.map((email) => (
          <span key={`${role}-${email}`} className="chip chip-whois mono">
            <span className="chip-role">{WHOIS_ROLE_LABEL[role] ?? role}</span>
            {email}
          </span>
        )),
      )}
    </div>
  );
}

function orderedContacts(row: IncidentRow): [string, string[]][] {
  return Object.entries(row.whoisContacts ?? {})
    .filter((entry): entry is [string, string[]] => Boolean(entry[1]?.length))
    .sort(([a], [b]) => (a === "registrarAbuse" ? -1 : b === "registrarAbuse" ? 1 : 0));
}

function RowDetail({ row }: { row: IncidentRow }) {
  const contacts = orderedContacts(row);

  return (
    <div className="detail-grid">
      <DetailBlock label="Listing">
        <DetailLine label="Actor" value={row.actorGroup} />
        <DetailLine label="Type" value={<IncidentTypeChip value={row.leakType} />} />
        <DetailLine label="Status" value={row.status} />
        <DetailLine label="Size" value={formatBytes(row.leakSizeBytes)} />
      </DetailBlock>

      {/*
        Four dates, each labelled, because they mean four different things and the collapsed
        row can only carry one. `publishedAt` is what the leak site claimed; `firstSeenAt` is
        when we saw it, which is the only one of the four we can vouch for and therefore the
        one the column sorts on.
      */}
      <DetailBlock label="Timeline">
        <DetailLine label="Published" value={<Stamp value={row.publishedAt} />} />
        <DetailLine label="First seen" value={<Stamp value={row.firstSeenAt} />} />
        <DetailLine label="Last seen" value={<Stamp value={row.lastSeenAt} />} />
        <DetailLine label="Enriched" value={<Stamp value={row.enrichedAt} />} />
      </DetailBlock>

      <DetailBlock label="Victim">
        <DetailLine label="Name" value={row.victimName ?? "—"} />
        <DetailLine
          label="Domain"
          value={
            row.victimDomain ? <ExternalLink value={row.victimDomain} className="mono" /> : "—"
          }
        />
        <DetailLine label="Country" value={row.victimCountry ?? "—"} />
        <DetailLine label="Activity" value={row.victimSector ?? "—"} />
        <DetailLine label="Registrar" value={row.registrar ?? "—"} />
      </DetailBlock>

      {(row.technologies?.length ?? 0) > 0 && (
        <DetailBlock label={`Web technologies · ${row.technologies!.length}`}>
          <div className="chips">
            {row.technologies!.map((tech) => (
              <span key={tech} className="chip chip-tech">
                {tech}
              </span>
            ))}
          </div>
        </DetailBlock>
      )}

      {contacts.length > 0 && (
        <DetailBlock label="WHOIS contacts">
          <div className="chips">
            {contacts.map(([role, emails]) =>
              emails.map((email) => (
                <span key={`${role}-${email}`} className="chip chip-whois mono">
                  <span className="chip-role">{WHOIS_ROLE_LABEL[role] ?? role}</span>
                  {email}
                </span>
              )),
            )}
          </div>
        </DetailBlock>
      )}

      <DetailBlock label="Provenance">
        <DetailLine label="Source" value={row.sourceSlug ?? "—"} />
        {/*
          The onion address is shown as text and never as a link. It is live criminal
          infrastructure; a console can record where a listing came from without offering one
          click to go there. The copy button is the affordance instead — an analyst who needs
          the address gets it without this app navigating to it.
        */}
        {row.sourceUrl && (
          <DetailLine
            label="Listing URL"
            value={
              <span className="src-url">
                <span className="mono break">{row.sourceUrl}</span>
                <CopyButton value={row.sourceUrl} label="Copy listing URL" />
              </span>
            }
          />
        )}
      </DetailBlock>
    </div>
  );
}

/** Absolute stamp with the relative beside it in muted text, or "Unknown". */
function Stamp({ value }: { value: string | null | undefined }) {
  const parsed = toDate(value);
  if (!parsed) return <span className="muted">Unknown</span>;
  return (
    <span className="stamp-inline">
      <span className="mono">{formatDateTime(parsed)}</span>
      <span className="muted small">{formatRelative(parsed)}</span>
    </span>
  );
}

function DetailBlock({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="detail-block">
      <h4>{label}</h4>
      {children}
    </section>
  );
}

function DetailLine({
  label,
  value,
  mono,
  wrap,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  wrap?: boolean;
}) {
  return (
    <div className="detail-line">
      <span className="detail-label">{label}</span>
      <span className={`${mono ? "mono " : ""}${wrap ? "break " : ""}detail-value`}>
        {value}
      </span>
    </div>
  );
}
