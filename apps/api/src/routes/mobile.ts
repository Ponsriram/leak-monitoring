import { mobileNumbers } from "@leak/db";
import { and, count, desc, eq, sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Bulk Intelligence · Mobile Number — phone numbers reported as used in scams.
 *
 * One row per report (a number in a public post). The worker finds the numbers with a regex
 * and validates them with libphonenumber; see `services/intel/intel/extract/phones.py`.
 *
 * `details` is returned whole. It is the full text of the report, and the section exists to
 * show it — nothing here shortens it.
 */

const MAX_LIMIT = 100;

const listQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(20),
  threatType: z.string().min(1).max(60).optional(),
  audience: z.string().min(1).max(80).optional(),
  source: z.string().min(1).max(40).optional(),
  /**
   * A number, a fragment of one, or words from the report.
   *
   * Matched against the stored number with the query's own digits — so "0208 540 2764",
   * "208-540" and "+442085402764" all find the same row — and against the report text.
   */
  q: z.string().min(1).max(200).optional(),
});

const mobileRow = z.object({
  id: z.number(),
  number: z.string(),
  numberDisplay: z.string(),
  numberRaw: z.string(),
  regionCode: z.string().nullable(),
  country: z.string().nullable(),
  lineType: z.string().nullable(),
  threatTypes: z.array(z.string()),
  targetAudience: z.array(z.string()),
  details: z.string(),
  source: z.string(),
  sourceUrl: z.string(),
  author: z.string().nullable(),
  reportedAt: z.date().nullable(),
  firstSeenAt: z.date(),
  /** How many reports in the table name this same number, this one included. */
  reportCount: z.number(),
});

const facet = z.array(z.object({ value: z.string(), total: z.number() }));

export const mobileRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/mobile-numbers",
    {
      schema: {
        description: "Scam phone-number reports from public posts, newest first.",
        tags: ["mobile"],
        querystring: listQuery,
        response: {
          200: z.object({
            data: z.array(mobileRow),
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
      const { page, limit, threatType, audience, source, q } = request.query;

      const conditions: SQL[] = [];
      // Array containment, so the GIN indexes are usable.
      if (threatType) conditions.push(sql`${mobileNumbers.threatTypes} @> array[${threatType}]::text[]`);
      if (audience) conditions.push(sql`${mobileNumbers.targetAudience} @> array[${audience}]::text[]`);
      if (source) conditions.push(eq(mobileNumbers.source, source));
      if (q) {
        const digits = q.replace(/\D/g, "");
        const arms: SQL[] = [sql`${mobileNumbers.details} ilike ${`%${q}%`}`];
        // A query of four or more digits is a number search. Stored numbers are E.164, so a
        // national "0208…" is compared without its trunk 0 as well as with it.
        if (digits.length >= 4) {
          arms.push(sql`${mobileNumbers.number} like ${`%${digits}%`}`);
          if (digits.startsWith("0")) {
            arms.push(sql`${mobileNumbers.number} like ${`%${digits.replace(/^0+/, "")}%`}`);
          }
        }
        conditions.push(sql`(${sql.join(arms, sql` or `)})`);
      }
      const where = conditions.length > 0 ? and(...conditions) : undefined;

      const [rows, totalResult] = await Promise.all([
        fastify.db
          .select({
            id: mobileNumbers.id,
            number: mobileNumbers.number,
            numberDisplay: mobileNumbers.numberDisplay,
            numberRaw: mobileNumbers.numberRaw,
            regionCode: mobileNumbers.regionCode,
            country: mobileNumbers.country,
            lineType: mobileNumbers.lineType,
            threatTypes: mobileNumbers.threatTypes,
            targetAudience: mobileNumbers.targetAudience,
            details: mobileNumbers.details,
            source: mobileNumbers.source,
            sourceUrl: mobileNumbers.sourceUrl,
            author: mobileNumbers.author,
            reportedAt: mobileNumbers.reportedAt,
            firstSeenAt: mobileNumbers.firstSeenAt,
            reportCount: sql<number>`(
              select count(*)::int from mobile_numbers m2
               where m2.number = ${mobileNumbers.number}
            )`,
          })
          .from(mobileNumbers)
          .where(where)
          .orderBy(
            sql`coalesce(${mobileNumbers.reportedAt}, ${mobileNumbers.firstSeenAt}) desc`,
            desc(mobileNumbers.id),
          )
          .limit(limit)
          .offset((page - 1) * limit),

        fastify.db.select({ value: count() }).from(mobileNumbers).where(where),
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
    "/api/mobile-numbers/facets",
    {
      schema: {
        description: "Threat types, target audiences and sources, with report counts.",
        tags: ["mobile"],
        response: {
          200: z.object({
            threatTypes: facet,
            audiences: facet,
            sources: facet,
          }),
        },
      },
    },
    async () => {
      const [threatTypes, audiences, sources] = await Promise.all([
        fastify.db.execute<{ value: string; total: number }>(
          sql`select t as value, count(*)::int as total
                from mobile_numbers, unnest(threat_types) as t
               group by 1 order by 2 desc`,
        ),
        fastify.db.execute<{ value: string; total: number }>(
          sql`select a as value, count(*)::int as total
                from mobile_numbers, unnest(target_audience) as a
               group by 1 order by 2 desc`,
        ),
        fastify.db.execute<{ value: string; total: number }>(
          sql`select source as value, count(*)::int as total
                from mobile_numbers group by 1 order by 2 desc`,
        ),
      ]);
      return { threatTypes: [...threatTypes], audiences: [...audiences], sources: [...sources] };
    },
  );
};
