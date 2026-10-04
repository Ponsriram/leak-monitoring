import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  index,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { user } from "./auth.js";

/**
 * What a watch entry names.
 *
 * `domain` is exact-or-subdomain (`acme.com` also matches `mail.acme.com`) and is matched
 * against a victim's domain, an indicator's host and the domain of an exposed email.
 * `keyword` is a substring of a victim's name or an indicator's value — for a brand that is
 * not a domain ("Acme Holdings").
 */
export const watchKind = pgEnum("watch_kind", ["domain", "keyword"]);

/** Which table a match points into. Not a foreign key: it names one of three tables. */
export const matchTarget = pgEnum("match_target", ["leak", "ioc", "exposure"]);

/**
 * What an organisation has asked to be told about.
 *
 * `value` is stored normalised (trimmed, lower-cased, a domain stripped of scheme, path and
 * `www.`) so the unique index means what it appears to mean: "ACME.com" and "acme.com " are
 * one entry, not two that would double every match.
 *
 * `matchedThrough` is the matcher's watermark. A new entry has none, which is how the worker
 * knows to match it against *all* history; after that it only looks at rows first seen since.
 * The API never matches anything itself — the same handoff as `crawl_requests` and
 * `hunt_jobs`, for the same reason.
 */
export const watchlistEntries = pgTable(
  "watchlist_entries",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    kind: watchKind("kind").notNull(),
    value: text("value").notNull(),
    /** A name for people: "Acme — primary domain". Optional. */
    label: text("label"),

    createdBy: text("created_by").references(() => user.id, { onDelete: "set null" }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),

    /** Rows first seen up to here have been matched. Null: never matched, do all of history. */
    matchedThrough: timestamp("matched_through", { withTimezone: true }),
  },
  (t) => [
    uniqueIndex("watchlist_entries_kind_value_key").on(t.kind, t.value),
    // The worker's "which entries still need their first full match?" lookup.
    index("watchlist_entries_unmatched_idx")
      .on(t.id)
      .where(sql`${t.matchedThrough} is null`),

    check("watchlist_entries_value_present", sql`length(btrim(${t.value})) > 0`),
  ],
);

/**
 * One watch entry hitting one record.
 *
 * Matches are written by the worker and only ever *added to*; the one thing a person changes
 * is `acknowledgedAt`, which is what takes a match out of the unread count. Deleting an entry
 * cascades its matches — a match is meaningless without the question it answers.
 */
export const watchlistMatches = pgTable(
  "watchlist_matches",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    entryId: bigint("entry_id", { mode: "number" })
      .notNull()
      .references(() => watchlistEntries.id, { onDelete: "cascade" }),
    targetType: matchTarget("target_type").notNull(),
    targetId: bigint("target_id", { mode: "number" }).notNull(),

    matchedAt: timestamp("matched_at", { withTimezone: true }).notNull().defaultNow(),
    acknowledgedAt: timestamp("acknowledged_at", { withTimezone: true }),
    acknowledgedBy: text("acknowledged_by").references(() => user.id, { onDelete: "set null" }),
  },
  (t) => [
    // Re-matching is safe: the matcher inserts `on conflict do nothing` against this.
    uniqueIndex("watchlist_matches_entry_target_key").on(t.entryId, t.targetType, t.targetId),
    index("watchlist_matches_matched_at_idx").on(t.matchedAt.desc()),
    // The unread count and the "needs attention" filter, over only the rows that qualify.
    index("watchlist_matches_unread_idx")
      .on(t.matchedAt.desc())
      .where(sql`${t.acknowledgedAt} is null`),
  ],
);

export type WatchlistEntry = typeof watchlistEntries.$inferSelect;
export type NewWatchlistEntry = typeof watchlistEntries.$inferInsert;
export type WatchlistMatch = typeof watchlistMatches.$inferSelect;
