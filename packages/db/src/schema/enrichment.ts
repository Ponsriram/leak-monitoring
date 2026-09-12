import {
  index,
  integer,
  jsonb,
  pgEnum,
  pgTable,
  text,
  timestamp,
} from "drizzle-orm/pg-core";

/**
 * Whether the victim's site was reachable the last time we asked.
 *
 * `not_scanned` is the default and is a real, displayable state — it means nobody has
 * looked yet, which is different from having looked and found nothing. `error` means the
 * probe itself failed (DNS refused, TLS handshake died) as opposed to the server answering
 * with a failure, which is `down`.
 */
export const siteStatus = pgEnum("site_status", ["not_scanned", "live", "down", "error"]);

/** WHOIS/RDAP contact emails, grouped by the role the registry assigns them. */
export type WhoisContacts = {
  registrarAbuse?: string[];
  registrant?: string[];
  admin?: string[];
  tech?: string[];
  billing?: string[];
};

/** What DNS and certificate transparency turned up. Shape is stable; contents are not. */
export type DnsFacts = {
  a?: string[];
  aaaa?: string[];
  mx?: string[];
  ns?: string[];
  /** Distinct hostnames seen in CT logs for this apex. Capped by the collector. */
  subdomains?: string[];
};

/**
 * Everything we know about a domain that did not come from a leak listing.
 *
 * Keyed by the domain itself, not by a leak, and deliberately so: the same company shows up
 * across ransomware listings, dark-web dumps and IOC feeds, and its WHOIS record is the same
 * record in all of them. Hanging enrichment off `leaks` would re-fetch it once per listing
 * and let two rows about one domain disagree.
 *
 * Every field here comes from a passive lookup — RDAP, DNS, certificate transparency, and a
 * single ordinary GET of the home page. Nothing in this table requires scanning, probing
 * beyond one request, or touching anything the domain's owner has not published.
 *
 * `checkedAt` is what makes this a cache rather than a fact table. Staleness is decided by
 * the caller, because "fresh enough" differs: a hunt triggered by a person wants a recent
 * probe, a table rendering 20 rows is happy with yesterday's.
 */
export const domainEnrichment = pgTable(
  "domain_enrichment",
  {
    /**
     * Normalized apex domain, lowercased, no scheme, no `www.`, no trailing dot.
     * Normalization happens in the enricher — this column stores the result, and being the
     * primary key is what stops `WWW.Example.com` and `example.com` becoming two rows.
     */
    domain: text("domain").primaryKey(),

    // --- registration ---
    registrar: text("registrar"),
    /**
     * Contact emails by role. Registries expose a varying subset and rename the roles
     * between TLDs, so this is a jsonb map rather than five nullable columns that would be
     * null for most rows and still not cover every registry's vocabulary.
     */
    whoisContacts: jsonb("whois_contacts").$type<WhoisContacts>(),
    registeredAt: timestamp("registered_at", { withTimezone: true }),
    expiresAt: timestamp("expires_at", { withTimezone: true }),

    // --- infrastructure ---
    dns: jsonb("dns").$type<DnsFacts>(),

    // --- liveness and stack ---
    status: siteStatus("status").notNull().default("not_scanned"),
    /** The HTTP status the probe actually got back. Null when the request never completed. */
    httpStatus: integer("http_status"),
    /**
     * Fingerprinted technologies — the chips in the Web Technologies column.
     * An array rather than a join table: it is only ever read whole, and never filtered on.
     */
    technologies: text("technologies").array(),
    /** Page title from the probe. Cheap, and the fastest way to spot a parked domain. */
    pageTitle: text("page_title"),

    /** Why the last attempt produced nothing, when it produced nothing. */
    error: text("error"),

    /** When the last probe ran, successful or not. Drives cache staleness. */
    checkedAt: timestamp("checked_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    /**
     * The refresh worker's query: "what is stale, oldest first?" Nulls sort first under
     * `asc`, which is what we want — a domain nobody has ever checked outranks one checked
     * last week.
     */
    index("domain_enrichment_checked_at_idx").on(t.checkedAt),
    index("domain_enrichment_status_idx").on(t.status),
  ],
);

export type DomainEnrichment = typeof domainEnrichment.$inferSelect;
export type NewDomainEnrichment = typeof domainEnrichment.$inferInsert;
