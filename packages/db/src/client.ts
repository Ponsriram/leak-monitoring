import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema/index.js";

export type Database = ReturnType<typeof createDb>["db"];

export type CreateDbOptions = {
  /** Pool size. Keep it below Postgres `max_connections` divided by your replica count. */
  max?: number;
  /** Seconds an idle connection is kept before being closed. */
  idleTimeout?: number;
  /** Seconds to wait for a connection before giving up. */
  connectTimeout?: number;
  /** Log every statement. Development only — queries can contain victim identifiers. */
  debug?: boolean;
};

/**
 * Build a Drizzle client plus the underlying connection.
 *
 * The raw `sql` handle is returned alongside so callers can close the pool on shutdown —
 * the old server never closed anything and relied on process death.
 */
export function createDb(url: string, options: CreateDbOptions = {}) {
  const sql = postgres(url, {
    max: options.max ?? 10,
    idle_timeout: options.idleTimeout ?? 30,
    connect_timeout: options.connectTimeout ?? 10,
    // Fail fast rather than buffering queries against a database that isn't there.
    onnotice: () => {},
    ...(options.debug ? { debug: true } : {}),
  });

  const db = drizzle(sql, { schema });

  return { db, sql };
}
