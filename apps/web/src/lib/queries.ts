import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, qs } from "./api";

/**
 * One hook per endpoint, typed to match the API's response schemas.
 *
 * Live-data note: this is a monitoring console, so the dashboard queries carry a
 * `refetchInterval`. The old app fetched once on mount and then showed stale numbers
 * indefinitely.
 */

// --- types (mirror the API's zod response schemas) ---

export type Leak = {
  id: number;
  dedupeHash: string;
  victimName: string | null;
  victimDomain: string | null;
  victimCountry: string | null;
  victimSector: string | null;
  actorGroup: string;
  sourceId: number | null;
  sourceSlug: string | null;
  sourceUrl: string | null;
  publishedAt: string | null;
  publishedAtRaw: string | null;
  firstSeenAt: string;
  lastSeenAt: string;
  status: string;
  leakType: string;
  leakSizeBytes: number | null;
};

export type Pagination = {
  page: number;
  limit: number;
  total: number;
  totalPages: number;
};

export type LeakFilters = {
  page: number;
  limit: number;
  group?: string;
  status?: string;
  q?: string;
  sort?: string;
  order?: "asc" | "desc";
};

export type SourceRow = {
  id: number;
  slug: string;
  name: string;
  baseUrl: string;
  collector: string;
  enabled: boolean;
  crawlIntervalSeconds: number;
  lastCrawlAt: string | null;
  lastSuccessAt: string | null;
  consecutiveFailures: number;
  leakCount: number;
  health: "healthy" | "degraded" | "failing" | "disabled";
};

export type Alert = {
  id: number;
  name: string;
  matchKind: "exact" | "domain" | "substring" | "actor_group";
  matchValue: string;
  channel: "email" | "webhook";
  target: string;
  enabled: boolean;
  createdAt: string;
  triggerCount: number;
};

export type AlertEvent = {
  id: number;
  alertId: number;
  alertName: string;
  leakId: number;
  victimName: string | null;
  actorGroup: string;
  matchedOn: string;
  channel: "email" | "webhook";
  status: "pending" | "sent" | "failed";
  sentAt: string | null;
  createdAt: string;
};

export type Summary = {
  totalLeaks: number;
  leaksLast7Days: number;
  leaksLast30Days: number;
  trackedGroups: number;
  activeSources: number;
  alertsTriggered: number;
  lastCollectionAt: string | null;
  failingSources: number;
};

// --- query keys ---

export const keys = {
  leaks: (filters: LeakFilters) => ["leaks", filters] as const,
  leak: (id: number) => ["leak", id] as const,
  sources: () => ["sources"] as const,
  summary: () => ["stats", "summary"] as const,
  perDay: (days: number) => ["stats", "per-day", days] as const,
  perGroup: (limit: number) => ["stats", "per-group", limit] as const,
  alerts: () => ["alerts"] as const,
  alertEvents: () => ["alert-events"] as const,
};

/** 60s — fresh enough for a monitoring view, gentle enough on the database. */
const LIVE_REFETCH = 60_000;

// --- leaks ---

export function useLeaks(filters: LeakFilters) {
  return useQuery({
    queryKey: keys.leaks(filters),
    queryFn: () =>
      apiFetch<{ data: Leak[]; pagination: Pagination }>(`/api/leaks${qs(filters)}`),
    // Keeps the previous page visible while the next one loads, so the table doesn't
    // collapse to a spinner on every page change.
    placeholderData: (previous) => previous,
  });
}

// --- stats ---

export function useSummary() {
  return useQuery({
    queryKey: keys.summary(),
    queryFn: () => apiFetch<Summary>("/api/stats/summary"),
    refetchInterval: LIVE_REFETCH,
  });
}

export function useLeaksPerDay(days = 30) {
  return useQuery({
    queryKey: keys.perDay(days),
    queryFn: () =>
      apiFetch<{ days: number; data: { date: string; total: number }[] }>(
        `/api/stats/leaks-per-day${qs({ days })}`,
      ),
    refetchInterval: LIVE_REFETCH,
  });
}

export function useLeaksPerGroup(limit = 8) {
  return useQuery({
    queryKey: keys.perGroup(limit),
    queryFn: () =>
      apiFetch<{ data: { group: string; total: number }[] }>(
        `/api/stats/leaks-per-group${qs({ limit })}`,
      ),
    refetchInterval: LIVE_REFETCH,
  });
}

// --- sources ---

export function useSources() {
  return useQuery({
    queryKey: keys.sources(),
    queryFn: () => apiFetch<{ data: SourceRow[] }>("/api/sources"),
    refetchInterval: LIVE_REFETCH,
  });
}

// --- alerts ---

export function useAlerts() {
  return useQuery({
    queryKey: keys.alerts(),
    queryFn: () => apiFetch<{ data: Alert[] }>("/api/alerts"),
  });
}

export function useAlertEvents() {
  return useQuery({
    queryKey: keys.alertEvents(),
    queryFn: () => apiFetch<{ total: number; data: AlertEvent[] }>("/api/alerts/events"),
    refetchInterval: LIVE_REFETCH,
  });
}

export type NewAlertInput = {
  name: string;
  matchKind: Alert["matchKind"];
  matchValue: string;
  channel: Alert["channel"];
  target: string;
};

export function useCreateAlert() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: NewAlertInput) =>
      apiFetch<Alert>("/api/alerts", { method: "POST", body: JSON.stringify(input) }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.alerts() }),
  });
}

export function useToggleAlert() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      apiFetch<Alert>(`/api/alerts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.alerts() }),
  });
}

export function useDeleteAlert() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch<void>(`/api/alerts/${id}`, { method: "DELETE" }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.alerts() }),
  });
}
