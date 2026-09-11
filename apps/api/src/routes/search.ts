import { domainEnrichment, huntFindings, huntJobs, leaks, sources } from "@leak/db";
import { and, desc, eq, gt, inArray, sql } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Company search, in two halves.
 *
 * `GET /api/search` is the instant half: it reads what we already hold and returns in
 * milliseconds. `POST /api/hunt` is the other half — it asks the worker to go and look, and
 * `GET /api/hunt/:id` streams the answer back as each lookup lands.
 *
 * They are separate endpoints rather than one slow endpoint on purpose. The overwhelming
 * majority of searches are for a company we already have listings for, and those must not
 * pay for a certificate-transparency lookup that might take fifteen seconds. Search answers
 * immediately with what it knows and tells the client whether a hunt is worth starting.
 */

/**
 * How fresh a finished hunt has to be to answer a new request for the same company.
 *
 * Matches the worker's `HUNT_CACHE_TTL` default. Duplicated rather than shared because the
 * two processes have no config channel between them — if you change one, change both. The
 * consequence of drift is mild in one direction (the API re-queues a hunt the worker would
 * have considered fresh) and invisible in the other.
 */
const HUNT_CACHE_MS = 60 * 60_000;

const MAX_RESULTS = 50;

const searchQuery = z.object({
  q: z.string().min(2).max(200),
  limit: z.coerce.number().int().min(1).max(MAX_RESULTS).default(20),
});

const leakHit = z.object({
  id: z.number(),
  victimName: z.string().nullable(),
  victimDomain: z.string().nullable(),
  victimCountry: z.string().nullable(),
  victimSector: z.string().nullable(),
  actorGroup: z.string(),
  status: z.string(),
  sourceSlug: z.string().nullable(),
  sourceUrl: z.string().nullable(),
  publishedAt: z.date().nullable(),
  firstSeenAt: z.date(),
  lastSeenAt: z.date(),
  leakSizeBytes: z.number().nullable(),
  /** Full-text rank. Exposed so the UI can show why one hit outranks another. */
  rank: z.number(),
});

const domainHit = z.object({
  domain: z.string(),
  registrar: z.string().nullable(),
  status: z.string(),
  httpStatus: z.number().nullable(),
  technologies: z.array(z.string()).nullable(),
  pageTitle: z.string().nullable(),
  whoisContacts: z.record(z.string(), z.array(z.string())).nullable(),
  checkedAt: z.date().nullable(),
});

const searchResponse = z.object({
  query: z.string(),
  leaks: z.array(leakHit),
  domains: z.array(domainHit),
  /**
   * Whether going out to the network would plausibly add anything, and why.
   *
   * The client uses this to decide whether to auto-start a hunt. Returning it from the
   * server keeps that judgement in one place — the alternative is every caller
   * reimplementing "is this result thin?" and disagreeing about the threshold.
   */
  hunt: z.object({
    suggested: z.boolean(),
    reason: z.string(),
    /** A recent hunt for this query, if one exists. The client can open it directly. */
    existingJobId: z.number().nullable(),
  }),
});

const huntJobSchema = z.object({
  id: z.number(),
  query: z.string(),
  targetDomain: z.string().nullable(),
  status: z.enum(["queued", "running", "succeeded", "partial", "failed"]),
  requestedAt: z.date(),
  startedAt: z.date().nullable(),
  finishedAt: z.date().nullable(),
  findingsCount: z.number(),
  errors: z.record(z.string(), z.string()).nullable(),
});

const huntFindingSchema = z.object({
  id: z.number(),
  kind: z.enum([
    "leak_match",
    "raw_page_mention",
    "registration",
    "infrastructure",
    "certificate",
    "liveness",
  ]),
  title: z.string(),
  detail: z.record(z.string(), z.unknown()).nullable(),
  sourceLabel: z.string(),
  occurredAt: z.date().nullable(),
});

/** Collapse whitespace and lowercase — must match the worker's `normalize_query`. */
function normalizeQuery(raw: string): string {
  return raw.trim().replace(/\s+/g, " ").toLowerCase();
}

export const searchRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/search",
    {
      schema: {
        description:
          "Instant search across everything already collected. Never touches the network.",
        tags: ["search"],
        querystring: searchQuery,
        response: { 200: searchResponse },
      },
    },
    async (request) => {
      const { q, limit } = request.query;
      const normalized = normalizeQuery(q);

      /**
       * Word match OR substring match, ranked.
       *
       * Both arms are necessary and neither is redundant. `plainto_tsquery` handles real
       * words and stemming and gives a usable ranking; `ilike` catches the run-together
       * spelling ("framegroup" for "The Frame Group") and the half-typed fragment, neither
       * of which `to_tsvector` can see because it splits on word boundaries.
       *
       * Each arm has its own index — the GIN full-text one and the two trigram ones added
       * in migration 0005 — so this stays an index scan rather than degrading to a table
       * scan as the leak count grows.
       *
       * Rows matching only on substring get rank 0 from `ts_rank` and sort below real word
       * matches, which is the correct ordering: an exact name match should beat a domain
       * that merely contains the letters.
       */
      const pattern = `%${q}%`;
      const rows = await fastify.db
        .select({
          id: leaks.id,
          victimName: leaks.victimName,
          victimDomain: leaks.victimDomain,
          victimCountry: leaks.victimCountry,
          victimSector: leaks.victimSector,
          actorGroup: leaks.actorGroup,
          status: leaks.status,
          sourceSlug: sources.slug,
          sourceUrl: leaks.sourceUrl,
          publishedAt: leaks.publishedAt,
          firstSeenAt: leaks.firstSeenAt,
          lastSeenAt: leaks.lastSeenAt,
          leakSizeBytes: leaks.leakSizeBytes,
          rank: sql<number>`ts_rank(
            to_tsvector('english',
              coalesce(${leaks.victimName}, '') || ' ' || coalesce(${leaks.victimDomain}, '')),
            plainto_tsquery('english', ${q})
          )::float8`,
        })
        .from(leaks)
        .leftJoin(sources, eq(leaks.sourceId, sources.id))
        .where(
          sql`to_tsvector('english',
                coalesce(${leaks.victimName}, '') || ' ' || coalesce(${leaks.victimDomain}, ''))
              @@ plainto_tsquery('english', ${q})
              or ${leaks.victimName} ilike ${pattern}
              or ${leaks.victimDomain} ilike ${pattern}`,
        )
        .orderBy(sql`4 desc`, desc(leaks.firstSeenAt))
        .limit(limit);

      /**
       * Enrichment we already hold for any domain in the results, plus any domain whose
       * name matches the query directly.
       *
       * Read from the cache, never fetched here: this endpoint promises to be instant, and
       * an RDAP lookup is not. A domain with no cached row simply comes back absent, which
       * is what makes `hunt.suggested` meaningful.
       */
      const domainsInResults = rows
        .map((r) => r.victimDomain)
        .filter((d): d is string => Boolean(d));

      const domainConditions = [sql`${domainEnrichment.domain} ilike ${pattern}`];
      if (domainsInResults.length > 0) {
        domainConditions.push(inArray(domainEnrichment.domain, domainsInResults));
      }

      const domainRows = await fastify.db
        .select({
          domain: domainEnrichment.domain,
          registrar: domainEnrichment.registrar,
          status: domainEnrichment.status,
          httpStatus: domainEnrichment.httpStatus,
          technologies: domainEnrichment.technologies,
          pageTitle: domainEnrichment.pageTitle,
          whoisContacts: domainEnrichment.whoisContacts,
          checkedAt: domainEnrichment.checkedAt,
        })
        .from(domainEnrichment)
        .where(sql`${sql.join(domainConditions, sql` or `)}`)
        .limit(limit);

      // A hunt already run for this query, recently enough to still answer.
      const [recent] = await fastify.db
        .select({ id: huntJobs.id })
        .from(huntJobs)
        .where(
          and(
            eq(huntJobs.normalizedQuery, normalized),
            inArray(huntJobs.status, ["succeeded", "partial"]),
            gt(huntJobs.finishedAt, new Date(Date.now() - HUNT_CACHE_MS)),
          ),
        )
        .orderBy(desc(huntJobs.finishedAt))
        .limit(1);

      const hunt = (() => {
        if (recent) {
          return {
            suggested: false,
            reason: "A recent hunt for this company is already available.",
            existingJobId: recent.id,
          };
        }
        if (rows.length === 0) {
          return {
            suggested: true,
            reason: "Nothing collected for this company yet.",
            existingJobId: null,
          };
        }
        if (domainRows.length === 0) {
          return {
            suggested: true,
            reason: "Listings found, but no registration or liveness data yet.",
            existingJobId: null,
          };
        }
        return {
          suggested: false,
          reason: "Everything we hold is already enriched.",
          existingJobId: null,
        };
      })();

      return {
        query: q,
        leaks: rows,
        domains: domainRows.map((d) => ({
          ...d,
          whoisContacts: (d.whoisContacts as Record<string, string[]> | null) ?? null,
        })),
        hunt,
      };
    },
  );

  fastify.post(
    "/api/hunt",
    {
      config: {
        /**
         * Far tighter than the global 300/min. Each of these sends requests to registries,
         * certificate logs and a third party's web server — a held-down key must not turn
         * this console into something that looks like an attack from the outside.
         */
        rateLimit: { max: 10, timeWindow: "1 minute" },
      },
      schema: {
        description:
          "Queue a company hunt. Returns an existing recent hunt rather than re-running one.",
        tags: ["search"],
        body: z.object({ query: z.string().min(2).max(200) }),
        response: {
          200: z.object({ job: huntJobSchema, cached: z.boolean() }),
          201: z.object({ job: huntJobSchema, cached: z.boolean() }),
        },
      },
    },
    async (request, reply) => {
      const query = request.body.query.trim();
      const normalized = normalizeQuery(query);

      /**
       * Reuse before enqueue, checked here rather than in the worker.
       *
       * The worker could dedupe after claiming, but by then the client has already been
       * handed a job id and is polling a hunt that will do nothing. Answering with the
       * existing job means a repeat search renders instantly from rows that already exist.
       */
      const [cached] = await fastify.db
        .select()
        .from(huntJobs)
        .where(
          and(
            eq(huntJobs.normalizedQuery, normalized),
            inArray(huntJobs.status, ["succeeded", "partial"]),
            gt(huntJobs.finishedAt, new Date(Date.now() - HUNT_CACHE_MS)),
          ),
        )
        .orderBy(desc(huntJobs.finishedAt))
        .limit(1);

      if (cached) {
        return reply.status(200).send({ job: toJobResponse(cached), cached: true });
      }

      // An identical hunt already in flight is answered with that one, for the same reason
      // `/api/crawl` returns the running request instead of stacking a second behind it.
      const [inFlight] = await fastify.db
        .select()
        .from(huntJobs)
        .where(
          and(
            eq(huntJobs.normalizedQuery, normalized),
            inArray(huntJobs.status, ["queued", "running"]),
          ),
        )
        .orderBy(desc(huntJobs.requestedAt))
        .limit(1);

      if (inFlight) {
        return reply.status(200).send({ job: toJobResponse(inFlight), cached: false });
      }

      const [created] = await fastify.db
        .insert(huntJobs)
        .values({
          query,
          normalizedQuery: normalized,
          requestedBy: request.currentUser?.id ?? null,
        })
        .returning();

      request.log.info({ hunt: created!.id, query }, "hunt queued");
      return reply.status(201).send({ job: toJobResponse(created!), cached: false });
    },
  );

  fastify.get(
    "/api/hunt/:id",
    {
      schema: {
        description:
          "A hunt and everything it has found so far. Safe to poll — findings accumulate " +
          "as each lookup returns, so a running hunt already has results worth rendering.",
        tags: ["search"],
        params: z.object({ id: z.coerce.number().int().positive() }),
        response: {
          200: z.object({
            job: huntJobSchema,
            findings: z.array(huntFindingSchema),
          }),
          404: z.object({ error: z.string(), message: z.string() }),
        },
      },
    },
    async (request, reply) => {
      const [job] = await fastify.db
        .select()
        .from(huntJobs)
        .where(eq(huntJobs.id, request.params.id))
        .limit(1);

      if (!job) {
        return reply
          .status(404)
          .send({ error: "not_found", message: `No hunt with id ${request.params.id}` });
      }

      const findings = await fastify.db
        .select({
          id: huntFindings.id,
          kind: huntFindings.kind,
          title: huntFindings.title,
          detail: huntFindings.detail,
          sourceLabel: huntFindings.sourceLabel,
          occurredAt: huntFindings.occurredAt,
        })
        .from(huntFindings)
        .where(eq(huntFindings.jobId, job.id))
        .orderBy(huntFindings.kind, desc(huntFindings.occurredAt));

      return { job: toJobResponse(job), findings };
    },
  );
};

/** Narrow a stored job row to the response shape. Shared by all three handlers. */
function toJobResponse(job: typeof huntJobs.$inferSelect) {
  return {
    id: job.id,
    query: job.query,
    targetDomain: job.targetDomain,
    status: job.status,
    requestedAt: job.requestedAt,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt,
    findingsCount: job.findingsCount,
    errors: (job.errors as Record<string, string> | null) ?? null,
  };
}
