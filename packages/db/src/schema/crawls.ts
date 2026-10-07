import { sql } from "drizzle-orm";
import {
  type AnyPgColumn,
  bigint,
  index,
  integer,
  jsonb,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { sources } from "./sources.js";

export const crawlStatus = pgEnum("crawl_status", [
  "running",
  "succeeded",
  "failed",
  "partial",
]);

/**
 * How much of a listing a crawl looked at.
 *
 * Recorded because the two are not comparable and averaging them together makes both
 * numbers meaningless: a shallow probe is one page and a couple of seconds, a deep walk is
 * the whole listing. Without this column "how long does a crawl take?" has no answer, and
 * neither does "is the probe actually finding things?" — which is the question that decides
 * whether the split was worth making.
 */
export const crawlDepth = pgEnum("crawl_depth", ["shallow", "deep"]);

/**
 * One row per crawl attempt. This is the provenance — "which site did this leak come from,
 * and when did we last successfully reach it?"
 */
export const crawlRuns = pgTable(
  "crawl_runs",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    sourceId: bigint("source_id", { mode: "number" })
      .notNull()
      .references(() => sources.id, { onDelete: "cascade" }),

    /**
     * The monitoring cycle this per-source attempt belongs to. Null for runs made by the
     * legacy `intel run` path and for everything recorded before cycles existed.
     */
    cycleId: bigint("cycle_id", { mode: "number" }).references(() => crawlCycles.id, {
      onDelete: "set null",
    }),

    status: crawlStatus("status").notNull().default("running"),
    depth: crawlDepth("depth").notNull().default("deep"),

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

    /**
     * Full-text over the crawled corpus itself.
     *
     * Extraction is lossy by construction — the linker only emits a leak when it recognises
     * the wording around a victim — so a company can be named on a page we successfully
     * fetched and still appear nowhere in `leaks`. This index is what lets a hunt search the
     * text we already hold and report "mentioned on this page, never extracted", which is
     * the one finding no external service can produce for us.
     *
     * An expression index rather than a stored column: the corpus is large and mostly cold,
     * and materializing a tsvector per page would grow the table by roughly the size of the
     * text for a query path that runs a few times a minute at most.
     */
    index("raw_pages_text_fts_idx").using("gin", sql`to_tsvector('english', ${t.text})`),
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

/**
 * One whole monitoring crawl: every due source, every URL of theirs, start to finish.
 *
 * `crawl_runs` stays the per-source record the Sources page reads; a cycle is the thing a
 * run belongs to. Cycle #101 has one `crawl_runs` row per source it touched.
 */
export const crawlCycleStatus = pgEnum("crawl_cycle_status", [
  "running",
  "completed",
  "failed",
  "abandoned",
]);

export const crawlCycles = pgTable(
  "crawl_cycles",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    status: crawlCycleStatus("status").notNull().default("running"),
    /** `schedule`, `manual` or `cli` — what started it. */
    trigger: text("trigger").notNull(),
    /** The UI request this cycle answers, when a person pressed Sync. */
    requestId: bigint("request_id", { mode: "number" }).references(() => crawlRequests.id, {
      onDelete: "set null",
    }),

    startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
    /** Bumped by every worker that finishes a job; a cycle that stops beating is abandoned. */
    heartbeatAt: timestamp("heartbeat_at", { withTimezone: true }).notNull().defaultNow(),
    completedAt: timestamp("completed_at", { withTimezone: true }),

    /** Written by the finalizer from `crawl_urls`, not incremented along the way. */
    totalUrls: integer("total_urls").notNull().default(0),
    successfulUrls: integer("successful_urls").notNull().default(0),
    failedUrls: integer("failed_urls").notNull().default(0),
    skippedUrls: integer("skipped_urls").notNull().default(0),
    newPages: integer("new_pages").notNull().default(0),
    changedPages: integer("changed_pages").notNull().default(0),
    unchangedPages: integer("unchanged_pages").notNull().default(0),
    leaksFound: integer("leaks_found").notNull().default(0),
    /** Listings seen again: what the Sync button reports as "seen again". */
    leaksUpdated: integer("leaks_updated").notNull().default(0),
    durationMs: bigint("duration_ms", { mode: "number" }),

    /**
     * The full report, written once at finalization: sources attempted and failed, retries,
     * links discovered and recrawled, an HTTP status histogram, the commonest errors, and a
     * per-source breakdown. A cycle's URLs are re-scheduled by later cycles, so this snapshot
     * is the only durable record of what a finished cycle did.
     */
    summary: jsonb("summary"),

    error: text("error"),
  },
  (t) => [
    /**
     * Only one cycle may be `running`. This is the overlap guard, and it is the database's
     * to enforce rather than a lock in one process's memory, so it holds across workers.
     */
    uniqueIndex("crawl_cycles_one_running")
      .on(t.status)
      .where(sql`${t.status} = 'running'`),
    index("crawl_cycles_started_at_idx").on(t.startedAt.desc()),
  ],
);

export const crawlUrlStatus = pgEnum("crawl_url_status", [
  "queued",
  "running",
  "retry",
  "succeeded",
  "failed",
  "skipped",
]);

/** A page of a source's own listing, or a page reached by following a link out of one. */
export const crawlUrlKind = pgEnum("crawl_url_kind", ["listing", "link"]);

/**
 * The frontier and the queue in one table: a row per URL, claimed by workers with
 * `FOR UPDATE SKIP LOCKED`.
 *
 * The URL is the unit of work. A source does not hold a worker while its pages are walked;
 * its pages are rows, and any worker may take any of them.
 */
export const crawlUrls = pgTable(
  "crawl_urls",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    sourceId: bigint("source_id", { mode: "number" })
      .notNull()
      .references(() => sources.id, { onDelete: "cascade" }),
    /** The cycle that last scheduled this URL. */
    cycleId: bigint("cycle_id", { mode: "number" }).references(() => crawlCycles.id, {
      onDelete: "set null",
    }),
    /** The page this link was found on. Null for listing pages. */
    parentId: bigint("parent_id", { mode: "number" }).references((): AnyPgColumn => crawlUrls.id, {
      onDelete: "set null",
    }),

    url: text("url").notNull(),
    /** What identifies the page: same page, same value, however the link was spelled. */
    urlNormalized: text("url_normalized").notNull(),
    kind: crawlUrlKind("kind").notNull(),
    /** Listing pages only. Claims go in this order, so a short listing prunes the rest. */
    pageNo: integer("page_no"),
    /** 0 for a listing page, 1 and up for each link followed from one. */
    depth: integer("depth").notNull().default(0),

    status: crawlUrlStatus("status").notNull().default("queued"),
    /** `new`, `changed`, `unchanged`, `empty` or `gate` once a fetch has been judged. */
    result: text("result"),

    /**
     * When this URL is next eligible: its due time, or its retry time after a transient
     * failure. One column for both — what matters to a claimer is only "not before this".
     */
    nextCrawlAt: timestamp("next_crawl_at", { withTimezone: true }).notNull().defaultNow(),
    lastCrawledAt: timestamp("last_crawled_at", { withTimezone: true }),
    /** Consecutive failures, across cycles. Reset by a success. */
    failureCount: integer("failure_count").notNull().default(0),
    lastError: text("last_error"),
    /** Written only after the page was ingested, so a crash never reads as "unchanged". */
    contentSha256: text("content_sha256"),
    httpStatus: integer("http_status"),
    responseMs: integer("response_ms"),

    /** Attempts within the current cycle. Reset when the URL is scheduled again. */
    attempt: integer("attempt").notNull().default(0),
    leasedUntil: timestamp("leased_until", { withTimezone: true }),
    leasedBy: text("leased_by"),

    leaksFound: integer("leaks_found").notNull().default(0),
    leaksUpdated: integer("leaks_updated").notNull().default(0),

    discoveredAt: timestamp("discovered_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("crawl_urls_source_url_uq").on(t.sourceId, t.urlNormalized),
    // The claim query: what is eligible, in page order.
    index("crawl_urls_claim_idx")
      .on(t.nextCrawlAt, t.pageNo)
      .where(sql`${t.status} in ('queued', 'retry')`),
    // The reaper, and the global / per-source in-flight counts.
    index("crawl_urls_leases_idx")
      .on(t.leasedUntil)
      .where(sql`${t.status} = 'running'`),
    // "Is this cycle finished?" and the cycle's statistics.
    index("crawl_urls_cycle_idx").on(t.cycleId, t.status),
  ],
);

export type CrawlCycle = typeof crawlCycles.$inferSelect;
export type NewCrawlCycle = typeof crawlCycles.$inferInsert;
export type CrawlUrl = typeof crawlUrls.$inferSelect;
export type NewCrawlUrl = typeof crawlUrls.$inferInsert;
