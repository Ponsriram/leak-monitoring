import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ChevronLeft, ChevronRight } from "../../components/icons";
import { EmptyState, ErrorState } from "../../components/states";
import { ApiError, apiFetch, qs } from "../../lib/api";
import { formatDate, formatNumber, formatRelative } from "../../lib/format";
import type { Pagination } from "../../lib/queries";

/**
 * Watchlist — what we have been asked to watch for, and what has turned up.
 *
 * Adding an entry only records the question. The worker picks it up within a minute and matches
 * it against everything already collected, which is why a new entry shows "matching…" rather
 * than "0 matches": the two are different answers and the page must not blur them.
 */

type WatchKind = "domain" | "keyword";
type TargetType = "leak" | "ioc" | "exposure";

type Entry = {
  id: number;
  kind: WatchKind;
  value: string;
  label: string | null;
  createdAt: string;
  pending: boolean;
  matchCount: number;
  unreadCount: number;
};

type Match = {
  id: number;
  entryId: number;
  entryKind: WatchKind;
  entryValue: string;
  entryLabel: string | null;
  targetType: TargetType;
  targetId: number;
  title: string | null;
  detail: string | null;
  matchedAt: string;
  acknowledgedAt: string | null;
};

type MatchFilters = { page: number; limit: number; entryId?: number; unread?: string };

const TARGET_LABEL: Record<TargetType, string> = {
  leak: "Leak",
  ioc: "Indicator",
  exposure: "Exposure",
};

const PAGE_SIZE = 20;

const KEYS = {
  entries: ["watchlist"] as const,
  matches: ["watchlist-matches"] as const,
};

export function WatchlistPage() {
  const client = useQueryClient();
  const [filters, setFilters] = useState<MatchFilters>({ page: 1, limit: PAGE_SIZE, unread: "true" });

  const entries = useQuery({
    queryKey: KEYS.entries,
    queryFn: () => apiFetch<{ data: Entry[]; unreadTotal: number }>("/api/watchlist"),
    // Fast while any entry is still waiting for its first match, so "matching…" resolves on
    // its own; otherwise the page checks in once a minute like the rest of the console.
    refetchInterval: (query) => (query.state.data?.data.some((e) => e.pending) ? 5_000 : 60_000),
  });

  const matches = useQuery({
    queryKey: [...KEYS.matches, filters],
    queryFn: () =>
      apiFetch<{ data: Match[]; pagination: Pagination }>(`/api/watchlist/matches${qs(filters)}`),
    placeholderData: (previous) => previous,
    refetchInterval: 30_000,
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: KEYS.entries });
    void client.invalidateQueries({ queryKey: KEYS.matches });
  };

  const remove = useMutation({
    mutationFn: (id: number) => apiFetch<void>(`/api/watchlist/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => {
      // A filter on a deleted entry would show an empty list with no way to tell why.
      setFilters((current) =>
        current.entryId === id ? { ...current, entryId: undefined, page: 1 } : current,
      );
      refresh();
    },
  });

  const acknowledge = useMutation({
    mutationFn: (id: number) =>
      apiFetch<void>(`/api/watchlist/matches/${id}/acknowledge`, { method: "POST" }),
    onSuccess: refresh,
  });

  const acknowledgeAll = useMutation({
    mutationFn: (entryId?: number) =>
      apiFetch<{ acknowledged: number }>("/api/watchlist/matches/acknowledge-all", {
        method: "POST",
        body: JSON.stringify(entryId ? { entryId } : {}),
      }),
    onSuccess: refresh,
  });

  const entryRows = entries.data?.data ?? [];
  const matchRows = matches.data?.data ?? [];
  const pagination = matches.data?.pagination;
  const unreadTotal = entries.data?.unreadTotal ?? 0;
  const selectedEntry = entryRows.find((entry) => entry.id === filters.entryId);

  function filter(patch: Partial<MatchFilters>) {
    setFilters((current) => ({ ...current, ...patch, page: 1 }));
  }

  return (
    <div className="page page-flow watch-page">
      <div className="page-head">
        <div>
          <h1>Bulk Intelligence · Watchlist</h1>
          <p className="page-sub">
            Domains and keywords to watch for — matched against leaks, indicators and exposures
            as they arrive
          </p>
        </div>
        {unreadTotal > 0 && (
          <span className="chip chip-count" role="status">
            {formatNumber(unreadTotal)} unread
          </span>
        )}
      </div>

      <AddEntryForm onAdded={refresh} />

      <section className="card table-card">
        <div className="card-head">
          <h2>Watching</h2>
        </div>
        <div className="card-body no-pad">
          {entries.isError ? (
            <ErrorState error={entries.error} onRetry={entries.refetch} />
          ) : entries.isPending ? (
            <div className="skeleton chart-box" />
          ) : entryRows.length === 0 ? (
            <EmptyState title="Nothing on the watchlist yet — add a domain or keyword above" />
          ) : (
            <div className="table-wrap">
              <table className="table stack-table">
                <thead>
                  <tr>
                    <th>Entry</th>
                    <th>Type</th>
                    <th>Added</th>
                    <th>Matches</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {entryRows.map((entry) => (
                    <tr key={entry.id}>
                      <td data-label="Entry">
                        <code className="ioc-value">{entry.value}</code>
                        {entry.label && <div className="muted small">{entry.label}</div>}
                      </td>
                      <td className="nowrap" data-label="Type">
                        <span className="chip chip-subtle">
                          {entry.kind === "domain" ? "Domain" : "Keyword"}
                        </span>
                      </td>
                      <td className="nowrap" data-label="Added" title={entry.createdAt}>
                        {formatDate(entry.createdAt)}
                        <div className="muted small">{formatRelative(entry.createdAt)}</div>
                      </td>
                      <td className="nowrap" data-label="Matches">
                        {entry.pending ? (
                          <span className="chip chip-subtle" role="status">
                            matching…
                          </span>
                        ) : entry.matchCount === 0 ? (
                          <span className="muted">none yet</span>
                        ) : (
                          <button
                            type="button"
                            className="chip chip-count"
                            onClick={() => filter({ entryId: entry.id, unread: undefined })}
                            title="Show this entry's matches"
                          >
                            {formatNumber(entry.matchCount)}
                            {entry.unreadCount > 0 ? ` · ${formatNumber(entry.unreadCount)} unread` : ""}
                          </button>
                        )}
                      </td>
                      <td className="nowrap cell-actions" data-label="">
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          disabled={remove.isPending}
                          onClick={() => {
                            if (
                              window.confirm(
                                `Stop watching "${entry.value}"? Its ${entry.matchCount} match(es) are removed too.`,
                              )
                            ) {
                              remove.mutate(entry.id);
                            }
                          }}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <div className="filter-bar watch-toolbar">
        <h2 className="filter-title">Matches</h2>

        <select
          className="filter-select"
          value={filters.entryId ?? ""}
          onChange={(event) =>
            filter({ entryId: event.target.value ? Number(event.target.value) : undefined })
          }
          aria-label="Filter by entry"
        >
          <option value="">All entries</option>
          {entryRows.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.value}
            </option>
          ))}
        </select>

        <select
          className="filter-select"
          value={filters.unread ?? ""}
          onChange={(event) => filter({ unread: event.target.value || undefined })}
          aria-label="Filter by read state"
        >
          <option value="">Read and unread</option>
          <option value="true">Unread only</option>
          <option value="false">Read only</option>
        </select>

        <button
          type="button"
          className="btn btn-sm"
          disabled={acknowledgeAll.isPending || unreadTotal === 0}
          onClick={() => acknowledgeAll.mutate(filters.entryId)}
        >
          {selectedEntry ? `Mark "${selectedEntry.value}" as read` : "Mark all as read"}
        </button>
      </div>

      <section className="card table-card">
        <div className="card-body no-pad">
          {matches.isError ? (
            <ErrorState error={matches.error} onRetry={matches.refetch} />
          ) : matches.isPending && matchRows.length === 0 ? (
            <div className="skeleton chart-box" />
          ) : matchRows.length === 0 ? (
            <EmptyState
              title={
                filters.unread === "true"
                  ? "Nothing unread — every match has been seen"
                  : "No matches yet"
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table incident-table stack-table">
                <thead>
                  <tr>
                    <th>Matched</th>
                    <th>Watching</th>
                    <th>Found</th>
                    <th>Where</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {matchRows.map((match) => (
                    <tr key={match.id} className={match.acknowledgedAt ? "row-read" : undefined}>
                      <td className="nowrap mono" data-label="Matched" title={match.matchedAt}>
                        {formatDate(match.matchedAt)}
                        <div className="muted small">{formatRelative(match.matchedAt)}</div>
                      </td>
                      <td data-label="Watching">
                        <code className="ioc-value">{match.entryValue}</code>
                        {match.entryLabel && <div className="muted small">{match.entryLabel}</div>}
                      </td>
                      <td data-label="Found" className="cell-found">
                        <strong>{match.title ?? "(record no longer available)"}</strong>
                        {match.detail && <div className="muted small">{match.detail}</div>}
                      </td>
                      <td className="nowrap" data-label="Where">
                        <span className="chip chip-tag">{TARGET_LABEL[match.targetType]}</span>
                      </td>
                      <td className="nowrap cell-actions" data-label="">
                        {match.acknowledgedAt ? (
                          <span className="muted small">read</span>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={acknowledge.isPending}
                            onClick={() => acknowledge.mutate(match.id)}
                          >
                            Mark read
                          </button>
                        )}
                      </td>
                    </tr>
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
              {formatNumber(pagination.total)} matches
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

function AddEntryForm({ onAdded }: { onAdded: () => void }) {
  const [kind, setKind] = useState<WatchKind>("domain");
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");

  const add = useMutation({
    mutationFn: () =>
      apiFetch<Entry>("/api/watchlist", {
        method: "POST",
        body: JSON.stringify({ kind, value, ...(label.trim() ? { label: label.trim() } : {}) }),
      }),
    onSuccess: () => {
      setValue("");
      setLabel("");
      onAdded();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (value.trim()) add.mutate();
  }

  return (
    <form className="card watch-add" onSubmit={submit}>
      <div className="watch-add-grid">
        <div className="field">
          <label htmlFor="watch-kind">Watch for</label>
          <select
            id="watch-kind"
            className="filter-select"
            value={kind}
            onChange={(event) => setKind(event.target.value as WatchKind)}
          >
            <option value="domain">Domain</option>
            <option value="keyword">Keyword</option>
          </select>
        </div>

        <div className="field">
          <label htmlFor="watch-value">{kind === "domain" ? "Domain" : "Keyword"}</label>
          <input
            id="watch-value"
            className="filter-input"
            placeholder={kind === "domain" ? "acme.com" : "A name or brand, e.g. Acme Holdings"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="watch-label">Label (optional)</label>
          <input
            id="watch-label"
            className="filter-input"
            placeholder="e.g. Primary domain"
            value={label}
            maxLength={120}
            onChange={(event) => setLabel(event.target.value)}
          />
        </div>

        <button type="submit" className="btn btn-primary" disabled={add.isPending || !value.trim()}>
          {add.isPending ? "Adding…" : "Add to watchlist"}
        </button>
      </div>

      <p className="watch-hint muted small">
        {kind === "domain"
          ? "A domain also matches its subdomains — acme.com finds mail.acme.com, not notacme.com. A URL or email address works too."
          : "A keyword matches anywhere inside a victim name or indicator."}
      </p>

      {add.isError && (
        <p className="form-error" role="alert">
          {add.error instanceof ApiError ? add.error.message : "Could not add that entry."}
        </p>
      )}
    </form>
  );
}
