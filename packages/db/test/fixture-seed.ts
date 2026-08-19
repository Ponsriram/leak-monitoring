/**
 * CI fixture. **Not development data, and not for a database that holds real crawls.**
 *
 * This exists for one caller: `scripts/smoke-api.sh` in CI, which runs against a fresh
 * throwaway Postgres with no Tor and therefore no way to collect anything. An API smoke test
 * against an empty database verifies almost nothing — it cannot check that pagination caps
 * a real result set, that a group filter returns only that group, that full-text search
 * matches, or that per-source `leakCount` correlates (a bug that once passed review because
 * every count was wrong in the same direction). Those checks need rows, so CI makes some.
 *
 * It used to live at `src/seed.ts` and be wired to `npm run db:seed`, one word away from any
 * developer's real database. Running it there mixed 144 invented victims — Northwind
 * Logistics, Contoso Manufacturing, the Microsoft sample-company set — into genuinely
 * collected intelligence, where they were indistinguishable at a glance from real listings
 * and quietly inflated every dashboard total, chart and filter dropdown built on that table.
 *
 * So it now lives under `test/`, out of the package's shipped surface, and refuses outright
 * to run against a database containing any leak it did not create. Re-polluting a real
 * dataset is not something a flag should make convenient.
 */
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { config } from "dotenv";
import { sql } from "drizzle-orm";
import { createDb } from "../src/client.js";
import { leaks, sources, type NewLeak } from "../src/schema/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
config({ path: path.resolve(here, "../../../.env") });

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("DATABASE_URL is not set.");
  process.exit(1);
}

const { db, sql: raw } = createDb(url, { max: 2 });

/**
 * Every row this file writes carries this marker, and it is the only thing that separates a
 * fixture victim from a collected one once both are sitting in `leaks`.
 */
const FIXTURE_MARKER = "seed";

/**
 * Same rule the pipeline will use, so seeded rows behave like real ones.
 *
 * Critically this must NOT include any wall-clock-derived value. An earlier version folded
 * the (jittered, Date.now()-based) publication timestamp into the hash, so every run produced
 * fresh hashes and the seed doubled the table instead of being a no-op. Identity is
 * (group, victim) — the timestamps are supporting evidence, not part of the key.
 */
function dedupeHash(group: string, victim: string): string {
  return crypto.createHash("sha256").update(`${group}|${victim}`).digest("hex");
}

/** Midnight-anchored so re-seeding on the same day produces identical rows. */
const TODAY = new Date(new Date().toISOString().slice(0, 10) + "T00:00:00.000Z").getTime();

const SOURCES = [
  { slug: "lockbit", name: "LockBit", collector: "browser" as const, failures: 0 },
  { slug: "blackcat", name: "BlackCat / ALPHV", collector: "http" as const, failures: 0 },
  { slug: "cl0p", name: "Cl0p", collector: "http" as const, failures: 1 },
  { slug: "play", name: "Play", collector: "http" as const, failures: 0 },
  { slug: "akira", name: "Akira", collector: "browser" as const, failures: 4 },
  { slug: "8base", name: "8Base", collector: "http" as const, failures: 0 },
];

/**
 * Country and sector are written in the extractor's canonical vocabulary — "United States",
 * not "US"; "Transportation & Logistics", not "Transport".
 *
 * These are the same columns the pipeline fills, and the Leaks page builds its Location and
 * Sector dropdowns by grouping on them. Seeding a second spelling would put "US" and
 * "United States" in the same list as two separate filters over the same country, which is
 * exactly the mess `actor_group` slugification exists to prevent. The canonical values live
 * in `services/intel/intel/extract/gazetteer.py`.
 */
const VICTIMS = [
  ["Northwind Logistics", "northwind.example", "United States", "Transportation & Logistics"],
  ["Contoso Manufacturing", "contoso.example", "Germany", "Manufacturing"],
  ["Fabrikam Health", "fabrikam.example", "United Kingdom", "Healthcare"],
  ["Adventure Works", "adventureworks.example", "Australia", "Retail"],
  ["Tailspin Financial", "tailspin.example", "Singapore", "Financial Services"],
  ["Wingtip Legal", "wingtip.example", "Canada", "Legal"],
  ["Proseware Energy", "proseware.example", "Norway", "Energy & Utilities"],
  ["Litware Education", "litware.example", "India", "Education"],
];

const STATUSES = ["published", "countdown", "sold", "unknown"] as const;

try {
  /**
   * The refusal. Any leak this fixture did not write means the database is somebody's real
   * dataset, and fixture rows must not join it.
   *
   * Checked against `leaks` rather than against a row count, because "empty" is the wrong
   * test: a database holding only fixture rows is safe to re-seed (that is what makes CI
   * re-runnable), and one holding a single collected listing is not, however small.
   */
  const [{ count: real } = { count: 0 }] = await db.execute<{ count: number }>(
    sql`select count(*)::int as count
          from leaks
         where extraction->>'modelVersion' is distinct from ${FIXTURE_MARKER}`,
  );

  if (real > 0) {
    console.error(
      [
        `Refusing to run: this database holds ${real} collected leak(s).`,
        "",
        "This is a CI fixture. It writes invented victims (Northwind Logistics, Contoso",
        "Manufacturing, …) which are indistinguishable from real listings once they are in",
        "the same table, and which inflate every total, chart and filter built on it.",
        "",
        "To collect real data instead:  npm run intel -- run",
        "To inspect what is already there:  npm run intel -- status",
      ].join("\n"),
    );
    process.exit(1);
  }

  console.log("Seeding sources…");
  for (const source of SOURCES) {
    await db
      .insert(sources)
      .values({
        slug: source.slug,
        name: source.name,
        // These are illustrative addresses, not real onion services. They exist so the
        // dashboard has something to render before the collection pipeline runs.
        baseUrl: `http://${source.slug}example.onion/`,
        collector: source.collector,
        // Demo rows must never be crawl targets: `intel run` would spend its retry budget
        // failing to reach four addresses that do not exist. Real sources are enabled
        // deliberately via `intel sources enable`.
        enabled: false,
        consecutiveFailures: source.failures,
        lastCrawlAt: new Date(Date.now() - 30 * 60_000),
        lastSuccessAt:
          source.failures === 0 ? new Date(Date.now() - 30 * 60_000) : new Date(Date.now() - 86_400_000),
      })
      .onConflictDoNothing({ target: sources.slug });
  }

  const sourceRows = await db.select({ id: sources.id, slug: sources.slug }).from(sources);
  const byslug = new Map(sourceRows.map((r) => [r.slug, r.id]));

  console.log("Seeding leaks…");
  const rows: NewLeak[] = [];
  let n = 0;
  // 8 victims x 6 groups x 3 = 144 leaks, spread across the last 60 days.
  for (let repeat = 0; repeat < 3; repeat++) {
    for (const source of SOURCES) {
      for (const [name, domain, country, sector] of VICTIMS) {
        const daysAgo = n % 60;
        const seenAt = new Date(TODAY - daysAgo * 86_400_000 + (n % 24) * 3_600_000);
        const published = new Date(seenAt.getTime() - 86_400_000);
        const victim = `${name}${repeat > 0 ? ` ${repeat + 1}` : ""}`;

        rows.push({
          dedupeHash: dedupeHash(source.slug, victim),
          victimName: victim,
          victimDomain: domain!,
          victimCountry: country!,
          victimSector: sector!,
          actorGroup: source.slug,
          sourceId: byslug.get(source.slug) ?? null,
          sourceUrl: `http://${source.slug}example.onion/post/${n}`,
          publishedAt: published,
          publishedAtRaw: published.toDateString(),
          firstSeenAt: seenAt,
          lastSeenAt: seenAt,
          status: STATUSES[n % STATUSES.length]!,
          leakSizeBytes: (n % 9) * 1_099_511_627_776,
          extraction: { method: "manual", modelVersion: FIXTURE_MARKER, confidence: 1 },
        });
        n++;
      }
    }
  }

  // onConflictDoNothing makes the whole script re-runnable — the very property the old
  // notebook lacked, which is why every run duplicated the dataset.
  await db.insert(leaks).values(rows).onConflictDoNothing({ target: leaks.dedupeHash });

  const [{ count: leakCount } = { count: 0 }] = await db.execute<{ count: number }>(
    sql`select count(*)::int as count from leaks`,
  );
  const [{ count: sourceCount } = { count: 0 }] = await db.execute<{ count: number }>(
    sql`select count(*)::int as count from sources`,
  );

  console.log(`Done. ${sourceCount} fixture sources, ${leakCount} fixture leaks.`);
} catch (error) {
  console.error("Fixture seed failed:", error);
  process.exitCode = 1;
} finally {
  await raw.end();
}
