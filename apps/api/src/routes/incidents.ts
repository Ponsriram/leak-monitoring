import { domainEnrichment, leaks, sources } from "@leak/db";
import { and, asc, count, desc, eq, gte, lte, sql, type SQL } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * The section tables, and the data behind the live map.
 *
 * Every row here is a leak we collected, joined to whatever enrichment exists for its
 * domain. The three sections differ in what they put first, not in where the data comes
 * from: Ransomware leads with the actor and the victim, Dark Web leads with the exposed site
 * and its stack. Serving them from one query shape means a column added to one is available
 * to the other, instead of two near-identical handlers drifting apart.
 *
 * The enrichment join is LEFT and always will be: a victim domain nobody has enriched yet is
 * a normal row with empty technology and status columns, not a row to hide. The background
 * sweep fills those in over time, and a table that only showed enriched rows would appear to
 * lose most of its contents.
 */

const MAX_LIMIT = 100;

const listQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(MAX_LIMIT).default(20),
  group: z.string().min(1).max(100).optional(),
  country: z.string().min(1).max(100).optional(),
  sector: z.string().min(1).max(100).optional(),
  status: z.string().min(1).max(30).optional(),
  siteStatus: z.enum(["live", "down", "error", "not_scanned"]).optional(),
  /**
   * `leak_type`, free text rather than an enum.
   *
   * The column is a plain text column with a default, not a pgEnum — a closed list here
   * would reject a classification the pipeline had already written to the row, and the
   * filter would silently return nothing for it.
   */
  type: z.string().min(1).max(40).optional(),
  q: z.string().min(1).max(200).optional(),
  from: z.coerce.date().optional(),
  to: z.coerce.date().optional(),
  sort: z.enum(["first_seen_at", "published_at", "victim_name"]).default("first_seen_at"),
  order: z.enum(["asc", "desc"]).default("desc"),
});

const incidentRow = z.object({
  id: z.number(),
  firstSeenAt: z.date(),
  publishedAt: z.date().nullable(),
  lastSeenAt: z.date(),
  actorGroup: z.string(),
  victimName: z.string().nullable(),
  victimDomain: z.string().nullable(),
  victimCountry: z.string().nullable(),
  victimSector: z.string().nullable(),
  status: z.string(),
  leakType: z.string(),
  leakSizeBytes: z.number().nullable(),
  sourceSlug: z.string().nullable(),
  sourceUrl: z.string().nullable(),

  // --- from domain_enrichment, all nullable until the sweep reaches this domain ---
  siteStatus: z.string().nullable(),
  httpStatus: z.number().nullable(),
  technologies: z.array(z.string()).nullable(),
  registrar: z.string().nullable(),
  whoisContacts: z.record(z.string(), z.array(z.string())).nullable(),
  pageTitle: z.string().nullable(),
  enrichedAt: z.date().nullable(),
});

const listResponse = z.object({
  data: z.array(incidentRow),
  pagination: z.object({
    page: z.number(),
    limit: z.number(),
    total: z.number(),
    totalPages: z.number(),
  }),
});

/** The columns every section selects. One list, so the sections cannot drift. */
function incidentColumns() {
  return {
    id: leaks.id,
    firstSeenAt: leaks.firstSeenAt,
    publishedAt: leaks.publishedAt,
    lastSeenAt: leaks.lastSeenAt,
    actorGroup: leaks.actorGroup,
    victimName: leaks.victimName,
    victimDomain: leaks.victimDomain,
    victimCountry: leaks.victimCountry,
    victimSector: leaks.victimSector,
    status: leaks.status,
    leakType: leaks.leakType,
    leakSizeBytes: leaks.leakSizeBytes,
    sourceSlug: sources.slug,
    sourceUrl: leaks.sourceUrl,
    siteStatus: domainEnrichment.status,
    httpStatus: domainEnrichment.httpStatus,
    technologies: domainEnrichment.technologies,
    registrar: domainEnrichment.registrar,
    whoisContacts: domainEnrichment.whoisContacts,
    pageTitle: domainEnrichment.pageTitle,
    enrichedAt: domainEnrichment.checkedAt,
  };
}

type Filters = z.infer<typeof listQuery>;

function buildConditions(filters: Filters, extra: SQL[] = []): SQL | undefined {
  const conditions: SQL[] = [...extra];
  if (filters.group) conditions.push(eq(leaks.actorGroup, filters.group));
  if (filters.country) conditions.push(eq(leaks.victimCountry, filters.country));
  if (filters.sector) conditions.push(eq(leaks.victimSector, filters.sector));
  if (filters.status) conditions.push(sql`${leaks.status}::text = ${filters.status}`);
  if (filters.type) conditions.push(eq(leaks.leakType, filters.type));
  if (filters.siteStatus) conditions.push(sql`${domainEnrichment.status}::text = ${filters.siteStatus}`);
  if (filters.from) conditions.push(gte(leaks.firstSeenAt, filters.from));
  if (filters.to) conditions.push(lte(leaks.firstSeenAt, filters.to));
  if (filters.q) {
    // Same two-armed match the search endpoint uses — word matching for real words,
    // substring for the run-together spellings and fragments people actually type.
    const pattern = `%${filters.q}%`;
    conditions.push(
      sql`(to_tsvector('english',
             coalesce(${leaks.victimName}, '') || ' ' || coalesce(${leaks.victimDomain}, ''))
           @@ plainto_tsquery('english', ${filters.q})
           or ${leaks.victimName} ilike ${pattern}
           or ${leaks.victimDomain} ilike ${pattern})`,
    );
  }
  return conditions.length > 0 ? and(...conditions) : undefined;
}

export const incidentRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  /**
   * One handler, three routes.
   *
   * `extra` is the only difference between the sections — Dark Web restricts to rows that
   * actually have an exposed site, because a listing with no domain has nothing to show in
   * any of that section's columns.
   */
  function section(path: string, description: string, extra: () => SQL[]) {
    fastify.get(
      path,
      {
        schema: {
          description,
          tags: ["incidents"],
          querystring: listQuery,
          response: { 200: listResponse },
        },
      },
      async (request) => {
        const filters = request.query;
        const where = buildConditions(filters, extra());

        const sortColumn =
          filters.sort === "published_at"
            ? leaks.publishedAt
            : filters.sort === "victim_name"
              ? leaks.victimName
              : leaks.firstSeenAt;

        const [rows, totalResult] = await Promise.all([
          fastify.db
            .select(incidentColumns())
            .from(leaks)
            .leftJoin(sources, eq(leaks.sourceId, sources.id))
            .leftJoin(domainEnrichment, eq(domainEnrichment.domain, leaks.victimDomain))
            .where(where)
            .orderBy(filters.order === "asc" ? asc(sortColumn) : desc(sortColumn))
            .limit(filters.limit)
            .offset((filters.page - 1) * filters.limit),

          fastify.db
            .select({ value: count() })
            .from(leaks)
            .leftJoin(domainEnrichment, eq(domainEnrichment.domain, leaks.victimDomain))
            .where(where),
        ]);

        const total = totalResult[0]?.value ?? 0;
        return {
          data: rows.map((row) => ({
            ...row,
            whoisContacts: (row.whoisContacts as Record<string, string[]> | null) ?? null,
          })),
          pagination: {
            page: filters.page,
            limit: filters.limit,
            total,
            totalPages: Math.ceil(total / filters.limit),
          },
        };
      },
    );
  }

  section(
    "/api/incidents/general",
    "Every collected incident, newest first.",
    () => [],
  );

  section(
    "/api/incidents/ransomware",
    "Leak-site listings: actor, victim, and the victim's registration and stack.",
    () => [sql`${leaks.leakType} = 'ransomware'`],
  );

  section(
    "/api/incidents/darkweb",
    "Exposed victim sites, enriched with web-technology and WHOIS data.",
    // A dark-web row is about the exposed *site*. With no domain there is no site, and
    // every column in that section would be empty.
    () => [sql`${leaks.victimDomain} is not null`],
  );

  /**
   * Everything the live map draws, for one window of time.
   *
   * Coordinates are deliberately not returned. The client already owns a country-to-point
   * table, and sending lat/long per row would mean the same constant shipped twice and two
   * places to fix when a country is missing.
   *
   * **On the arcs.** They connect countries the *same actor* hit inside the window. They are
   * not attack paths and do not claim to be: we know which victims a group listed, and we do
   * not know where that group operates from. Drawing a line from an actor's supposed home
   * country would be inventing attribution, which is the one thing a threat-intel console
   * must never do — so the arcs show campaign spread, which is a fact we hold.
   */
  fastify.get(
    "/api/map/live",
    {
      schema: {
        description:
          "Actors, victim countries and campaign spread for the live threat map. " +
          "Arcs link countries hit by the same actor — they are not attack paths.",
        tags: ["map"],
        querystring: z.object({
          /** Anchor date. The window is built backwards from the end of this day. */
          date: z.coerce.date().optional(),
          period: z.enum(["date", "week", "month"]).default("date"),
          /** Arcs per actor. Caps what a single very active group can draw. */
          maxArcs: z.coerce.number().int().min(1).max(40).default(12),
        }),
        response: {
          200: z.object({
            period: z.object({
              kind: z.string(),
              from: z.date(),
              to: z.date(),
            }),
            totals: z.object({ actors: z.number(), incidents: z.number() }),
            actors: z.array(
              z.object({
                actorGroup: z.string(),
                categories: z.array(z.string()),
                targets: z.number(),
                countries: z.array(z.string()),
                counts: z.object({
                  iocs: z.number(),
                  cves: z.number(),
                  ttps: z.number(),
                  tech: z.number(),
                }),
              }),
            ),
            points: z.array(
              z.object({
                country: z.string(),
                total: z.number(),
                actors: z.array(z.string()),
              }),
            ),
            arcs: z.array(
              z.object({ actorGroup: z.string(), from: z.string(), to: z.string() }),
            ),
          }),
        },
      },
    },
    async (request) => {
      const { period, maxArcs } = request.query;
      const anchor = request.query.date ?? new Date();

      // The window ends at the end of the anchor day, so "today" includes everything
      // collected today rather than stopping at the moment the request was made.
      const to = new Date(anchor);
      to.setHours(23, 59, 59, 999);
      const from = new Date(to);
      if (period === "week") from.setDate(from.getDate() - 6);
      else if (period === "month") from.setDate(from.getDate() - 29);
      from.setHours(0, 0, 0, 0);

      const window = and(gte(leaks.firstSeenAt, from), lte(leaks.firstSeenAt, to));

      /**
       * Bound as ISO strings, not as Dates.
       *
       * Drizzle's query builder converts a Date for the column it is comparing against, but
       * `sql` template params go straight to the postgres-js driver, which only accepts
       * strings and buffers — a Date reaches it as an object and the query dies with
       * ERR_INVALID_ARG_TYPE. The explicit cast is what keeps Postgres reading them as
       * timestamps rather than text.
       */
      const fromParam = from.toISOString();
      const toParam = to.toISOString();

      /**
       * One pass over the window, aggregated per actor.
       *
       * `tech` counts the distinct technologies across that actor's victims, which is a real
       * number we hold. `iocs`, `cves` and `ttps` are structurally zero until the indicator
       * and vulnerability feeds land — reported as zero rather than omitted, because the
       * column exists and "we have no source for this yet" is what zero means here.
       */
      const actorRows = await fastify.db.execute<{
        actor_group: string;
        targets: number;
        countries: string[];
        leak_types: string[];
        statuses: string[];
        tech: number;
      }>(sql`
        select l.actor_group,
               count(*)::int as targets,
               coalesce(
                 array_agg(distinct l.victim_country)
                   filter (where l.victim_country is not null),
                 '{}'
               ) as countries,
               coalesce(array_agg(distinct l.leak_type), '{}') as leak_types,
               coalesce(array_agg(distinct l.status::text), '{}') as statuses,
               count(distinct t.tech)::int as tech
          from leaks l
          left join domain_enrichment e on e.domain = l.victim_domain
          left join lateral unnest(coalesce(e.technologies, '{}')) as t(tech) on true
         where l.first_seen_at >= ${fromParam}::timestamptz
           and l.first_seen_at <= ${toParam}::timestamptz
         group by l.actor_group
         order by count(*) desc
      `);

      const countryRows = await fastify.db.execute<{
        country: string;
        total: number;
        actors: string[];
      }>(sql`
        select l.victim_country as country,
               count(*)::int as total,
               array_agg(distinct l.actor_group) as actors
          from leaks l
         where l.victim_country is not null
           and l.first_seen_at >= ${fromParam}::timestamptz
           and l.first_seen_at <= ${toParam}::timestamptz
         group by l.victim_country
         order by count(*) desc
      `);

      const [incidentTotal] = await fastify.db
        .select({ value: count() })
        .from(leaks)
        .where(window);

      const actors = [...actorRows].map((row) => ({
        actorGroup: row.actor_group,
        // The chips beside an actor's name. Built from what the listings actually said —
        // the leak type, plus "Data Leak" once anything of theirs is published.
        categories: Array.from(
          new Set(
            [
              ...row.leak_types.map(titleCase),
              ...(row.statuses.includes("published") ? ["Data Leak"] : []),
            ].filter(Boolean),
          ),
        ),
        targets: row.targets,
        countries: row.countries,
        counts: { iocs: 0, cves: 0, ttps: 0, tech: row.tech },
      }));

      /**
       * Arcs: chain each actor's countries together in order.
       *
       * A chain rather than every pair. A group with eight countries has 28 pairs and seven
       * chain segments, and the 28 render as a solid blob that says nothing — the chain
       * conveys the same "these were hit by one actor" relationship legibly.
       */
      const arcs: Array<{ actorGroup: string; from: string; to: string }> = [];
      for (const actor of actors) {
        const countries = actor.countries;
        for (let i = 0; i + 1 < countries.length && i < maxArcs; i += 1) {
          arcs.push({
            actorGroup: actor.actorGroup,
            from: countries[i]!,
            to: countries[i + 1]!,
          });
        }
      }

      return {
        period: { kind: period, from, to },
        totals: { actors: actors.length, incidents: incidentTotal?.value ?? 0 },
        actors,
        points: [...countryRows].map((row) => ({
          country: row.country,
          total: row.total,
          actors: row.actors,
        })),
        arcs,
      };
    },
  );
};

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((word) => word[0]!.toUpperCase() + word.slice(1))
    .join(" ");
}
