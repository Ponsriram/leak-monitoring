import { leaks, sources } from "@leak/db";
import { asc, count, eq, sql } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Monitored sources and their health.
 *
 * This backs the "Ransomware Groups Index" page, which in the old app was ten hardcoded rows
 * with invented "last seen 2 hours ago" values.
 */
export const sourceRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/sources",
    {
      schema: {
        description: "All monitored sources with crawl health and leak counts.",
        tags: ["sources"],
        querystring: z.object({
          enabled: z
            .enum(["true", "false"])
            .optional()
            .transform((v) => (v === undefined ? undefined : v === "true")),
        }),
        response: {
          200: z.object({
            data: z.array(
              z.object({
                id: z.number(),
                slug: z.string(),
                name: z.string(),
                baseUrl: z.string(),
                collector: z.string(),
                enabled: z.boolean(),
                crawlIntervalSeconds: z.number(),
                lastCrawlAt: z.date().nullable(),
                lastSuccessAt: z.date().nullable(),
                consecutiveFailures: z.number(),
                leakCount: z.number(),
                /** Derived, so the UI doesn't reimplement the rule in three places. */
                health: z.enum(["healthy", "degraded", "failing", "disabled"]),
              }),
            ),
          }),
        },
      },
    },
    async (request) => {
      const rows = await fastify.db
        .select({
          id: sources.id,
          slug: sources.slug,
          name: sources.name,
          baseUrl: sources.baseUrl,
          collector: sources.collector,
          enabled: sources.enabled,
          crawlIntervalSeconds: sources.crawlIntervalSeconds,
          lastCrawlAt: sources.lastCrawlAt,
          lastSuccessAt: sources.lastSuccessAt,
          consecutiveFailures: sources.consecutiveFailures,
          /**
           * Use `$count`, not a hand-written subquery.
           *
           * Interpolating a column into a `sql` template emits an UNQUALIFIED identifier:
           * `... where leaks.source_id = "id"`. Inside a subquery over `leaks` — which has
           * its own `id` — that binds to `leaks.id`, silently making the predicate
           * `leaks.source_id = leaks.id`. It returns plausible small numbers rather than
           * failing, so it reads as correct. `$count` emits
           * `"leaks"."source_id" = "sources"."id"` and correlates properly.
           */
          leakCount: fastify.db.$count(leaks, eq(leaks.sourceId, sources.id)),
        })
        .from(sources)
        .where(
          request.query.enabled === undefined
            ? undefined
            : eq(sources.enabled, request.query.enabled),
        )
        .orderBy(asc(sources.name));

      return {
        data: rows.map((row) => ({
          ...row,
          health: !row.enabled
            ? ("disabled" as const)
            : row.consecutiveFailures === 0
              ? ("healthy" as const)
              : row.consecutiveFailures < 3
                ? ("degraded" as const)
                : ("failing" as const),
        })),
      };
    },
  );

  fastify.get(
    "/api/sources/stats",
    {
      schema: {
        description: "Counts by health state, for the dashboard tile.",
        tags: ["sources"],
        response: {
          200: z.object({
            total: z.number(),
            enabled: z.number(),
            failing: z.number(),
          }),
        },
      },
    },
    async () => {
      const [total, enabled, failing] = await Promise.all([
        fastify.db.select({ value: count() }).from(sources),
        fastify.db
          .select({ value: count() })
          .from(sources)
          .where(eq(sources.enabled, true)),
        fastify.db
          .select({ value: count() })
          .from(sources)
          .where(sql`${sources.consecutiveFailures} >= 3 and ${sources.enabled} = true`),
      ]);

      return {
        total: total[0]?.value ?? 0,
        enabled: enabled[0]?.value ?? 0,
        failing: failing[0]?.value ?? 0,
      };
    },
  );
};
