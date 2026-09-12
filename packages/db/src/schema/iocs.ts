import { sql } from "drizzle-orm";
import {
  bigint,
  index,
  integer,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";

/**
 * What kind of thing an indicator names.
 *
 * `file_hash` rather than one value per algorithm: MD5, SHA-1 and SHA-256 are all "a hash of
 * the sample", they are filtered together, and splitting them would mean three enum values
 * that no part of the UI ever distinguishes. The algorithm is recoverable from the length.
 */
export const iocType = pgEnum("ioc_type", ["ip", "domain", "url", "file_hash", "email"]);

/**
 * Indicators of compromise, from public feeds.
 *
 * Unlike `leaks`, nothing here is ours — every row came from URLhaus, ThreatFox or another
 * feed, and `feed` plus `feedRef` is what lets a questionable indicator be traced back to
 * whoever asserted it. That provenance is the whole reason to store the upstream link:
 * these feeds are community-reported and do contain false positives, so an analyst has to be
 * able to go and read the original report.
 *
 * A URL indicator also produces a domain indicator, because they are genuinely different
 * facts: "this exact URL served malware" and "this host is involved in malware delivery"
 * have different lifetimes and different responses. The feeds report the former; the latter
 * is what someone blocking at DNS needs.
 */
export const iocs = pgTable(
  "iocs",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    /** The indicator itself, exactly as it will be matched against. */
    value: text("value").notNull(),
    iocType: iocType("ioc_type").notNull(),

    /**
     * The hostname inside the indicator, when it has one.
     *
     * Denormalized on purpose: it is what joins an indicator to `domain_enrichment`, so the
     * IOC table can show the same WHOIS column the other sections show without parsing a URL
     * in SQL on every row.
     */
    host: text("host"),

    /** Feed-supplied labels — "phishing", "ClickFix", a malware family. Rendered as chips. */
    tags: text("tags").array(),
    /** The threat this indicator is associated with: a malware family or a threat type. */
    threat: text("threat"),
    /** Free-text context from the feed. The reference console calls this column NOTE. */
    note: text("note"),
    /** 0-100 where the feed supplies one. Null means the feed does not score its reports. */
    confidence: integer("confidence"),

    /** Which feed asserted this. Never null — an indicator with no provenance is a rumour. */
    feed: text("feed").notNull(),
    /** Upstream permalink, so a questionable indicator can be read at its source. */
    feedRef: text("feed_ref"),
    /** Who reported it upstream, where the feed says. */
    reporter: text("reporter"),

    /** When the feed says the indicator was first observed. Null when it does not say. */
    reportedAt: timestamp("reported_at", { withTimezone: true }),
    /** When we first ingested it. Set once — the "new since yesterday" clock, as on leaks. */
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
    /** Advanced every time a feed still carries it. Answers "is this still being reported?". */
    lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    /**
     * Identity is the pair, not the value alone.
     *
     * `example.com` as a domain indicator and `http://example.com/x` as a URL indicator are
     * two different assertions, and the same string can legitimately arrive as both a URL
     * and, after host extraction, a domain. Keying on value alone would make the second
     * ingest overwrite the first.
     */
    uniqueIndex("iocs_value_type_key").on(t.value, t.iocType),

    // The table's default ordering, newest first.
    index("iocs_reported_at_idx").on(t.reportedAt.desc()),
    index("iocs_first_seen_at_idx").on(t.firstSeenAt.desc()),
    // Filter by type then sort, which is what the type dropdown does.
    index("iocs_type_reported_idx").on(t.iocType, t.reportedAt.desc()),
    // The WHOIS join, and "every indicator for this host".
    index("iocs_host_idx").on(t.host).where(sql`${t.host} is not null`),
    // Tag filtering. GIN because the query is "contains this tag", not "equals this array".
    index("iocs_tags_idx").using("gin", t.tags),

    /**
     * Substring search over the indicator.
     *
     * The same reasoning as the trigram indexes on `leaks`: an analyst pastes a fragment of
     * a URL or the middle of a hash, and full-text tokenization cannot match either. There
     * is no full-text index here at all — an indicator is not prose, and stemming
     * `partnerships.adobecreatives.com` into English lexemes helps nobody.
     */
    index("iocs_value_trgm_idx").using("gin", sql`${t.value} gin_trgm_ops`),
  ],
);

export type Ioc = typeof iocs.$inferSelect;
export type NewIoc = typeof iocs.$inferInsert;
