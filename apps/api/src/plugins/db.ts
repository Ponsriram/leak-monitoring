import { createDb, type Database } from "@leak/db";
import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";
import type { Sql } from "postgres";
import { appConfig } from "../config.js";

declare module "fastify" {
  interface FastifyInstance {
    db: Database;
    sql: Sql;
  }
}

/**
 * One connection pool for the process, closed on shutdown.
 *
 * Decorating the instance (rather than importing a module-level singleton) keeps the app
 * testable: a test can build the app against a throwaway database without patching imports.
 */
const dbPlugin: FastifyPluginAsync = async (fastify) => {
  const { db, sql } = createDb(appConfig.DATABASE_URL, {
    max: appConfig.DATABASE_POOL_MAX,
    debug: !appConfig.isProduction && appConfig.LOG_LEVEL === "trace",
  });

  fastify.decorate("db", db);
  fastify.decorate("sql", sql);

  fastify.addHook("onClose", async () => {
    fastify.log.info("closing database pool");
    await sql.end({ timeout: 5 });
  });
};

export default fp(dbPlugin, { name: "db" });
