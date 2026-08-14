/**
 * Apply pending migrations. Run with `npm run db:migrate` from the repo root.
 *
 * This is separate from `drizzle-kit push` on purpose: push diffs the schema against a live
 * database and is fine for local iteration, but only versioned migration files are safe to
 * run against data you care about.
 */
import { fileURLToPath } from "node:url";
import path from "node:path";
import { config } from "dotenv";
import { drizzle } from "drizzle-orm/postgres-js";
import { migrate } from "drizzle-orm/postgres-js/migrator";
import postgres from "postgres";

const here = path.dirname(fileURLToPath(import.meta.url));
config({ path: path.resolve(here, "../../../.env") });

const url = process.env.DATABASE_URL;
if (!url) {
  console.error(
    "DATABASE_URL is not set. Copy .env.example to .env at the repo root and fill it in.",
  );
  process.exit(1);
}

// A dedicated single connection — migrations must not run concurrently.
const sql = postgres(url, { max: 1 });

try {
  console.log("Applying migrations…");
  await migrate(drizzle(sql), {
    migrationsFolder: path.resolve(here, "../migrations"),
  });
  console.log("Migrations applied.");
} catch (error) {
  console.error("Migration failed:", error);
  process.exitCode = 1;
} finally {
  await sql.end();
}
