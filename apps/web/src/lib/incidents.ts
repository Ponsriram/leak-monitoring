import { useQuery } from "@tanstack/react-query";
import { apiFetch, qs } from "./api";
import type { Pagination, WhoisContacts } from "./queries";

/**
 * The section tables and the live map.
 *
 * Kept out of `queries.ts` because those hooks are one-per-endpoint and these are one hook
 * parameterised by section — the three incident pages differ in which columns they show, not
 * in how they fetch.
 */

export type SiteStatus = "live" | "down" | "error" | "not_scanned";

export type IncidentRow = {
  id: number;
  firstSeenAt: string;
  publishedAt: string | null;
  lastSeenAt: string;
  actorGroup: string;
  victimName: string | null;
  victimDomain: string | null;
  victimCountry: string | null;
  victimSector: string | null;
  status: string;
  leakType: string;
  leakSizeBytes: number | null;
  sourceSlug: string | null;
  sourceUrl: string | null;

  /** All null until the background sweep reaches this domain. */
  siteStatus: SiteStatus | null;
  httpStatus: number | null;
  technologies: string[] | null;
  registrar: string | null;
  whoisContacts: WhoisContacts | null;
  pageTitle: string | null;
  enrichedAt: string | null;
};

export type IncidentSection = "general" | "ransomware" | "darkweb";

/** Exactly what the API will order by. Anything else is rejected by its querystring schema. */
export type IncidentSort = "first_seen_at" | "published_at" | "victim_name";

export type IncidentFilters = {
  page: number;
  limit: number;
  group?: string;
  country?: string;
  sector?: string;
  status?: string;
  siteStatus?: SiteStatus;
  /** `leaks.leak_type` — the incident classification, not the listing status. */
  type?: string;
  q?: string;
  sort?: IncidentSort;
  order?: "asc" | "desc";
};

export type MapPeriod = "date" | "week" | "month";

export type MapActor = {
  actorGroup: string;
  categories: string[];
  targets: number;
  countries: string[];
  counts: { iocs: number; cves: number; ttps: number; tech: number };
};

export type MapData = {
  period: { kind: string; from: string; to: string };
  totals: { actors: number; incidents: number };
  actors: MapActor[];
  points: { country: string; total: number; actors: string[] }[];
  arcs: { actorGroup: string; from: string; to: string }[];
};

export const incidentKeys = {
  list: (section: IncidentSection, filters: IncidentFilters) =>
    ["incidents", section, filters] as const,
  map: (period: MapPeriod, date: string) => ["map", period, date] as const,
};

/** 60s — the tables are a monitoring view, and the live stream invalidates them on arrival. */
const LIVE_REFETCH = 60_000;

export function useIncidents(section: IncidentSection, filters: IncidentFilters) {
  return useQuery({
    queryKey: incidentKeys.list(section, filters),
    queryFn: () =>
      apiFetch<{ data: IncidentRow[]; pagination: Pagination }>(
        `/api/incidents/${section}${qs(filters)}`,
      ),
    // Keeps the current page on screen while the next loads, so paging doesn't collapse the
    // table to a spinner on every click.
    placeholderData: (previous) => previous,
    refetchInterval: LIVE_REFETCH,
  });
}

export function useLiveMap(period: MapPeriod, date: string) {
  return useQuery({
    queryKey: incidentKeys.map(period, date),
    queryFn: () => apiFetch<MapData>(`/api/map/live${qs({ period, date })}`),
    refetchInterval: LIVE_REFETCH,
  });
}
