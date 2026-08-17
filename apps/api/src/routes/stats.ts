import { alertEvents, leaks, sources } from "@leak/db";
import { count, gte, sql } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Dashboard aggregates.
 *
 * These replace three things at once: the `/api/leaks-per-day` endpoint that always returned
 * an empty array (it compared a BSON date against a free-text field), and the two static
 * fixture files (`Data.json`, `top.json`) the charts silently fell back to.
 *
 * Everything here aggregates on `first_seen_at`, which is a real `timestamptz` with an index.
 */
export const statsRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/stats/leaks-per-day",
    {
      schema: {
        description: "Leak counts per day over a trailing window. Zero-filled.",
        tags: ["stats"],
        querystring: z.object({
          days: z.coerce.number().int().min(1).max(365).default(30),
        }),
        response: {
          200: z.object({
            days: z.number(),
            data: z.array(z.object({ date: z.string(), total: z.number() })),
          }),
        },
      },
    },
    async (request) => {
      const { days } = request.query;

      /**
       * generate_series zero-fills days with no leaks. Without it the chart draws a line
       * straight between two distant points and implies activity that never happened.
       */
      // NOTE: with the postgres-js driver `execute()` resolves to a RowList (an array),
      // not a `{ rows }` wrapper as it does under PGlite. Don't reach for `.rows` here.
      const rows = await fastify.db.execute<{ date: string; total: number }>(sql`
        with span as (
          select generate_series(
            date_trunc('day', now()) - make_interval(days => ${days - 1}),
            date_trunc('day', now()),
            interval '1 day'
          )::date as day
        )
        select
          to_char(span.day, 'YYYY-MM-DD') as date,
          coalesce(count(l.id), 0)::int as total
        from span
        left join leaks l
          on date_trunc('day', l.first_seen_at)::date = span.day
        group by span.day
        order by span.day
      `);

      return { days, data: [...rows] };
    },
  );

  fastify.get(
    "/api/stats/leaks-per-group",
    {
      schema: {
        description: "Leak counts per ransomware group, most active first.",
        tags: ["stats"],
        querystring: z.object({
          limit: z.coerce.number().int().min(1).max(50).default(10),
          days: z.coerce.number().int().min(1).max(365).optional(),
        }),
        response: {
          200: z.object({
            data: z.array(z.object({ group: z.string(), total: z.number() })),
          }),
        },
      },
    },
    async (request) => {
      const { limit, days } = request.query;

      const query = fastify.db
        .select({ group: leaks.actorGroup, total: count() })
        .from(leaks)
        .groupBy(leaks.actorGroup)
        .orderBy(sql`count(*) desc`)
        .limit(limit);

      const rows = days
        ? await query.where(
            gte(leaks.firstSeenAt, new Date(Date.now() - days * 86_400_000)),
          )
        : await query;

      return { data: rows };
    },
  );

  fastify.get(
    "/api/stats/summary",
    {
      schema: {
        description: "Headline counts for the dashboard tiles.",
        tags: ["stats"],
        response: {
          200: z.object({
            totalLeaks: z.number(),
            leaksLast7Days: z.number(),
            leaksLast30Days: z.number(),
            trackedGroups: z.number(),
            activeSources: z.number(),
            alertsTriggered: z.number(),
            /**
             * When collection last succeeded, and how many sources are currently failing.
             *
             * Every other number here counts leaks, which means a working crawler that
             * finds nothing new looks identical to a crawler that has stopped — and leak
             * sites publish in bursts, so "nothing new" is the normal state for hours at a
             * time. These two fields are the ones that actually answer "is it still
             * running?" without opening a terminal.
             */
            lastCollectionAt: z.date().nullable(),
            failingSources: z.number(),
          }),
        },
      },
    },
    async () => {
      const since = (days: number) => new Date(Date.now() - days * 86_400_000);

      const [total, last7, last30, groups, activeSources, triggered, collection] =
        await Promise.all([
        fastify.db.select({ value: count() }).from(leaks),
        fastify.db
          .select({ value: count() })
          .from(leaks)
          .where(gte(leaks.firstSeenAt, since(7))),
        fastify.db
          .select({ value: count() })
          .from(leaks)
          .where(gte(leaks.firstSeenAt, since(30))),
        fastify.db.execute<{ value: number }>(
          sql`select count(distinct actor_group)::int as value from leaks`,
        ),
        fastify.db
          .select({ value: count() })
          .from(sources)
          .where(sql`${sources.enabled} = true`),
        // The old dashboard read this from a collection nothing ever wrote to, so it was
        // permanently zero. Now it counts real deliveries.
        fastify.db.select({ value: count() }).from(alertEvents),
        // Most recent successful crawl of any enabled source, plus how many are failing.
        //
        // `execute` runs raw SQL, which bypasses Drizzle's column decoding — the driver is
        // configured to hand timestamps back as strings and let Drizzle map them, so a
        // timestamptz selected this way arrives as a string, not a Date. The count above
        // gets away with it because `::int` is already a number. Coerced below rather than
        // loosening the response schema, which would just push the ambiguity to callers.
        fastify.db.execute<{ last_success: string | Date | null; failing: number }>(
          sql`select max(last_success_at) as last_success,
                     count(*) filter (where consecutive_failures >= 3)::int as failing
                from sources where enabled`,
        ),
      ]);

      const health = (
        collection as unknown as { last_success: string | Date | null; failing: number }[]
      )[0];
      const lastCollectionAt = health?.last_success ? new Date(health.last_success) : null;

      return {
        totalLeaks: total[0]?.value ?? 0,
        leaksLast7Days: last7[0]?.value ?? 0,
        leaksLast30Days: last30[0]?.value ?? 0,
        trackedGroups: groups[0]?.value ?? 0,
        activeSources: activeSources[0]?.value ?? 0,
        alertsTriggered: triggered[0]?.value ?? 0,
        lastCollectionAt,
        failingSources: health?.failing ?? 0,
      };
    },
  );
};
