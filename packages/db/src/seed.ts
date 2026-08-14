/**
 * Development seed. Idempotent — safe to run repeatedly.
 *
 * Deliberately seeds enough leaks to prove pagination is real: if an endpoint ever regresses
 * to returning everything, the row count makes it obvious.
 */
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { config } from "dotenv";
import { sql } from "drizzle-orm";
import { createDb } from "./client.js";
import { leaks, sources, type NewLeak } from "./schema/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));
config({ path: path.resolve(here, "../../../.env") });

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("DATABASE_URL is not set.");
  process.exit(1);
}

const { db, sql: raw } = createDb(url, { max: 2 });

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

const VICTIMS = [
  ["Northwind Logistics", "northwind.example", "US", "Transport"],
  ["Contoso Manufacturing", "contoso.example", "DE", "Manufacturing"],
  ["Fabrikam Health", "fabrikam.example", "UK", "Healthcare"],
  ["Adventure Works", "adventureworks.example", "AU", "Retail"],
  ["Tailspin Financial", "tailspin.example", "SG", "Finance"],
  ["Wingtip Legal", "wingtip.example", "CA", "Legal"],
  ["Proseware Energy", "proseware.example", "NO", "Energy"],
  ["Litware Education", "litware.example", "IN", "Education"],
];

const STATUSES = ["published", "countdown", "sold", "unknown"] as const;

try {
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
          extraction: { method: "manual", modelVersion: "seed", confidence: 1 },
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

  console.log(`Done. ${sourceCount} sources, ${leakCount} leaks.`);
} catch (error) {
  console.error("Seed failed:", error);
  process.exitCode = 1;
} finally {
  await raw.end();
}
