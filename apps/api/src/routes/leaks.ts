import { leaks, sources } from "@leak/db";
import { and, asc, count, desc, eq, gte, lte, sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/** Hard ceiling. The old `/api/leaks` returned the entire collection with no limit at all. */
const MAX_LIMIT = 100;

const listQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(25),

  /** Filter by ransomware group slug. */
  group: z.string().min(1).max(100).optional(),
  status: z.enum(["published", "countdown", "sold", "removed", "unknown"]).optional(),
  sourceId: z.coerce.number().int().positive().optional(),

  /** Free-text search over victim name and domain, served by the GIN index. */
  q: z.string().min(1).max(200).optional(),

  /** Inclusive bounds on first_seen_at. */
  from: z.coerce.date().optional(),
  to: z.coerce.date().optional(),

  sort: z.enum(["first_seen_at", "published_at", "victim_name"]).default("first_seen_at"),
  order: z.enum(["asc", "desc"]).default("desc"),
});

const leakSchema = z.object({
  id: z.number(),
  dedupeHash: z.string(),
  victimName: z.string().nullable(),
  victimDomain: z.string().nullable(),
  victimCountry: z.string().nullable(),
  victimSector: z.string().nullable(),
  actorGroup: z.string(),
  sourceId: z.number().nullable(),
  sourceSlug: z.string().nullable(),
  sourceUrl: z.string().nullable(),
  publishedAt: z.date().nullable(),
  publishedAtRaw: z.string().nullable(),
  firstSeenAt: z.date(),
  lastSeenAt: z.date(),
  status: z.string(),
  leakType: z.string(),
  leakSizeBytes: z.number().nullable(),
});

const listResponse = z.object({
  data: z.array(leakSchema),
  pagination: z.object({
    page: z.number(),
    limit: z.number(),
    total: z.number(),
    totalPages: z.number(),
  }),
});

export const leakRoutes: FastifyPluginAsyncZod = async (fastify) => {
  // Nothing in here is public.
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/leaks",
    {
      schema: {
        description:
          "Paginated, filtered leak listing. Filtering and pagination happen in Postgres — " +
          "the client never receives more than `limit` rows.",
        tags: ["leaks"],
        querystring: listQuery,
        response: { 200: listResponse },
      },
    },
    async (request) => {
      const { page, limit, group, status, sourceId, q, from, to, sort, order } =
        request.query;

      const conditions: SQL[] = [];
      if (group) conditions.push(eq(leaks.actorGroup, group));
      if (status) conditions.push(eq(leaks.status, status));
      if (sourceId) conditions.push(eq(leaks.sourceId, sourceId));
      if (from) conditions.push(gte(leaks.firstSeenAt, from));
      if (to) conditions.push(lte(leaks.firstSeenAt, to));
      if (q) {
        // Mirrors the GIN index expression exactly, so this uses the index rather than
        // scanning. `plainto_tsquery` treats the input as literal words — there is no way
        // to inject query syntax through it.
        conditions.push(
          sql`to_tsvector('english', coalesce(${leaks.victimName}, '') || ' ' || coalesce(${leaks.victimDomain}, ''))
              @@ plainto_tsquery('english', ${q})`,
        );
      }

      const where = conditions.length > 0 ? and(...conditions) : undefined;

      const sortColumn =
        sort === "published_at"
          ? leaks.publishedAt
          : sort === "victim_name"
            ? leaks.victimName
            : leaks.firstSeenAt;

      const [rows, totalResult] = await Promise.all([
        fastify.db
          .select({
            id: leaks.id,
            dedupeHash: leaks.dedupeHash,
            victimName: leaks.victimName,
            victimDomain: leaks.victimDomain,
            victimCountry: leaks.victimCountry,
            victimSector: leaks.victimSector,
            actorGroup: leaks.actorGroup,
            sourceId: leaks.sourceId,
            sourceSlug: sources.slug,
            sourceUrl: leaks.sourceUrl,
            publishedAt: leaks.publishedAt,
            publishedAtRaw: leaks.publishedAtRaw,
            firstSeenAt: leaks.firstSeenAt,
            lastSeenAt: leaks.lastSeenAt,
            status: leaks.status,
            leakType: leaks.leakType,
            leakSizeBytes: leaks.leakSizeBytes,
          })
          .from(leaks)
          .leftJoin(sources, eq(leaks.sourceId, sources.id))
          .where(where)
          .orderBy(order === "asc" ? asc(sortColumn) : desc(sortColumn))
          .limit(limit)
          .offset((page - 1) * limit),

        fastify.db.select({ value: count() }).from(leaks).where(where),
      ]);

      const total = totalResult[0]?.value ?? 0;

      return {
        data: rows,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      };
    },
  );

  fastify.get(
    "/api/leaks/:id",
    {
      schema: {
        description: "A single leak by id.",
        tags: ["leaks"],
        params: z.object({ id: z.coerce.number().int().positive() }),
        response: {
          200: leakSchema,
          404: z.object({ error: z.string(), message: z.string() }),
        },
      },
    },
    async (request, reply) => {
      const rows = await fastify.db
        .select({
          id: leaks.id,
          dedupeHash: leaks.dedupeHash,
          victimName: leaks.victimName,
          victimDomain: leaks.victimDomain,
          victimCountry: leaks.victimCountry,
          victimSector: leaks.victimSector,
          actorGroup: leaks.actorGroup,
          sourceId: leaks.sourceId,
          sourceSlug: sources.slug,
          sourceUrl: leaks.sourceUrl,
          publishedAt: leaks.publishedAt,
          publishedAtRaw: leaks.publishedAtRaw,
          firstSeenAt: leaks.firstSeenAt,
          lastSeenAt: leaks.lastSeenAt,
          status: leaks.status,
          leakType: leaks.leakType,
          leakSizeBytes: leaks.leakSizeBytes,
        })
        .from(leaks)
        .leftJoin(sources, eq(leaks.sourceId, sources.id))
        .where(eq(leaks.id, request.params.id))
        .limit(1);

      const row = rows[0];
      if (!row) {
        return reply
          .status(404)
          .send({ error: "not_found", message: `No leak with id ${request.params.id}` });
      }
      return row;
    },
  );
};
