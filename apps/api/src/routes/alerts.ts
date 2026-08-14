import { alertEvents, alerts, leaks } from "@leak/db";
import { and, count, desc, eq } from "drizzle-orm";
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { requireAuth } from "../plugins/auth.js";

/**
 * Alert rules, scoped to their owner.
 *
 * Two deliberate differences from the old implementation:
 *
 *   1. The matcher is a typed enum plus a plain value, never a user-supplied regex. The old
 *      code interpolated the keyword straight into `{ $regex: keyword }`, so `(a+)+$` was a
 *      denial of service against the whole collection.
 *   2. Every query is scoped by `ownerId`. The old endpoints had no notion of ownership,
 *      so any caller could read or trigger anyone's alerts.
 */

const matchKindSchema = z.enum(["exact", "domain", "substring", "actor_group"]);
const channelSchema = z.enum(["email", "webhook"]);

const createBody = z
  .object({
    name: z.string().min(1).max(120),
    matchKind: matchKindSchema,
    matchValue: z.string().min(2).max(200),
    channel: channelSchema,
    target: z.string().min(3).max(320),
    enabled: z.boolean().default(true),
  })
  .refine(
    (v) =>
      v.channel === "email"
        ? z.email().safeParse(v.target).success
        : z.url().safeParse(v.target).success,
    { message: "target must be an email address for email alerts, or a URL for webhooks", path: ["target"] },
  );

const alertSchema = z.object({
  id: z.number(),
  name: z.string(),
  matchKind: matchKindSchema,
  matchValue: z.string(),
  channel: channelSchema,
  target: z.string(),
  enabled: z.boolean(),
  createdAt: z.date(),
  triggerCount: z.number(),
});

export const alertRoutes: FastifyPluginAsyncZod = async (fastify) => {
  fastify.addHook("preHandler", requireAuth);

  fastify.get(
    "/api/alerts",
    {
      schema: {
        description: "The caller's alert rules, with how many times each has fired.",
        tags: ["alerts"],
        response: { 200: z.object({ data: z.array(alertSchema) }) },
      },
    },
    async (request) => {
      const ownerId = request.currentUser!.id;

      const rows = await fastify.db
        .select({
          id: alerts.id,
          name: alerts.name,
          matchKind: alerts.matchKind,
          matchValue: alerts.matchValue,
          channel: alerts.channel,
          target: alerts.target,
          enabled: alerts.enabled,
          createdAt: alerts.createdAt,
          triggerCount: fastify.db.$count(alertEvents, eq(alertEvents.alertId, alerts.id)),
        })
        .from(alerts)
        .where(eq(alerts.ownerId, ownerId))
        .orderBy(desc(alerts.createdAt));

      return { data: rows };
    },
  );

  fastify.post(
    "/api/alerts",
    {
      schema: {
        description: "Create an alert rule.",
        tags: ["alerts"],
        body: createBody,
        response: { 201: alertSchema },
      },
    },
    async (request, reply) => {
      const ownerId = request.currentUser!.id;
      const body = request.body;

      const [created] = await fastify.db
        .insert(alerts)
        .values({
          ownerId,
          name: body.name,
          matchKind: body.matchKind,
          // Normalised on the way in, so matching never depends on how the user typed it.
          matchValue: body.matchValue.trim().toLowerCase(),
          channel: body.channel,
          target: body.target.trim(),
          enabled: body.enabled,
        })
        .returning();

      return reply.status(201).send({ ...created!, triggerCount: 0 });
    },
  );

  fastify.patch(
    "/api/alerts/:id",
    {
      schema: {
        description: "Update an alert rule. Only the owner may do this.",
        tags: ["alerts"],
        params: z.object({ id: z.coerce.number().int().positive() }),
        body: z.object({
          name: z.string().min(1).max(120).optional(),
          enabled: z.boolean().optional(),
          matchValue: z.string().min(2).max(200).optional(),
        }),
        response: {
          200: alertSchema.omit({ triggerCount: true }),
          404: z.object({ error: z.string(), message: z.string() }),
        },
      },
    },
    async (request, reply) => {
      const ownerId = request.currentUser!.id;
      const body = request.body;

      const [updated] = await fastify.db
        .update(alerts)
        .set({
          ...(body.name !== undefined ? { name: body.name } : {}),
          ...(body.enabled !== undefined ? { enabled: body.enabled } : {}),
          ...(body.matchValue !== undefined
            ? { matchValue: body.matchValue.trim().toLowerCase() }
            : {}),
          updatedAt: new Date(),
        })
        // Ownership is part of the WHERE, not a separate check — there is no window where
        // a wrong id could touch someone else's row.
        .where(and(eq(alerts.id, request.params.id), eq(alerts.ownerId, ownerId)))
        .returning();

      if (!updated) {
        return reply
          .status(404)
          .send({ error: "not_found", message: "No such alert." });
      }
      return updated;
    },
  );

  fastify.delete(
    "/api/alerts/:id",
    {
      schema: {
        description: "Delete an alert rule. Only the owner may do this.",
        tags: ["alerts"],
        params: z.object({ id: z.coerce.number().int().positive() }),
        response: {
          204: z.null(),
          404: z.object({ error: z.string(), message: z.string() }),
        },
      },
    },
    async (request, reply) => {
      const ownerId = request.currentUser!.id;

      const deleted = await fastify.db
        .delete(alerts)
        .where(and(eq(alerts.id, request.params.id), eq(alerts.ownerId, ownerId)))
        .returning({ id: alerts.id });

      if (deleted.length === 0) {
        return reply.status(404).send({ error: "not_found", message: "No such alert." });
      }
      return reply.status(204).send(null);
    },
  );

  fastify.get(
    "/api/alerts/events",
    {
      schema: {
        description:
          "Recent alert deliveries for the caller. Replaces the old trigger counter, " +
          "which read from a collection nothing ever wrote to and so was always zero.",
        tags: ["alerts"],
        querystring: z.object({
          limit: z.coerce.number().int().min(1).max(100).default(25),
        }),
        response: {
          200: z.object({
            total: z.number(),
            data: z.array(
              z.object({
                id: z.number(),
                alertId: z.number(),
                alertName: z.string(),
                leakId: z.number(),
                victimName: z.string().nullable(),
                actorGroup: z.string(),
                matchedOn: z.string(),
                channel: channelSchema,
                status: z.enum(["pending", "sent", "failed"]),
                sentAt: z.date().nullable(),
                createdAt: z.date(),
              }),
            ),
          }),
        },
      },
    },
    async (request) => {
      const ownerId = request.currentUser!.id;

      const [rows, totalResult] = await Promise.all([
        fastify.db
          .select({
            id: alertEvents.id,
            alertId: alertEvents.alertId,
            alertName: alerts.name,
            leakId: alertEvents.leakId,
            victimName: leaks.victimName,
            actorGroup: leaks.actorGroup,
            matchedOn: alertEvents.matchedOn,
            channel: alertEvents.channel,
            status: alertEvents.status,
            sentAt: alertEvents.sentAt,
            createdAt: alertEvents.createdAt,
          })
          .from(alertEvents)
          .innerJoin(alerts, eq(alertEvents.alertId, alerts.id))
          .innerJoin(leaks, eq(alertEvents.leakId, leaks.id))
          .where(eq(alerts.ownerId, ownerId))
          .orderBy(desc(alertEvents.createdAt))
          .limit(request.query.limit),

        fastify.db
          .select({ value: count() })
          .from(alertEvents)
          .innerJoin(alerts, eq(alertEvents.alertId, alerts.id))
          .where(eq(alerts.ownerId, ownerId)),
      ]);

      return { total: totalResult[0]?.value ?? 0, data: rows };
    },
  );
};
