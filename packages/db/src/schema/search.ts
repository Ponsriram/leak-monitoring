import { sql } from "drizzle-orm";
import {
  bigint,
  customType,
  index,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";

/**
 * Postgres `tsvector`. Drizzle has no built-in for it, and storing it as `text` would let a
 * plain string be assigned to a column the database will only accept a tsvector in.
 */
const tsvector = customType<{ data: string; driverData: string }>({
  dataType() {
    return "tsvector";
  },
});

/**
 * What kind of thing a search hit points at.
 *
 * Only the three the indexer writes today. Adding a value is a one-line `ALTER TYPE ... ADD
 * VALUE` migration, which is the honest cost of a new section — better than pre-declaring
 * `ioc` and `web_shell` here and having them silently return nothing for months.
 */
export const searchEntityType = pgEnum("search_entity_type", ["leak", "source", "domain"]);

/**
 * The unified search index — one row per searchable thing, whatever table it really lives in.
 *
 * Search has to answer "what do we know about this company?" in one query. Without this
 * table that means UNION-ing every entity table by hand and re-writing the union every time
 * a section is added, with each arm carrying its own ranking. One denormalized row per
 * entity costs a little write amplification and buys a single indexed query that never
 * changes shape.
 *
 * Two indexes, because company search needs both and neither is sufficient alone:
 *
 *   `tsv`    word matching. "frame group" finds "The Frame Group", stems plurals, ranks by
 *            where the match landed (title beats body — see the weights below).
 *   trigram  substring matching. `to_tsvector` splits on word boundaries, so a search for
 *            "framegroup" does NOT match "The Frame Group", and a search for "framegro"
 *            matches nothing at all. Analysts type domains and fragments constantly.
 *
 * `tsv` is a stored generated column rather than an expression index: the expression is
 * evaluated once on write instead of on every query that has to recheck a candidate row,
 * and the column can be read back to explain *why* something matched.
 */
export const searchDocuments = pgTable(
  "search_documents",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    entityType: searchEntityType("entity_type").notNull(),
    /**
     * The entity's key in its own table, as text.
     *
     * Text rather than bigint because the things worth indexing are not all keyed by a
     * number — a domain's identity *is* its name. A bigint column would force a surrogate
     * key onto `domain_enrichment` purely to satisfy this table.
     */
    entityId: text("entity_id").notNull(),

    /** The primary label: victim name, domain, source name. Weighted highest. */
    title: text("title").notNull(),
    /** Secondary identity: actor group, country, sector. */
    subtitle: text("subtitle"),
    /** Everything else worth matching on, flattened. Weighted lowest. */
    body: text("body"),

    /** Rendered as chips in the result row. Not searched — that is what `body` is for. */
    tags: text("tags").array(),

    /**
     * When the underlying thing happened, so results can be ordered by recency once
     * relevance has done its job. Nullable: a source has no single moment.
     */
    occurredAt: timestamp("occurred_at", { withTimezone: true }),

    tsv: tsvector("tsv").generatedAlwaysAs(
      sql`setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
          setweight(to_tsvector('english', coalesce(subtitle, '')), 'B') ||
          setweight(to_tsvector('english', coalesce(body, '')), 'C')`,
    ),

    indexedAt: timestamp("indexed_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // Identity. The indexer upserts on this, so re-indexing an entity replaces its row
    // rather than accumulating one per run.
    uniqueIndex("search_documents_entity_key").on(t.entityType, t.entityId),

    // Word search.
    index("search_documents_tsv_idx").using("gin", t.tsv),

    // Substring search. `gin_trgm_ops` requires the pg_trgm extension, created in the
    // migration that adds this table.
    index("search_documents_title_trgm_idx").using(
      "gin",
      sql`${t.title} gin_trgm_ops`,
    ),

    // "Newest first" within a type, for the section-scoped result lists.
    index("search_documents_type_occurred_idx").on(t.entityType, t.occurredAt.desc()),
  ],
);

export type SearchDocument = typeof searchDocuments.$inferSelect;
export type NewSearchDocument = typeof searchDocuments.$inferInsert;
