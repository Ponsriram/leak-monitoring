import { domainEnrichment, iocs } from "@leak/db";
import { and, count, desc, eq, sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Indicators of compromise, from the public feeds.
 *
 * The WHOIS column is a join onto `domain_enrichment` via the indicator's host, which is why
 * `iocs.host` is denormalized at ingest rather than parsed out of the URL here — doing it in
 * SQL would mean a function call per row on every page of a table with tens of thousands of
 * them, and it would not be indexable.
 */

const MAX_LIMIT = 100;

const listQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(20),
  type: z.enum(["ip", "domain", "url", "file_hash", "email"]).optional(),
  feed: z.string().min(1).max(40).optional(),
  tag: z.string().min(1).max(60).optional(),
  /** Substring search over the indicator, served by the trigram index. */
  q: z.string().min(1).max(200).optional(),
  sort: z.enum(["reported_at", "first_seen_at"]).default("reported_at"),
});

const iocRow = z.object({
  id: z.number(),
  value: z.string(),
  iocType: z.string(),
  host: z.string().nullable(),
  tags: z.array(z.string()).nullable(),
  threat: z.string().nullable(),
  note: z.string().nullable(),
  confidence: z.number().nullable(),
  feed: z.string(),
  feedRef: z.string().nullable(),
  reporter: z.string().nullable(),
  reportedAt: z.date().nullable(),
  firstSeenAt: z.date(),
  lastSeenAt: z.date(),

  // --- WHOIS, from the enrichment cache. Null until the sweep reaches this host. ---
  registrar: z.string().nullable(),
  whoisContacts: z.record(z.string(), z.array(z.string())).nullable(),
  siteStatus: z.string().nullable(),
});

export const iocRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/iocs",
    {
      schema: {
        description: "Indicators of compromise from public feeds, newest first.",
        tags: ["iocs"],
        querystring: listQuery,
        response: {
          200: z.object({
            data: z.array(iocRow),
            pagination: z.object({
              page: z.number(),
              limit: z.number(),
              total: z.number(),
              totalPages: z.number(),
            }),
          }),
        },
      },
    },
    async (request) => {
      const { page, limit, type, feed, tag, q, sort } = request.query;

      const conditions: SQL[] = [];
      if (type) conditions.push(eq(iocs.iocType, type));
      if (feed) conditions.push(eq(iocs.feed, feed));
      // Array containment, so the GIN index on tags is usable. `= any(tags)` would not be.
      if (tag) conditions.push(sql`${iocs.tags} @> array[${tag}]::text[]`);
      if (q) {
        // Substring only. An indicator is not prose — stemming a URL into English lexemes
        // helps nobody, and a fragment of a hash is the normal way people search here.
        conditions.push(sql`${iocs.value} ilike ${`%${q}%`}`);
      }

      const where = conditions.length > 0 ? and(...conditions) : undefined;
      const sortColumn = sort === "first_seen_at" ? iocs.firstSeenAt : iocs.reportedAt;

      const [rows, totalResult] = await Promise.all([
        fastify.db
          .select({
            id: iocs.id,
            value: iocs.value,
            iocType: iocs.iocType,
            host: iocs.host,
            tags: iocs.tags,
            threat: iocs.threat,
            note: iocs.note,
            confidence: iocs.confidence,
            feed: iocs.feed,
            feedRef: iocs.feedRef,
            reporter: iocs.reporter,
            reportedAt: iocs.reportedAt,
            firstSeenAt: iocs.firstSeenAt,
            lastSeenAt: iocs.lastSeenAt,
            registrar: domainEnrichment.registrar,
            whoisContacts: domainEnrichment.whoisContacts,
            siteStatus: domainEnrichment.status,
          })
          .from(iocs)
          .leftJoin(domainEnrichment, eq(domainEnrichment.domain, iocs.host))
          .where(where)
          // Nulls last: an indicator whose feed gave no timestamp should not head the table.
          .orderBy(sql`${sortColumn} desc nulls last`, desc(iocs.id))
          .limit(limit)
          .offset((page - 1) * limit),

        fastify.db.select({ value: count() }).from(iocs).where(where),
      ]);

      const total = totalResult[0]?.value ?? 0;
      return {
        data: rows.map((row) => ({
          ...row,
          whoisContacts: (row.whoisContacts as Record<string, string[]> | null) ?? null,
        })),
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      };
    },
  );

  /**
   * The filter dropdowns.
   *
   * Served from the server because the client only ever holds one page of rows and could not
   * derive the full set of tags or feeds from it — a facet list built client-side would show
   * only the tags that happen to appear on the current twenty rows.
   */
  fastify.get(
    "/api/iocs/facets",
    {
      schema: {
        description: "Distinct types, feeds and the most common tags, with counts.",
        tags: ["iocs"],
        response: {
          200: z.object({
            types: z.array(z.object({ value: z.string(), total: z.number() })),
            feeds: z.array(z.object({ value: z.string(), total: z.number() })),
            tags: z.array(z.object({ value: z.string(), total: z.number() })),
          }),
        },
      },
    },
    async () => {
      const [types, feeds, tags] = await Promise.all([
        fastify.db.execute<{ value: string; total: number }>(
          sql`select ioc_type::text as value, count(*)::int as total
                from iocs group by 1 order by 2 desc`,
        ),
        fastify.db.execute<{ value: string; total: number }>(
          sql`select feed as value, count(*)::int as total
                from iocs group by 1 order by 2 desc`,
        ),
        // Capped: the feeds carry thousands of distinct tags and a dropdown of all of them
        // is not a filter, it is a haystack.
        fastify.db.execute<{ value: string; total: number }>(
          sql`select tag as value, count(*)::int as total
                from iocs, unnest(coalesce(tags, '{}')) as tag
               group by 1 order by 2 desc limit 40`,
        ),
      ]);

      return { types: [...types], feeds: [...feeds], tags: [...tags] };
    },
  );
};
