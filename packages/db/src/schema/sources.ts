import {
  bigint,
  boolean,
  index,
  integer,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";

/** How a source has to be fetched. Browser sources are ~100x more expensive — default to http. */
export const collectorKind = pgEnum("collector_kind", ["http", "browser"]);

/**
 * The onion sites we monitor.
 *
 * This table replaces the 83 URLs that currently live inside a notebook cell, and it is what
 * backs the dashboard's "Ransomware Groups Index" page (today: ten hardcoded fake rows).
 */
export const sources = pgTable(
  "sources",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** Stable machine name, e.g. "lockbit". Used in logs, metrics and the API. */
    slug: text("slug").notNull(),
    /** Display name for the UI. */
    name: text("name").notNull(),
    baseUrl: text("base_url").notNull(),

    /**
     * The address actually in use, when the site has moved off `baseUrl`.
     *
     * Kept separate from `baseUrl` because `sources.yaml` is the source of truth for
     * `baseUrl` and overwrites it on every `intel sources sync` — a failover written there
     * would be silently reverted by the next sync. Null means "use baseUrl".
     */
    activeUrl: text("active_url"),

    collector: collectorKind("collector").notNull().default("http"),

    /**
     * How to walk pages. `none` means the base URL is the whole listing.
     * The old crawler blindly appended `?page=N` to every site, which is why it produced
     * duplicate and empty pages.
     */
    paginationStyle: text("pagination_style").notNull().default("none"),
    maxPages: integer("max_pages").notNull().default(10),

    /** Per-source cadence, so a fast-moving leak site isn't throttled by a dead one. */
    crawlIntervalSeconds: integer("crawl_interval_seconds").notNull().default(3600),
    /** Politeness delay between page fetches within one crawl. */
    requestDelaySeconds: integer("request_delay_seconds").notNull().default(10),

    enabled: boolean("enabled").notNull().default(true),

    // --- health, surfaced on the Index page ---
    lastCrawlAt: timestamp("last_crawl_at", { withTimezone: true }),
    lastSuccessAt: timestamp("last_success_at", { withTimezone: true }),
    consecutiveFailures: integer("consecutive_failures").notNull().default(0),

    notes: text("notes"),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("sources_slug_key").on(t.slug),
    // The scheduler's hot query: "which enabled sources are due?"
    index("sources_enabled_last_crawl_idx").on(t.enabled, t.lastCrawlAt),
  ],
);

/**
 * How much a mirror address is trusted.
 *
 * `candidate` is the default and is deliberately not usable for failover. These addresses
 * are scraped off pages served by criminal infrastructure — a "our new address is X" banner
 * is attacker-controlled text, and following it automatically would let the crawled site
 * choose where the crawler connects. Promotion to `approved` is a human decision
 * (`intel mirrors approve`).
 *
 * `self_declared` sits between the two: the address was found on the source's own page and
 * shares nothing else, so it is used for failover only when the primary address is dead and
 * only when `CRAWL_MIRROR_FAILOVER` is on.
 */
export const mirrorStatus = pgEnum("mirror_status", [
  "candidate",
  "self_declared",
  "approved",
  "rejected",
]);

/**
 * Onion addresses seen on a source's pages.
 *
 * Leak sites rotate addresses constantly and announce the replacement on the old site
 * before it dies. Recording every address a page mentions turns that announcement into
 * something queryable instead of something an operator has to notice by hand.
 */
export const sourceMirrors = pgTable(
  "source_mirrors",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    sourceId: bigint("source_id", { mode: "number" })
      .notNull()
      .references(() => sources.id, { onDelete: "cascade" }),

    /** Full URL, scheme included, as it will be fetched. */
    url: text("url").notNull(),
    /** The onion host on its own, so the same site behind two paths dedupes sensibly. */
    onionHost: text("onion_host").notNull(),

    /** Which page this address was found on — the audit trail for a failover. */
    discoveredFromUrl: text("discovered_from_url"),

    status: mirrorStatus("status").notNull().default("candidate"),

    /** How many distinct crawls have seen it. A one-off mention is weaker evidence. */
    timesSeen: integer("times_seen").notNull().default(1),

    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),
    /** Last time a fetch through this address actually returned a usable page. */
    lastOkAt: timestamp("last_ok_at", { withTimezone: true }),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("source_mirrors_source_host_key").on(t.sourceId, t.onionHost),
    index("source_mirrors_status_idx").on(t.status),
  ],
);

export type Source = typeof sources.$inferSelect;
export type NewSource = typeof sources.$inferInsert;
export type SourceMirror = typeof sourceMirrors.$inferSelect;
export type NewSourceMirror = typeof sourceMirrors.$inferInsert;
