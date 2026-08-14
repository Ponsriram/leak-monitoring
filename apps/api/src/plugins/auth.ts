import type {
  FastifyPluginAsync,
  FastifyReply,
  FastifyRequest,
  preHandlerAsyncHookHandler,
} from "fastify";
import fp from "fastify-plugin";
import { fromNodeHeaders } from "better-auth/node";
import { auth } from "../auth.js";

type SessionUser = {
  id: string;
  email: string;
  name: string;
  role?: string;
};

declare module "fastify" {
  interface FastifyRequest {
    /** Populated by `requireAuth`. Null on unauthenticated routes. */
    currentUser: SessionUser | null;
  }
}

/**
 * Mounts Better Auth's handler and exposes a guard for our own routes.
 *
 * Better Auth speaks the Web Fetch API (Request in, Response out), so the bridge here is
 * mechanical — with one sharp edge worth calling out, see the set-cookie note below.
 */
const authPlugin: FastifyPluginAsync = async (fastify) => {
  fastify.decorateRequest("currentUser", null);

  fastify.route({
    method: ["GET", "POST"],
    url: "/api/auth/*",
    config: {
      // Credential endpoints get a much tighter budget than the global 300/min.
      // Brute-forcing a password should exhaust this long before it finds anything.
      rateLimit: { max: 20, timeWindow: "1 minute" },
    },
    handler: async (request, reply) => {
      const url = new URL(request.url, `http://${request.headers.host ?? "localhost"}`);

      const webRequest = new Request(url.toString(), {
        method: request.method,
        headers: fromNodeHeaders(request.headers),
        ...(request.body === undefined || request.method === "GET"
          ? {}
          : { body: JSON.stringify(request.body) }),
      });

      const response = await auth.handler(webRequest);

      reply.status(response.status);

      /**
       * `Headers.forEach` folds repeated set-cookie values into a single comma-joined
       * string, which silently corrupts multi-cookie responses (session + csrf). Undici
       * exposes `getSetCookie()` precisely for this — take those separately and skip
       * set-cookie in the general loop.
       */
      for (const cookie of response.headers.getSetCookie()) {
        reply.header("set-cookie", cookie);
      }
      response.headers.forEach((value, key) => {
        if (key.toLowerCase() !== "set-cookie") reply.header(key, value);
      });

      // Bypass Fastify's serializer: the body is already encoded by Better Auth.
      return reply.send(response.body ? await response.text() : null);
    },
  });
};

export default fp(authPlugin, { name: "auth" });

/**
 * Route guard. Attach as `preHandler` on anything that must not be public.
 *
 * The old app had no equivalent: `/dashboard` and every `/api` endpoint were reachable by
 * anyone who typed the URL.
 */
export const requireAuth: preHandlerAsyncHookHandler = async (
  request: FastifyRequest,
  reply: FastifyReply,
) => {
  const result = await auth.api.getSession({
    headers: fromNodeHeaders(request.headers),
  });

  if (!result?.user) {
    return reply.status(401).send({
      error: "unauthorized",
      message: "Authentication required.",
      requestId: request.id,
    });
  }

  request.currentUser = {
    id: result.user.id,
    email: result.user.email,
    name: result.user.name,
    role: (result.user as { role?: string }).role,
  };
};
