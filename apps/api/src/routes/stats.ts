import { leaks, sources } from "@leak/db";
import { count, gte, isNotNull, sql } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Dashboard aggregates.
 *
 * Everything here aggregates on `first_seen_at`, which is a real `timestamptz` with an index.
 */

/**
 * Accepts an IANA zone name only ("Asia/Kolkata", "UTC"), never a bare offset.
 *
 * `AT TIME ZONE '+05:30'` is accepted by Postgres but its sign convention for offset
 * literals is the POSIX one — the opposite of what the caller means — so an offset that
 * parses would silently shift buckets the wrong way by twice the offset. Names carry DST
 * rules too, which offsets cannot.
 */
const IANA_NAME = /^[A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z0-9_+-]+){0,2}$/;

function isTimeZone(value: string): boolean {
  if (!IANA_NAME.test(value)) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/**
 * The zone the day boundaries are drawn in. Defaults to UTC so a caller that says nothing
 * gets the old behaviour; the dashboard sends the browser's zone.
 */
const timeZoneParam = z
  .string()
  .max(64)
  .refine(isTimeZone, "must be an IANA time zone name, e.g. Asia/Kolkata")
  .default("UTC");

export const statsRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/stats/leaks-per-day",
    {
      schema: {
        description:
          "Leak counts per day over a trailing window, bucketed in `tz`. Zero-filled.",
        tags: ["stats"],
        querystring: z.object({
          days: z.coerce.number().int().min(1).max(365).default(30),
          tz: timeZoneParam,
        }),
        response: {
          200: z.object({
            days: z.number(),
            timeZone: z.string(),
            data: z.array(z.object({ date: z.string(), total: z.number() })),
          }),
        },
      },
    },
    async (request) => {
      const { days, tz } = request.query;

      /**
       * Day boundaries are drawn in the caller's zone, not the server's.
       *
       * `first_seen_at` is a timestamptz and the database session runs in UTC, so
       * truncating it directly bucketed by UTC day while every date the UI prints beside
       * it is formatted in the browser's zone. East of Greenwich that puts the evening's
       * arrivals on the previous bar and leaves the chart a day behind the leak table —
       * the same rows, two different dates.
       *
       * The join compares the indexed column against an instant range rather than
       * wrapping it in date_trunc, so `leaks_first_seen_at_idx` is still usable.
       */
      // NOTE: with the postgres-js driver `execute()` resolves to a RowList (an array),
      // not a `{ rows }` wrapper as it does under PGlite. Don't reach for `.rows` here.
      /**
       * generate_series zero-fills days with no leaks. Without it the chart draws a line
       * straight between two distant points and implies activity that never happened.
       */
      const rows = await fastify.db.execute<{ date: string; total: number }>(sql`
        with span as (
          select generate_series(
            date_trunc('day', now() at time zone ${tz}::text)
              - make_interval(days => ${days - 1}),
            date_trunc('day', now() at time zone ${tz}::text),
            interval '1 day'
          )::date as day
        )
        select
          to_char(span.day, 'YYYY-MM-DD') as date,
          coalesce(count(l.id), 0)::int as total
        from span
        left join leaks l
          on l.first_seen_at >= span.day::timestamp at time zone ${tz}::text
         and l.first_seen_at < (span.day::timestamp + interval '1 day')
               at time zone ${tz}::text
        group by span.day
        order by span.day
      `);

      return { days, timeZone: tz, data: [...rows] };
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

  /**
   * The NER tag facets, used to build the Leaks page's Country and Sector filters.
   *
   * One endpoint for both, because they answer the same question about two columns and the
   * shape of the response is identical — the alternative was two near-copies of the same
   * handler. Nulls are excluded rather than bucketed as "unknown": these columns are null
   * whenever a listing named no country and its domain carried no ccTLD, which is a large
   * share of rows, and a filter option that means "we could not tell" is not a filter.
   */
  fastify.get(
    "/api/stats/leaks-per-tag",
    {
      schema: {
        description:
          "Leak counts per extracted country or sector, or per incident type, most common first.",
        tags: ["stats"],
        querystring: z.object({
          tag: z.enum(["country", "sector", "type"]),
          limit: z.coerce.number().int().min(1).max(100).default(30),
        }),
        response: {
          200: z.object({
            tag: z.enum(["country", "sector", "type"]),
            data: z.array(z.object({ value: z.string(), total: z.number() })),
          }),
        },
      },
    },
    async (request) => {
      const { tag, limit } = request.query;

      // Types are multi-valued, so a row counts once under each type it carries — which is
      // what the incident-type filter it feeds will return for that value.
      if (tag === "type") {
        const rows = await fastify.db.execute<{ value: string; total: number }>(sql`
          select t as value, count(*)::int as total
            from ${leaks}, unnest(${leaks.incidentTypes}) as t
           group by t
           order by count(*) desc
           limit ${limit}
        `);
        return { tag, data: [...rows] };
      }

      const column = tag === "country" ? leaks.victimCountry : leaks.victimSector;

      const rows = await fastify.db
        .select({ value: column, total: count() })
        .from(leaks)
        .where(isNotNull(column))
        .groupBy(column)
        .orderBy(sql`count(*) desc`)
        .limit(limit);

      // `isNotNull` already excludes them, so the cast is narrowing a type the query has
      // already guaranteed rather than papering over a possible null.
      return { tag, data: rows as { value: string; total: number }[] };
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

      const [total, last7, last30, groups, activeSources, collection] =
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
        lastCollectionAt,
        failingSources: health?.failing ?? 0,
      };
    },
  );
};
