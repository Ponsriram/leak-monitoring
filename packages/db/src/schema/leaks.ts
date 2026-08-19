import { sql } from "drizzle-orm";
import {
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

/**
 * The state of a victim's listing on the leak site.
 *
 * `negotiating` was split out of `removed`: the extractor used to map "paid" and
 * "negotiations ongoing" onto `removed`, which said the opposite of what the page meant —
 * a listing under negotiation is still up, and is the most actionable state there is.
 *
 * None of these mean "we checked and the listing is gone". `unknown` means the page printed
 * no status wording at all, which is the common case; whether a listing is still up is
 * answered by `lastSeenAt`, not by this column.
 */
export const leakStatus = pgEnum("leak_status", [
  "published",
  "countdown",
  "sold",
  "removed",
  "negotiating",
  "unknown",
]);

/** How a given leak's fields were derived. Stored so a bad extractor run can be identified later. */
export type ExtractionMeta = {
  method: "gliner" | "llm" | "manual" | "migrated";
  modelVersion?: string;
  confidence?: number;
  /** Raw spans as extracted, before normalization — kept for debugging bad rows. */
  raw?: Record<string, unknown>;
};

/**
 * The canonical leak entity.
 *
 * Three things here are load-bearing and each fixes a specific defect in the old system:
 *
 *   `dedupeHash`   UNIQUE, so the loader can upsert. The old notebook called insert_one() in a
 *                  loop with no key, which duplicated the entire dataset on every run.
 *   `publishedAt`  a real timestamptz. The old code stored whatever text the site used
 *                  ("10 Feb, 2025"), so `$gte` against a date matched nothing and the weekly
 *                  chart silently rendered empty.
 *   `firstSeenAt`  set on INSERT only. Without it "what's new since yesterday" — the entire
 *                  premise of a monitoring product — has no definition.
 */
export const leaks = pgTable(
  "leaks",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** sha256(actor_group | victim_domain-or-name | published_at). Computed by the pipeline. */
    dedupeHash: text("dedupe_hash").notNull(),

    // --- victim ---
    victimName: text("victim_name"),
    victimDomain: text("victim_domain"),
    victimCountry: text("victim_country"),
    victimSector: text("victim_sector"),

    // --- actor ---
    /** Normalized slug, e.g. "lockbit" — not the raw display string off the page. */
    actorGroup: text("actor_group").notNull(),

    // --- provenance ---
    sourceId: bigint("source_id", { mode: "number" }).references(() => sources.id, {
      onDelete: "set null",
    }),
    sourceUrl: text("source_url"),

    // --- timing ---
    /** Normalized publication date. This is what the charts query. Nullable: some sites omit it. */
    publishedAt: timestamp("published_at", { withTimezone: true }),
    /** The original text, kept so a bad parse can be audited rather than guessed at. */
    publishedAtRaw: text("published_at_raw"),
    /** Set once, on insert. Drives "new leaks" and alerting. */
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
    /** Touched on every crawl that still sees this listing. Drives "is it still up?". */
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),

    // --- payload ---
    status: leakStatus("status").notNull().default("unknown"),
    leakType: text("leak_type").notNull().default("ransomware"),
    /** Normalized from "1.2 TB" etc. Nullable because plenty of listings don't state a size. */
    leakSizeBytes: bigint("leak_size_bytes", { mode: "number" }),

    extraction: jsonb("extraction").$type<ExtractionMeta>(),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // The constraint that makes upsert possible. Everything else here is an optimisation;
    // this one is correctness.
    uniqueIndex("leaks_dedupe_hash_key").on(t.dedupeHash),

    // "What's new" — the dashboard's default ordering.
    index("leaks_first_seen_at_idx").on(t.firstSeenAt.desc()),
    // Filter-by-group then sort, which is the common dashboard query.
    index("leaks_actor_group_first_seen_idx").on(t.actorGroup, t.firstSeenAt.desc()),
    // Time-series aggregation for the per-day chart.
    index("leaks_published_at_idx").on(t.publishedAt.desc()),
    index("leaks_source_idx").on(t.sourceId),

    /**
     * The two tag filters on the Leaks page. Both columns are heavily null — a listing that
     * names no country and whose domain carries no ccTLD gets neither — so these are
     * partial: the index covers only the rows a filter can ever return, and skips the
     * nulls, which are the majority.
     */
    index("leaks_victim_country_idx")
      .on(t.victimCountry, t.firstSeenAt.desc())
      .where(sql`${t.victimCountry} is not null`),
    index("leaks_victim_sector_idx")
      .on(t.victimSector, t.firstSeenAt.desc())
      .where(sql`${t.victimSector} is not null`),

    // Full-text search over victim identity, so the table's search box hits an index
    // instead of pulling every row to the browser and filtering there.
    index("leaks_victim_search_idx").using(
      "gin",
      sql`to_tsvector('english', coalesce(${t.victimName}, '') || ' ' || coalesce(${t.victimDomain}, ''))`,
    ),
  ],
);

export type Leak = typeof leaks.$inferSelect;
export type NewLeak = typeof leaks.$inferInsert;
