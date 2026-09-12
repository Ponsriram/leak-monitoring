import { sql } from "drizzle-orm";
import { bigint, index, jsonb, pgEnum, pgTable, text, timestamp } from "drizzle-orm/pg-core";

/**
 * How far along a hunt is.
 *
 * `partial` is a real outcome, not a failure: a hunt fans out to several independent
 * lookups and RDAP being down does not invalidate what certificate transparency found.
 * Collapsing that into `failed` would throw away good findings, and into `succeeded` would
 * quietly claim we looked everywhere when we did not.
 */
export const huntStatus = pgEnum("hunt_status", [
  "queued",
  "running",
  "succeeded",
  "partial",
  "failed",
]);

/**
 * A search that goes and looks, rather than only reading what we already have.
 *
 * The instant half of search reads `search_documents` and returns in milliseconds. This
 * table is the other half: when someone types a company we hold little or nothing on, the
 * API writes a row here and the Python worker — which owns the network egress, the Tor
 * circuits and the politeness budget — picks it up and runs the enrichers.
 *
 * The handoff is a table row for exactly the reason `crawl_requests` is: the API is
 * TypeScript and arq serializes its job payloads with pickle, so enqueueing an arq job from
 * the API would mean maintaining a Python pickle encoder in Node. A row costs nothing, is
 * inspectable with `psql` when a hunt appears to hang, and gives the UI a lifecycle to
 * stream against.
 */
export const huntJobs = pgTable(
  "hunt_jobs",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** Exactly what the user typed, kept for display and for auditing a bad result. */
    query: text("query").notNull(),
    /**
     * Lowercased and whitespace-collapsed. This is the cache key: a hunt for "Frame Group"
     * must reuse a recent hunt for "frame  group" rather than re-running every lookup.
     */
    normalizedQuery: text("normalized_query").notNull(),
    /**
     * The apex domain the query resolved to, once the worker has worked it out.
     * Null while queued, and null forever for a company name we could not map to a domain —
     * which is not a failure, it just means only the corpus lookups had anything to say.
     */
    targetDomain: text("target_domain"),

    status: huntStatus("status").notNull().default("queued"),

    /** Better Auth user id. Null for hunts triggered by automation rather than a person. */
    requestedBy: text("requested_by"),

    requestedAt: timestamp("requested_at", { withTimezone: true }).notNull().defaultNow(),
    startedAt: timestamp("started_at", { withTimezone: true }),
    finishedAt: timestamp("finished_at", { withTimezone: true }),

    /** Denormalized so the results header can render without counting the findings table. */
    findingsCount: bigint("findings_count", { mode: "number" }).notNull().default(0),

    /**
     * Which enrichers did not complete, and why. A map of enricher name to message, so
     * `partial` can say *what* is missing instead of just admitting something is.
     */
    errors: jsonb("errors").$type<Record<string, string>>(),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    /**
     * The cache lookup: "has this been hunted recently?" Most-recent-first on the query, so
     * the freshness check reads exactly one row.
     */
    index("hunt_jobs_query_requested_idx").on(t.normalizedQuery, t.requestedAt.desc()),

    /**
     * The worker's drain query, run every few seconds. Partial on the two open statuses so
     * the index holds only what is actually outstanding — finished hunts accumulate forever
     * and none of them are ever claimed again.
     */
    index("hunt_jobs_status_idx")
      .on(t.status, t.requestedAt)
      .where(sql`${t.status} in ('queued', 'running')`),
  ],
);

/**
 * What a hunt found, one row per fact.
 *
 * Rows rather than a single blob on the job, because the UI streams: each enricher writes
 * its findings the moment it returns and the results page renders them as they land,
 * instead of showing a spinner until the slowest lookup finishes.
 */
export const huntFindingKind = pgEnum("hunt_finding_kind", [
  /** An existing `leaks` row matched the query. The most valuable kind. */
  "leak_match",
  /**
   * The name appears in a page we crawled but never extracted into a leak.
   *
   * This is the kind that justifies keeping `raw_pages` at all: extraction is lossy, and a
   * company can be named on a leak site in wording the linker did not recognise. Searching
   * the corpus we already hold surfaces those for free.
   */
  "raw_page_mention",
  /** WHOIS/RDAP: registrar, contacts, registration dates. */
  "registration",
  /** DNS: address, mail and nameserver records. */
  "infrastructure",
  /** A hostname seen in certificate transparency logs. */
  "certificate",
  /** The single HTTP probe: reachable or not, status code, fingerprinted stack. */
  "liveness",
]);

export const huntFindings = pgTable(
  "hunt_findings",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    jobId: bigint("job_id", { mode: "number" })
      .notNull()
      .references(() => huntJobs.id, { onDelete: "cascade" }),

    kind: huntFindingKind("kind").notNull(),

    /** One-line summary, rendered as the finding's heading. */
    title: text("title").notNull(),
    /** Kind-specific payload. The UI switches on `kind` to render it. */
    detail: jsonb("detail").$type<Record<string, unknown>>(),

    /** Where this came from — "RDAP", "crt.sh", "corpus", "leaks". Shown as provenance. */
    sourceLabel: text("source_label").notNull(),

    /** When the underlying event happened, when that is knowable. */
    occurredAt: timestamp("occurred_at", { withTimezone: true }),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // The results page's only query: everything for one job, grouped by kind.
    index("hunt_findings_job_kind_idx").on(t.jobId, t.kind),
  ],
);

export type HuntJob = typeof huntJobs.$inferSelect;
export type NewHuntJob = typeof huntJobs.$inferInsert;
export type HuntFinding = typeof huntFindings.$inferSelect;
export type NewHuntFinding = typeof huntFindings.$inferInsert;
