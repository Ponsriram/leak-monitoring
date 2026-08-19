import { crawlRequests, crawlRuns, sources } from "@leak/db";
import { and, count, desc, eq, gt, inArray } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * On-demand collection: "sync now", and the status of the last sync.
 *
 * The API does not run crawls and does not talk to Tor — the Python worker owns both, along
 * with the advisory lock that keeps two crawls off one Tor daemon. What the API can do is
 * write a row the worker is already watching for. `crawl_requests` is that row, and its
 * lifecycle (queued → running → succeeded) is what the Leaks page polls so a Sync button
 * can say something truthful instead of just spinning.
 *
 * Deliberately not an arq enqueue: arq pickles its job payloads, so producing one from
 * TypeScript would mean writing a Python pickle encoder here and keeping it in step with a
 * library this service does not depend on.
 */

/** Statuses that mean "this request has not finished yet". */
const OPEN_STATUSES = ["queued", "running"] as const;

/**
 * How long a `crawl_runs` row may sit at `running` before it is treated as abandoned.
 *
 * A worker killed hard — `docker compose down`, an OOM — leaves its in-flight rows at
 * `running` forever; `finish_crawl` is shielded against cancellation but not against the
 * process disappearing. Two such rows from a crash on 2026-08-17 were still marking
 * collection "in progress" a day later, which through this endpoint would have kept the
 * Sync button disabled permanently: the one control whose entire job is to recover from a
 * sync that isn't happening, disabled because a sync appeared to be happening.
 *
 * Matches the worker's `CRAWL_JOB_TIMEOUT` default. A real crawl cannot outlive it, because
 * arq cancels the job at exactly that point.
 */
const RUN_STALE_AFTER_MS = 60 * 60_000;

const requestSchema = z.object({
  id: z.number(),
  sourceSlug: z.string().nullable(),
  status: z.enum(["queued", "running", "succeeded", "failed", "skipped"]),
  requestedAt: z.date(),
  startedAt: z.date().nullable(),
  finishedAt: z.date().nullable(),
  sourcesCrawled: z.number(),
  newLeaks: z.number(),
  updatedLeaks: z.number(),
  failedSources: z.number(),
  error: z.string().nullable(),
});

const statusResponse = z.object({
  /** The most recent request, or null if nobody has ever asked for one. */
  latest: requestSchema.nullable(),
  /**
   * Whether collection is happening right now — from `crawl_runs`, not from the request.
   * The scheduled sweep produces crawl runs with no request behind them, and the UI should
   * say "syncing" for those too rather than claiming the system is idle.
   */
  running: z.boolean(),
  queued: z.number(),
});

export const crawlRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.post(
    "/api/crawl",
    {
      config: {
        // Far tighter than the global 300/min. Each of these can put a source under a
        // crawl, and a held button should not be able to queue a hundred of them.
        rateLimit: { max: 10, timeWindow: "1 minute" },
      },
      schema: {
        description:
          "Queue a collection run. Returns the existing request if one is already " +
          "queued or running, rather than stacking a second crawl behind it.",
        tags: ["crawl"],
        body: z.object({
          /** Omit to crawl every enabled source. */
          sourceSlug: z.string().min(1).max(100).optional(),
        }),
        response: {
          200: z.object({ request: requestSchema, created: z.boolean() }),
          404: z.object({ error: z.string(), message: z.string() }),
        },
      },
    },
    async (request, reply) => {
      const { sourceSlug } = request.body;

      if (sourceSlug) {
        const known = await fastify.db
          .select({ id: sources.id })
          .from(sources)
          .where(eq(sources.slug, sourceSlug))
          .limit(1);
        if (known.length === 0) {
          return reply
            .status(404)
            .send({ error: "not_found", message: `No source ${sourceSlug}` });
        }
      }

      /**
       * Collapse onto the request already in flight.
       *
       * A crawl is minutes of Tor traffic against the same sources whoever clicks it, so
       * three impatient clicks must not become three crawls queued back to back — the
       * second and third would refetch pages the first had just hashed. Returning the open
       * request also means the UI polls one id and sees it through.
       */
      const open = await fastify.db
        .select()
        .from(crawlRequests)
        .where(inArray(crawlRequests.status, [...OPEN_STATUSES]))
        .orderBy(desc(crawlRequests.requestedAt))
        .limit(1);

      if (open[0]) return { request: open[0], created: false };

      const inserted = await fastify.db
        .insert(crawlRequests)
        .values({
          sourceSlug: sourceSlug ?? null,
          requestedBy: request.currentUser?.id ?? null,
        })
        .returning();

      const row = inserted[0];
      if (!row) {
        // `returning()` on a successful single-row insert always yields a row; this is here
        // so the type is honest rather than asserted away.
        throw new Error("crawl request insert returned no row");
      }

      request.log.info(
        { requestId: row.id, sourceSlug: sourceSlug ?? "all" },
        "crawl requested",
      );
      return { request: row, created: true };
    },
  );

  fastify.get(
    "/api/crawl/status",
    {
      schema: {
        description:
          "The most recent collection request and whether collection is running now. " +
          "Polled by the Leaks page while a sync is in flight.",
        tags: ["crawl"],
        response: { 200: statusResponse },
      },
    },
    async () => {
      const [latest, active, queued] = await Promise.all([
        fastify.db
          .select()
          .from(crawlRequests)
          .orderBy(desc(crawlRequests.requestedAt))
          .limit(1),
        fastify.db
          .select({ value: count() })
          .from(crawlRuns)
          .where(
            and(
              eq(crawlRuns.status, "running"),
              gt(crawlRuns.startedAt, new Date(Date.now() - RUN_STALE_AFTER_MS)),
            ),
          ),
        fastify.db
          .select({ value: count() })
          .from(crawlRequests)
          .where(eq(crawlRequests.status, "queued")),
      ]);

      return {
        latest: latest[0] ?? null,
        running: (active[0]?.value ?? 0) > 0,
        queued: queued[0]?.value ?? 0,
      };
    },
  );

  fastify.get(
    "/api/crawl/requests",
    {
      schema: {
        description: "Recent collection requests, newest first.",
        tags: ["crawl"],
        querystring: z.object({
          limit: z.coerce.number().int().min(1).max(50).default(10),
        }),
        response: { 200: z.object({ data: z.array(requestSchema) }) },
      },
    },
    async (request) => {
      const rows = await fastify.db
        .select()
        .from(crawlRequests)
        .orderBy(desc(crawlRequests.requestedAt))
        .limit(request.query.limit);

      return { data: rows };
    },
  );
};
