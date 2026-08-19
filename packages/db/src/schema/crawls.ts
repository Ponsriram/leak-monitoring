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

/**
 * How far along an on-demand crawl request is.
 *
 * `skipped` is a real outcome, not an error: a request that arrives while another crawl
 * holds the lock is answered by the run already in flight, and telling the user "already
 * syncing" is more honest than queueing a second crawl of the same sources behind it.
 */
export const crawlRequestStatus = pgEnum("crawl_request_status", [
  "queued",
  "running",
  "succeeded",
  "failed",
  "skipped",
]);

/**
 * A crawl asked for by a person, rather than by the clock.
 *
 * The worker owns Tor and the crawl lock; the API owns the session and the HTTP surface.
 * They share Postgres and nothing else — the API has no Redis client, and arq serializes
 * its jobs with pickle, so "the API enqueues an arq job" would mean reimplementing a Python
 * pickle payload in TypeScript and keeping it in step with arq's format. A row in a table
 * both processes already talk to costs nothing and is directly inspectable when a sync
 * appears to do nothing.
 *
 * It also gives the UI something to poll: queued → running → succeeded is exactly the
 * feedback a "Sync now" button needs, and the counts below are what it reports afterwards.
 */
export const crawlRequests = pgTable(
  "crawl_requests",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** Null means every enabled source. Otherwise the one source slug to crawl. */
    sourceSlug: text("source_slug"),

    status: crawlRequestStatus("status").notNull().default("queued"),

    /** The authenticated user id, so a surprise crawl can be traced to whoever asked. */
    requestedBy: text("requested_by"),
    requestedAt: timestamp("requested_at", { withTimezone: true }).notNull().defaultNow(),
    startedAt: timestamp("started_at", { withTimezone: true }),
    finishedAt: timestamp("finished_at", { withTimezone: true }),

    sourcesCrawled: integer("sources_crawled").notNull().default(0),
    newLeaks: integer("new_leaks").notNull().default(0),
    updatedLeaks: integer("updated_leaks").notNull().default(0),
    failedSources: integer("failed_sources").notNull().default(0),

    error: text("error"),
  },
  (t) => [
    // The worker's tick, several times a minute: "is anything waiting?". Ordered so the
    // oldest queued request is the first row scanned.
    index("crawl_requests_pending_idx").on(t.status, t.requestedAt),
    // The UI's poll: "what happened to the request I just made?".
    index("crawl_requests_requested_at_idx").on(t.requestedAt.desc()),
  ],
);

export type CrawlRequest = typeof crawlRequests.$inferSelect;
export type NewCrawlRequest = typeof crawlRequests.$inferInsert;
