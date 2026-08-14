import {
  bigint,
  index,
  integer,
  pgEnum,
  pgTable,
  text,
  timestamp,
} from "drizzle-orm/pg-core";
import { sources } from "./sources.js";

export const crawlStatus = pgEnum("crawl_status", [
  "running",
  "succeeded",
  "failed",
  "partial",
]);

/**
 * One row per crawl attempt. This is the provenance the old system had none of —
 * "which site did this leak come from, and when did we last successfully reach it?"
 */
export const crawlRuns = pgTable(
  "crawl_runs",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    sourceId: bigint("source_id", { mode: "number" })
      .notNull()
      .references(() => sources.id, { onDelete: "cascade" }),

    status: crawlStatus("status").notNull().default("running"),

    startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),

    pagesFetched: integer("pages_fetched").notNull().default(0),
    pagesChanged: integer("pages_changed").notNull().default(0),
    bytesFetched: bigint("bytes_fetched", { mode: "number" }).notNull().default(0),

    error: text("error"),
  },
  (t) => [index("crawl_runs_source_started_idx").on(t.sourceId, t.startedAt.desc())],
);

/**
 * Raw fetched pages, keyed by content hash.
 *
 * `contentSha256` is the load-bearing column: if a refetch produces a hash we already have,
 * the pipeline stops there and never re-runs extraction. That single check is what turns
 * "reprocess the entire 1.2 MB corpus every run" into "only handle what changed".
 */
export const rawPages = pgTable(
  "raw_pages",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    sourceId: bigint("source_id", { mode: "number" })
      .notNull()
      .references(() => sources.id, { onDelete: "cascade" }),
    crawlRunId: bigint("crawl_run_id", { mode: "number" }).references(() => crawlRuns.id, {
      onDelete: "set null",
    }),

    url: text("url").notNull(),
    pageNo: integer("page_no").notNull().default(1),

    contentSha256: text("content_sha256").notNull(),
    /** Cleaned text, not raw HTML — this is what extraction consumes. */
    text: text("text").notNull(),
    byteSize: integer("byte_size").notNull().default(0),

    fetchedAt: timestamp("fetched_at", { withTimezone: true }).notNull().defaultNow(),
    /** Set once extraction has run against this page, so reruns can skip it. */
    extractedAt: timestamp("extracted_at", { withTimezone: true }),
  },
  (t) => [
    // The "have we seen this content before?" lookup.
    index("raw_pages_source_hash_idx").on(t.sourceId, t.contentSha256),
    // The retention job's scan, and the extraction worker's backlog query.
    index("raw_pages_fetched_at_idx").on(t.fetchedAt),
    index("raw_pages_pending_extract_idx").on(t.extractedAt),
  ],
);

export type CrawlRun = typeof crawlRuns.$inferSelect;
export type NewCrawlRun = typeof crawlRuns.$inferInsert;
export type RawPage = typeof rawPages.$inferSelect;
export type NewRawPage = typeof rawPages.$inferInsert;
