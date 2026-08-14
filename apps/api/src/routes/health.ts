import { sql } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";

/**
 * Liveness and readiness are deliberately different endpoints.
 *
 * `/healthz` answers "is the process up?" — an orchestrator uses it to decide whether to
 * restart. `/readyz` answers "can it serve traffic?" — it checks the database, so a pod with
 * a dead pool is pulled from the load balancer instead of serving errors.
 */
export const healthRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.get(
    "/healthz",
    {
      schema: {
        description: "Liveness probe. Does not touch the database.",
        tags: ["system"],
        response: {
          200: z.object({
            status: z.literal("ok"),
            uptimeSeconds: z.number(),
          }),
        },
      },
    },
    async () => ({ status: "ok" as const, uptimeSeconds: Math.floor(process.uptime()) }),
  );

  fastify.get(
    "/readyz",
    {
      schema: {
        description: "Readiness probe. Verifies the database is reachable.",
        tags: ["system"],
        response: {
          200: z.object({ status: z.literal("ready"), database: z.literal("up") }),
          503: z.object({ status: z.literal("degraded"), database: z.literal("down") }),
        },
      },
    },
    async (_request, reply) => {
      try {
        await fastify.db.execute(sql`select 1`);
        return { status: "ready" as const, database: "up" as const };
      } catch (error) {
        fastify.log.error({ err: error }, "readiness check failed");
        return reply
          .status(503)
          .send({ status: "degraded" as const, database: "down" as const });
      }
    },
  );
};
