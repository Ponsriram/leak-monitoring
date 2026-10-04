import { exposures, sources } from "@leak/db";
import { and, count, desc, eq, gte, sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Credentials, keys and card numbers the worker found inside crawled pages.
 *
 * What is returned is exactly what is stored: a masked `preview` and the organisation part of
 * an email. There is no secret in the database, so there is none for this route to leak — see
 * `packages/db/src/schema/exposures.ts` and `services/intel/intel/extract/secrets.py`.
 */

const MAX_LIMIT = 100;

const kindEnum = z.enum([
  "credential_pair",
  "password_hash",
  "api_key",
  "payment_card",
  "private_key",
]);

const listQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(20),
  kind: kindEnum.optional(),
  /** Findings at or above this score. The detector's confidence is a ranking, not a probability. */
  minConfidence: z.coerce.number().int().min(0).max(100).default(0),
  /** An organisation: matches the exposed email's domain exactly or as a parent of it. */
  domain: z.string().min(3).max(253).optional(),
  /** Only findings some watchlist entry has matched. */
  watched: z
    .enum(["true", "false"])
    .transform((value) => value === "true")
    .optional(),
  /** Words in the preview, the detector name, or the domain. */
  q: z.string().min(1).max(200).optional(),
});

const exposureRow = z.object({
  id: z.number(),
  kind: kindEnum,
  detector: z.string(),
  preview: z.string(),
  emailDomain: z.string().nullable(),
  confidence: z.number(),
  sourceSlug: z.string().nullable(),
  sourceUrl: z.string().nullable(),
  firstSeenAt: z.date(),
  lastSeenAt: z.date(),
  /** How many watchlist entries have matched this finding. */
  watchCount: z.number(),
});

const facet = z.array(z.object({ value: z.string(), total: z.number() }));

export const exposureRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/exposures",
    {
      schema: {
        description: "Credentials, keys and card numbers found in crawled pages, newest first.",
        tags: ["exposures"],
        querystring: listQuery,
        response: {
          200: z.object({
            data: z.array(exposureRow),
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
      const { page, limit, kind, minConfidence, domain, watched, q } = request.query;

      const watchedExists = sql`exists (
        select 1 from watchlist_matches w
         where w.target_type = 'exposure' and w.target_id = ${exposures.id}
      )`;

      const conditions: SQL[] = [];
      if (kind) conditions.push(eq(exposures.kind, kind));
      if (minConfidence > 0) conditions.push(gte(exposures.confidence, minConfidence));
      if (domain) {
        const value = domain.trim().toLowerCase();
        conditions.push(
          sql`(${exposures.emailDomain} = ${value} or ${exposures.emailDomain} like ${`%.${value}`})`,
        );
      }
      if (watched === true) conditions.push(watchedExists);
      if (watched === false) conditions.push(sql`not ${watchedExists}`);
      if (q) {
        const pattern = `%${q}%`;
        conditions.push(
          sql`(${exposures.preview} ilike ${pattern} or ${exposures.detector} ilike ${pattern}
               or ${exposures.emailDomain} ilike ${pattern})`,
        );
      }
      const where = conditions.length > 0 ? and(...conditions) : undefined;

      const [rows, totalResult] = await Promise.all([
        fastify.db
          .select({
            id: exposures.id,
            kind: exposures.kind,
            detector: exposures.detector,
            preview: exposures.preview,
            emailDomain: exposures.emailDomain,
            confidence: exposures.confidence,
            sourceSlug: sources.slug,
            sourceUrl: exposures.sourceUrl,
            firstSeenAt: exposures.firstSeenAt,
            lastSeenAt: exposures.lastSeenAt,
            watchCount: sql<number>`(
              select count(*)::int from watchlist_matches w
               where w.target_type = 'exposure' and w.target_id = ${exposures.id}
            )`,
          })
          .from(exposures)
          .leftJoin(sources, eq(sources.id, exposures.sourceId))
          .where(where)
          .orderBy(desc(exposures.firstSeenAt), desc(exposures.id))
          .limit(limit)
          .offset((page - 1) * limit),

        fastify.db.select({ value: count() }).from(exposures).where(where),
      ]);

      const total = totalResult[0]?.value ?? 0;
      return {
        data: rows,
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      };
    },
  );

  /** The filter dropdowns, from the whole table rather than the page on screen. */
  fastify.get(
    "/api/exposures/facets",
    {
      schema: {
        description: "Exposure kinds and detectors, with counts.",
        tags: ["exposures"],
        response: { 200: z.object({ kinds: facet, detectors: facet }) },
      },
    },
    async () => {
      const [kinds, detectors] = await Promise.all([
        fastify.db.execute<{ value: string; total: number }>(
          sql`select kind::text as value, count(*)::int as total
                from exposures group by 1 order by 2 desc`,
        ),
        fastify.db.execute<{ value: string; total: number }>(
          sql`select detector as value, count(*)::int as total
                from exposures group by 1 order by 2 desc`,
        ),
      ]);
      return { kinds: [...kinds], detectors: [...detectors] };
    },
  );
};
