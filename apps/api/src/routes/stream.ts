import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import postgres from "postgres";
import { z } from "zod";
import { appConfig } from "../config.js";
import { requireAuth } from "../plugins/auth.js";

/**
 * Live push, replacing the dashboard's 60-second poll.
 *
 * The chain is: the worker inserts a leak → the `leaks_notify_insert` trigger from migration
 * 0005 fires `pg_notify` → one dedicated connection here is LISTENing → every open browser
 * gets a server-sent event. A new leak reaches the screen about a second after it is
 * collected, instead of up to a minute later.
 *
 * SSE rather than WebSockets: this is strictly one-directional and there is nothing a client
 * would send back. SSE is a plain HTTP response, so it needs no protocol upgrade, carries the
 * session cookie exactly like every other request, and reconnects on its own — the browser
 * retries automatically, which is behaviour we would otherwise have to write and get wrong.
 *
 * Why a separate connection rather than the shared pool: a connection running LISTEN is
 * occupied for the lifetime of the process. Taking one from the pool would permanently
 * remove it from the pool's budget and, under a small pool, deadlock every query behind it.
 */

/** Postgres channels, matching the trigger names in migration 0005. */
const CHANNELS = ["leak_inserted", "hunt_changed"] as const;

/**
 * How often to send a comment line when nothing is happening.
 *
 * Leak sites publish in bursts, so an idle stream is the normal state for hours. Proxies and
 * load balancers close connections they believe are dead — nginx's default read timeout is
 * 60 seconds — so without this the stream would drop and reconnect all day. A `:` line is
 * the SSE spec's comment: it costs two bytes and the browser ignores it.
 */
const HEARTBEAT_MS = 25_000;

type StreamEvent = { event: string; data: unknown };

export const streamRoutes: FastifyPluginAsyncZod = async (fastify) => {
  /**
   * One LISTEN connection for the whole process, shared by every connected client.
   *
   * The alternative — a connection per browser tab — puts Postgres's connection limit
   * directly in the path of "how many analysts can have the dashboard open", which is not a
   * trade anyone would choose deliberately.
   */
  const listener = postgres(appConfig.DATABASE_URL, {
    max: 1,
    // This connection only ever waits for notifications; it must not be reaped as idle.
    idle_timeout: 0,
    connection: { application_name: "leak-api-listener" },
  });

  /** Every open response, and the function that writes one event to it. */
  const clients = new Set<(event: StreamEvent) => void>();

  function broadcast(event: StreamEvent) {
    for (const send of clients) {
      try {
        send(event);
      } catch (error) {
        // A client that has gone away without us noticing yet. The `close` handler will
        // remove it; failing to write to one must not stop the others receiving.
        fastify.log.debug({ err: error }, "sse: dropping write to a closed client");
      }
    }
  }

  const unlisten: Array<() => Promise<unknown>> = [];

  for (const channel of CHANNELS) {
    const subscription = await listener.listen(channel, (payload) => {
      let data: unknown;
      try {
        data = JSON.parse(payload);
      } catch {
        // The trigger builds its payload with `json_build_object`, so this should not
        // happen — but a malformed notification must not take down the listener for
        // everyone.
        fastify.log.warn({ channel, payload }, "sse: unparseable notification payload");
        return;
      }
      broadcast({ event: channel, data });
    });
    unlisten.push(subscription.unlisten);
  }

  fastify.addHook("onClose", async () => {
    await Promise.allSettled(unlisten.map((fn) => fn()));
    await listener.end({ timeout: 5 });
  });

  fastify.get(
    "/api/stream",
    {
      // Long-lived by design: the rate limiter counts connections, not the events on them.
      config: { rateLimit: { max: 30, timeWindow: "1 minute" } },
      schema: {
        description:
          "Server-sent events: `leak_inserted` when a new leak is collected, " +
          "`hunt_changed` when a hunt advances. Long-lived; the browser reconnects itself.",
        tags: ["stream"],
        response: { 200: z.string() },
      },
      preHandler: requireAuth,
    },
    async (request, reply) => {
      reply.raw.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        // nginx buffers proxied responses by default, which holds every event until the
        // buffer fills — an event stream that arrives in batches minutes late is worse than
        // no event stream, because it looks like it is working.
        "X-Accel-Buffering": "no",
      });

      const send = (payload: StreamEvent) => {
        reply.raw.write(`event: ${payload.event}\n`);
        reply.raw.write(`data: ${JSON.stringify(payload.data)}\n\n`);
      };

      clients.add(send);
      // Tell the client immediately that the stream is up, so the UI can show a live
      // indicator without waiting for the first leak — which may be hours away.
      send({ event: "connected", data: { at: new Date().toISOString() } });

      const heartbeat = setInterval(() => {
        try {
          reply.raw.write(": keep-alive\n\n");
        } catch {
          clearInterval(heartbeat);
        }
      }, HEARTBEAT_MS);

      const cleanup = () => {
        clearInterval(heartbeat);
        clients.delete(send);
        fastify.log.debug({ clients: clients.size }, "sse: client disconnected");
      };

      request.raw.on("close", cleanup);
      request.raw.on("error", cleanup);

      fastify.log.debug({ clients: clients.size }, "sse: client connected");

      // Never resolves. Returning would end the response, which is the one thing this
      // handler must not do — Fastify treats a returned value as the complete body.
      return reply;
    },
  );
};
