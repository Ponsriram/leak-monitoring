/**
 * Schema tests against a real Postgres engine (PGlite = Postgres compiled to WASM).
 *
 * These aren't smoke tests. Each one pins down a constraint that exists specifically because
 * its absence caused a live defect in the old system:
 *
 *   - duplicate leaks on every pipeline run   -> UNIQUE (dedupe_hash) + upsert
 *   - "what's new" was unanswerable           -> first_seen_at set once, last_seen_at touched
 *   - the same alert email could be re-sent    -> UNIQUE (alert_id, leak_id)
 *   - two accounts could share an email        -> UNIQUE (user.email)
 *   - status was free text                     -> leak_status enum
 */
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import { PGlite } from "@electric-sql/pglite";
import { eq, sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/pglite";
import { migrate } from "drizzle-orm/pglite/migrator";
import * as schema from "../src/schema/index.js";

const here = path.dirname(fileURLToPath(import.meta.url));

const client = new PGlite();
const db = drizzle(client, { schema });

await migrate(db, { migrationsFolder: path.resolve(here, "../migrations") });

/**
 * Drizzle wraps driver errors, so the Postgres message ("duplicate key value violates …")
 * lands on `error.cause`, not `error.message`. Asserting on the top-level message alone
 * silently passes for the wrong reason, so walk the whole chain.
 */
function pgError(pattern: RegExp) {
  return (error: unknown): boolean => {
    let current: unknown = error;
    while (current != null) {
      const message = (current as { message?: unknown }).message;
      if (typeof message === "string" && pattern.test(message)) return true;
      current = (current as { cause?: unknown }).cause;
    }
    throw new Error(
      `expected an error matching ${pattern} somewhere in the cause chain, got: ${String(error)}`,
    );
  };
}

/** Every test gets its own source row so ids never collide across tests. */
async function makeSource(slug: string) {
  const [row] = await db
    .insert(schema.sources)
    .values({ slug, name: slug, baseUrl: `http://${slug}.onion/` })
    .returning();
  return row!;
}

async function makeUser(id: string, email: string) {
  const [row] = await db
    .insert(schema.user)
    .values({ id, name: id, email })
    .returning();
  return row!;
}

describe("migration", () => {
  it("creates every expected table", async () => {
    const result = await db.execute<{ table_name: string }>(
      sql`select table_name from information_schema.tables
          where table_schema = 'public' and table_type = 'BASE TABLE'`,
    );
    const tables = result.rows.map((r) => r.table_name).sort();

    for (const expected of [
      "account",
      "alert_events",
      "alerts",
      "crawl_runs",
      "leaks",
      "raw_pages",
      "session",
      "sources",
      "user",
      "verification",
    ]) {
      assert.ok(tables.includes(expected), `missing table: ${expected}`);
    }
  });

  it("creates the leak_status enum with the expected values", async () => {
    const result = await db.execute<{ enumlabel: string }>(
      sql`select enumlabel from pg_enum e
          join pg_type t on t.oid = e.enumtypid
          where t.typname = 'leak_status' order by e.enumsortorder`,
    );
    assert.deepEqual(
      result.rows.map((r) => r.enumlabel),
      ["published", "countdown", "sold", "removed", "unknown"],
    );
  });
});

describe("leaks.dedupe_hash", () => {
  it("rejects a duplicate hash", async () => {
    const source = await makeSource("dupe-test");
    const values = {
      dedupeHash: "hash-duplicate",
      actorGroup: "lockbit",
      victimName: "Acme Corp",
      sourceId: source.id,
    };

    await db.insert(schema.leaks).values(values);

    // The old pipeline had no constraint here, so a second run silently doubled the dataset.
    await assert.rejects(
      () => db.insert(schema.leaks).values(values),
      pgError(/duplicate key value violates unique constraint/),
      "a second insert with the same dedupe_hash should be rejected",
    );
  });

  it("upsert preserves first_seen_at and advances last_seen_at", async () => {
    const source = await makeSource("upsert-test");
    const firstSeen = new Date("2026-01-01T00:00:00Z");

    await db.insert(schema.leaks).values({
      dedupeHash: "hash-upsert",
      actorGroup: "blackcat",
      victimName: "Initial Name",
      sourceId: source.id,
      firstSeenAt: firstSeen,
      lastSeenAt: firstSeen,
      status: "countdown",
    });

    // Simulate the same listing being seen again on a later crawl.
    const secondSeen = new Date("2026-02-01T00:00:00Z");
    await db
      .insert(schema.leaks)
      .values({
        dedupeHash: "hash-upsert",
        actorGroup: "blackcat",
        victimName: "Initial Name",
        sourceId: source.id,
        firstSeenAt: secondSeen,
        lastSeenAt: secondSeen,
        status: "published",
      })
      .onConflictDoUpdate({
        target: schema.leaks.dedupeHash,
        // Deliberately absent: firstSeenAt. That column is written once, ever.
        set: { lastSeenAt: secondSeen, status: "published" },
      });

    const rows = await db
      .select()
      .from(schema.leaks)
      .where(eq(schema.leaks.dedupeHash, "hash-upsert"));

    assert.equal(rows.length, 1, "upsert must not create a second row");
    const row = rows[0]!;
    assert.equal(
      row.firstSeenAt.toISOString(),
      firstSeen.toISOString(),
      "first_seen_at must survive the upsert — it defines 'what's new'",
    );
    assert.equal(
      row.lastSeenAt.toISOString(),
      secondSeen.toISOString(),
      "last_seen_at must advance — it defines 'still listed'",
    );
    assert.equal(row.status, "published", "mutable fields should update");
  });

  it("rejects an invalid status value", async () => {
    const source = await makeSource("enum-test");
    // Bypassing the TS types on purpose: this is what a bad pipeline write would look like.
    await assert.rejects(
      () =>
        db.execute(sql`
          insert into leaks (dedupe_hash, actor_group, source_id, status)
          values ('hash-bad-status', 'lockbit', ${source.id}, 'totally-made-up')
        `),
      pgError(/invalid input value for enum leak_status/),
    );
  });

  it("rejects a leak pointing at a source that does not exist", async () => {
    await assert.rejects(
      () =>
        db.insert(schema.leaks).values({
          dedupeHash: "hash-orphan",
          actorGroup: "lockbit",
          sourceId: 999_999,
        }),
      pgError(/violates foreign key constraint/),
    );
  });
});

describe("alert delivery idempotency", () => {
  it("rejects sending the same leak to the same alert twice", async () => {
    const owner = await makeUser("user-idem", "idem@example.com");
    const source = await makeSource("alert-test");

    const [alert] = await db
      .insert(schema.alerts)
      .values({
        ownerId: owner.id,
        name: "Acme watch",
        matchKind: "substring",
        matchValue: "acme",
        channel: "email",
        target: "analyst@example.com",
      })
      .returning();

    const [leak] = await db
      .insert(schema.leaks)
      .values({
        dedupeHash: "hash-alerted",
        actorGroup: "lockbit",
        victimName: "Acme Corp",
        sourceId: source.id,
      })
      .returning();

    const event = {
      alertId: alert!.id,
      leakId: leak!.id,
      matchedOn: "victim_name",
      channel: "email" as const,
      target: "analyst@example.com",
    };

    await db.insert(schema.alertEvents).values(event);

    // A worker retry or a duplicate queue message must not produce a second email.
    await assert.rejects(
      () => db.insert(schema.alertEvents).values(event),
      pgError(/duplicate key value violates unique constraint/),
    );
  });
});

describe("user.email", () => {
  it("rejects two accounts sharing an email", async () => {
    await makeUser("user-a", "shared@example.com");
    await assert.rejects(
      () => makeUser("user-b", "shared@example.com"),
      pgError(/duplicate key value violates unique constraint/),
      "the old schema allowed this",
    );
  });
});

describe("victim full-text search", () => {
  it("matches on the GIN index expression", async () => {
    const source = await makeSource("search-test");
    await db.insert(schema.leaks).values([
      {
        dedupeHash: "hash-search-1",
        actorGroup: "lockbit",
        victimName: "Northwind Logistics",
        victimDomain: "northwind.example",
        sourceId: source.id,
      },
      {
        dedupeHash: "hash-search-2",
        actorGroup: "lockbit",
        victimName: "Contoso Manufacturing",
        victimDomain: "contoso.example",
        sourceId: source.id,
      },
    ]);

    const result = await db.execute<{ victim_name: string }>(sql`
      select victim_name from leaks
      where to_tsvector('english', coalesce(victim_name, '') || ' ' || coalesce(victim_domain, ''))
            @@ plainto_tsquery('english', 'northwind')
    `);

    assert.equal(result.rows.length, 1);
    assert.equal(result.rows[0]!.victim_name, "Northwind Logistics");
  });
});
