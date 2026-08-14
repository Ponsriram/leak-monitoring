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

export type Source = typeof sources.$inferSelect;
export type NewSource = typeof sources.$inferInsert;
