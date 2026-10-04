import { watchlistEntries } from "@leak/db";
import { sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { MAX_KEYWORD_LENGTH, MIN_KEYWORD_LENGTH, normalizeWatchValue } from "../lib/watch.js";
import { requireAuth } from "../plugins/auth.js";

/**
 * The watchlist: what an organisation has asked to be told about, and what has turned up.
 *
 * This route only records the question. It never looks for the answer — matching is done by
 * the worker (`Storage.match_watchlist`), which sees a new entry on its next tick and matches
 * it against all of history. The same handoff as `crawl_requests` and `hunt_jobs`, and for the
 * same reason: the worker owns the work, the API owns the session and the HTTP surface.
 * `pending` on an entry is how a client tells "no matches" from "not looked yet".
 */

const MAX_LIMIT = 100;

const watchKind = z.enum(["domain", "keyword"]);
const targetType = z.enum(["leak", "ioc", "exposure"]);

/*
 * The list queries below are raw SQL, and the driver hands raw rows back with `bigint` ids and
 * timestamps as strings (it only converts for the query builder). Those fields are coerced
 * here, in the response schema, rather than cast in every query — and they were missed once:
 * a check against an in-process Postgres returns native numbers and dates, which hid it.
 */
const entryRow = z.object({
  id: z.coerce.number(),
  kind: watchKind,
  value: z.string(),
  label: z.string().nullable(),
  createdAt: z.coerce.date(),
  /** True until the worker has matched this entry against history for the first time. */
  pending: z.boolean(),
  matchCount: z.number(),
  unreadCount: z.number(),
});

const matchRow = z.object({
  id: z.coerce.number(),
  entryId: z.coerce.number(),
  entryKind: watchKind,
  entryValue: z.string(),
  entryLabel: z.string().nullable(),
  targetType,
  targetId: z.coerce.number(),
  /** What matched: a victim, an indicator, or a masked exposure. */
  title: z.string().nullable(),
  /** One line of context — the crew and status, the feed, the detector and confidence. */
  detail: z.string().nullable(),
  matchedAt: z.coerce.date(),
  acknowledgedAt: z.coerce.date().nullable(),
});

const createBody = z.object({
  kind: watchKind,
  value: z.string().min(1).max(300),
  label: z.string().trim().max(120).optional(),
});

const matchesQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(20),
  entryId: z.coerce.number().int().min(1).optional(),
  targetType: targetType.optional(),
  /** Only matches nobody has acknowledged yet. */
  unread: z
    .enum(["true", "false"])
    .transform((value) => value === "true")
    .optional(),
});

const idParams = z.object({ id: z.coerce.number().int().min(1) });

export const watchlistRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/watchlist",
    {
      schema: {
        description: "Watch entries with how many matches each has, and how many are unread.",
        tags: ["watchlist"],
        response: {
          200: z.object({ data: z.array(entryRow), unreadTotal: z.number() }),
        },
      },
    },
    async () => {
      const rows = await fastify.db.execute<z.infer<typeof entryRow>>(
        sql`select e.id, e.kind::text as kind, e.value, e.label,
                   e.created_at as "createdAt",
                   (e.matched_through is null) as pending,
                   count(m.id)::int as "matchCount",
                   (count(m.id) filter (where m.acknowledged_at is null))::int as "unreadCount"
              from watchlist_entries e
              left join watchlist_matches m on m.entry_id = e.id
             group by e.id
             order by e.created_at desc, e.id desc`,
      );
      const data = [...rows];
      return { data, unreadTotal: data.reduce((sum, row) => sum + row.unreadCount, 0) };
    },
  );

  fastify.post(
    "/api/watchlist",
    {
      schema: {
        description: "Add a domain or keyword to the watchlist.",
        tags: ["watchlist"],
        body: createBody,
        response: { 201: entryRow },
      },
    },
    async (request, reply) => {
      const { kind, value: raw, label } = request.body;

      const value = normalizeWatchValue(kind, raw);
      if (!value) {
        throw fastify.httpErrors.badRequest(
          kind === "domain"
            ? "That does not look like a domain. Try acme.com — a URL or an email address works too."
            : `A keyword must be ${MIN_KEYWORD_LENGTH}-${MAX_KEYWORD_LENGTH} characters and contain a letter or digit.`,
        );
      }

      const [row] = await fastify.db
        .insert(watchlistEntries)
        .values({
          kind,
          value,
          label: label || null,
          createdBy: request.currentUser?.id ?? null,
        })
        .onConflictDoNothing()
        .returning();

      if (!row) {
        throw fastify.httpErrors.conflict(`"${value}" is already on the watchlist.`);
      }

      reply.status(201);
      return {
        id: row.id,
        kind: row.kind,
        value: row.value,
        label: row.label,
        createdAt: row.createdAt,
        pending: true,
        matchCount: 0,
        unreadCount: 0,
      };
    },
  );

  fastify.delete(
    "/api/watchlist/:id",
    {
      schema: {
        description: "Remove a watch entry and its matches.",
        tags: ["watchlist"],
        params: idParams,
        response: { 204: z.null() },
      },
    },
    async (request, reply) => {
      const deleted = await fastify.db.execute<{ id: number }>(
        sql`delete from watchlist_entries where id = ${request.params.id} returning id`,
      );
      if (deleted.length === 0) throw fastify.httpErrors.notFound("No such watch entry.");
      reply.status(204);
      return null;
    },
  );

  fastify.get(
    "/api/watchlist/matches",
    {
      schema: {
        description: "What the watchlist has matched, newest first.",
        tags: ["watchlist"],
        querystring: matchesQuery,
        response: {
          200: z.object({
            data: z.array(matchRow),
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
      const { page, limit, entryId, targetType: target, unread } = request.query;

      // Every filter is on `watchlist_matches` itself, so the count needs no joins.
      const conditions: SQL[] = [];
      if (entryId) conditions.push(sql`m.entry_id = ${entryId}`);
      if (target) conditions.push(sql`m.target_type = ${target}`);
      if (unread === true) conditions.push(sql`m.acknowledged_at is null`);
      if (unread === false) conditions.push(sql`m.acknowledged_at is not null`);
      const where =
        conditions.length > 0 ? sql`where ${sql.join(conditions, sql` and `)}` : sql``;

      const [rows, totals] = await Promise.all([
        fastify.db.execute<z.infer<typeof matchRow>>(
          sql`select m.id,
                     m.entry_id as "entryId",
                     e.kind::text as "entryKind",
                     e.value as "entryValue",
                     e.label as "entryLabel",
                     m.target_type::text as "targetType",
                     m.target_id as "targetId",
                     case m.target_type
                       when 'leak' then coalesce(l.victim_name, l.victim_domain, 'unnamed victim')
                       when 'ioc' then i.value
                       when 'exposure' then x.preview
                     end as title,
                     case m.target_type
                       when 'leak' then l.actor_group || ' · ' || l.status::text
                       when 'ioc' then i.feed || coalesce(' · ' || i.threat, '')
                       when 'exposure' then x.kind::text || ' · ' || x.detector
                                            || ' · confidence ' || x.confidence
                     end as detail,
                     m.matched_at as "matchedAt",
                     m.acknowledged_at as "acknowledgedAt"
                from watchlist_matches m
                join watchlist_entries e on e.id = m.entry_id
                left join leaks l on m.target_type = 'leak' and l.id = m.target_id
                left join iocs i on m.target_type = 'ioc' and i.id = m.target_id
                left join exposures x on m.target_type = 'exposure' and x.id = m.target_id
                ${where}
               order by m.matched_at desc, m.id desc
               limit ${limit} offset ${(page - 1) * limit}`,
        ),
        fastify.db.execute<{ total: number }>(
          sql`select count(*)::int as total from watchlist_matches m ${where}`,
        ),
      ]);

      const total = totals[0]?.total ?? 0;
      return {
        data: [...rows],
        pagination: { page, limit, total, totalPages: Math.ceil(total / limit) },
      };
    },
  );

  fastify.post(
    "/api/watchlist/matches/:id/acknowledge",
    {
      schema: {
        description: "Mark one match as seen.",
        tags: ["watchlist"],
        params: idParams,
        response: { 204: z.null() },
      },
    },
    async (request, reply) => {
      const { id } = request.params;
      const changed = await fastify.db.execute<{ id: number }>(
        sql`update watchlist_matches
               set acknowledged_at = now(), acknowledged_by = ${request.currentUser?.id ?? null}
             where id = ${id} and acknowledged_at is null
         returning id`,
      );
      if (changed.length === 0) {
        // Acknowledging twice is not an error, but acknowledging nothing is.
        const exists = await fastify.db.execute<{ id: number }>(
          sql`select id from watchlist_matches where id = ${id}`,
        );
        if (exists.length === 0) throw fastify.httpErrors.notFound("No such match.");
      }
      reply.status(204);
      return null;
    },
  );

  fastify.post(
    "/api/watchlist/matches/acknowledge-all",
    {
      schema: {
        description: "Mark every unread match as seen, optionally for one entry.",
        tags: ["watchlist"],
        body: z.object({ entryId: z.number().int().min(1).optional() }),
        response: { 200: z.object({ acknowledged: z.number() }) },
      },
    },
    async (request) => {
      const { entryId } = request.body;
      const changed = await fastify.db.execute<{ id: number }>(
        sql`update watchlist_matches
               set acknowledged_at = now(), acknowledged_by = ${request.currentUser?.id ?? null}
             where acknowledged_at is null
               ${entryId ? sql`and entry_id = ${entryId}` : sql``}
         returning id`,
      );
      return { acknowledged: changed.length };
    },
  );
};
