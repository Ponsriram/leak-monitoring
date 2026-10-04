import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  index,
  integer,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { sources } from "./sources.js";

/** What kind of secret a finding is. Mirrors `ExposureKind` in `intel/extract/secrets.py`. */
export const exposureKind = pgEnum("exposure_kind", [
  "credential_pair",
  "password_hash",
  "api_key",
  "payment_card",
  "private_key",
]);

/**
 * Credentials, keys and card numbers found inside crawled pages.
 *
 * **This table never holds the secret.** A finding is stored as a masked `preview`
 * (`j***@acme.com:********`, `AKIA…QRST`) and a `fingerprint`: an HMAC of the value under a
 * deployment secret. The preview is what an analyst reads; the fingerprint is how the same
 * finding on a re-crawled page is recognised as the same row. Keeping the plaintext here would
 * turn a monitoring database into a second copy of the leak it monitors — a liability for
 * whoever runs it, and a prize for whoever breaches it.
 *
 * `emailDomain` is the one clear-text field derived from the value, because it names an
 * organisation rather than a person and is what the watchlist matches on.
 *
 * `confidence` is 0-100 and comes from the rule that matched, raised when the page around it
 * corroborates (a card number beside "cvv", a combolist's worth of pairs). It is a ranking,
 * not a probability.
 */
export const exposures = pgTable(
  "exposures",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    kind: exposureKind("kind").notNull(),
    /** The rule that matched: "aws_access_key_id", "email_password", "bcrypt", "visa"… */
    detector: text("detector").notNull(),

    /** HMAC-SHA256 of the value. With `kind`, this row's identity. */
    fingerprint: text("fingerprint").notNull(),
    /** Masked, safe to display. Never reversible to the value. */
    preview: text("preview").notNull(),
    /** Organisation part of an email-bearing finding. Null for keys, hashes and cards. */
    emailDomain: text("email_domain"),
    confidence: integer("confidence").notNull(),

    /** Where it was found. Null for a finding from a local file scan. */
    sourceId: bigint("source_id", { mode: "number" }).references(() => sources.id, {
      onDelete: "set null",
    }),
    sourceUrl: text("source_url"),

    /** Set once, on insert — the "new since yesterday" clock, as on leaks and iocs. */
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
    /** Advanced every time a crawl sees it again: "is this still on the page?" */
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("exposures_fingerprint_kind_key").on(t.fingerprint, t.kind),

    index("exposures_first_seen_at_idx").on(t.firstSeenAt.desc()),
    index("exposures_kind_first_seen_idx").on(t.kind, t.firstSeenAt.desc()),
    // The watchlist's domain join, and "everything exposed for acme.com".
    index("exposures_email_domain_idx")
      .on(t.emailDomain)
      .where(sql`${t.emailDomain} is not null`),
    index("exposures_source_idx").on(t.sourceId),

    check("exposures_confidence_range", sql`${t.confidence} between 0 and 100`),
    check("exposures_preview_present", sql`length(btrim(${t.preview})) > 0`),
  ],
);

export type Exposure = typeof exposures.$inferSelect;
export type NewExposure = typeof exposures.$inferInsert;
