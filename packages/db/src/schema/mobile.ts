import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  index,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";

/**
 * Phone numbers reported as used in scams, one row per report.
 *
 * Every row is a public post in which someone reports a number that called, texted or
 * messaged them as part of a scam. The number is pulled out of the post's text with a regex
 * and kept only when libphonenumber agrees it is a real, dialable number
 * (`services/intel/intel/extract/phones.py`).
 *
 * One row per (number, post), not per number: the same number reported in three posts is
 * three reports, each with its own wording, date and target. How many reports a number has is
 * a count over this table, not a column that could drift from it.
 *
 * `threat_types`, `target_audience` and `details` are mandatory. The two arrays are enforced
 * non-empty by CHECK constraints, so a row can never reach the table with a blank column.
 * `details` is the whole post as written — it is never shortened.
 */
export const mobileNumbers = pgTable(
  "mobile_numbers",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** E.164, e.g. "+442085402764". What the table is keyed and searched on. */
    number: text("number").notNull(),
    /** International format for display, e.g. "+44 20 8540 2764". */
    numberDisplay: text("number_display").notNull(),
    /** Exactly as it appeared in the post, e.g. "0208 540 2764". */
    numberRaw: text("number_raw").notNull(),
    /** ISO 3166 alpha-2 region the number belongs to, per libphonenumber. */
    regionCode: text("region_code"),
    country: text("country"),
    /** mobile, fixed_line, fixed_line_or_mobile, voip, toll_free, premium_rate, … */
    lineType: text("line_type"),

    /** What kind of scam the report describes — "Smishing", "Bank Impersonation", … */
    threatTypes: text("threat_types").array().notNull(),
    /** Who the scam is aimed at — "Bank Customers", "Parcel Recipients", … */
    targetAudience: text("target_audience").array().notNull(),
    /** The full text of the report. Never truncated. */
    details: text("details").notNull(),

    /** Which collector found the report: "mastodon", "reddit". */
    source: text("source").notNull(),
    /** Permalink to the post. With `number`, this row's identity. */
    sourceUrl: text("source_url").notNull(),
    author: text("author"),
    language: text("language"),

    /** When the post was published. */
    reportedAt: timestamp("reported_at", { withTimezone: true }),
    /** Set once, on insert — the "new since yesterday" clock, as on leaks and iocs. */
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("mobile_numbers_number_source_key").on(t.number, t.sourceUrl),

    index("mobile_numbers_reported_at_idx").on(t.reportedAt.desc()),
    index("mobile_numbers_first_seen_at_idx").on(t.firstSeenAt.desc()),
    // "Every report of this number" and the per-number report count.
    index("mobile_numbers_number_idx").on(t.number),
    index("mobile_numbers_threat_types_idx").using("gin", t.threatTypes),
    index("mobile_numbers_target_audience_idx").using("gin", t.targetAudience),
    // Substring search over the report text, for "which reports mention DHL".
    index("mobile_numbers_details_trgm_idx").using("gin", sql`${t.details} gin_trgm_ops`),

    check("mobile_numbers_threat_types_present", sql`cardinality(${t.threatTypes}) > 0`),
    check("mobile_numbers_target_audience_present", sql`cardinality(${t.targetAudience}) > 0`),
    check("mobile_numbers_details_present", sql`length(btrim(${t.details})) > 0`),
  ],
);

export type MobileNumber = typeof mobileNumbers.$inferSelect;
export type NewMobileNumber = typeof mobileNumbers.$inferInsert;
