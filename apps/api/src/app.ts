import cors from "@fastify/cors";
import helmet from "@fastify/helmet";
import rateLimit from "@fastify/rate-limit";
import sensible from "@fastify/sensible";
import Fastify, { type FastifyInstance } from "fastify";
import {
  serializerCompiler,
  validatorCompiler,
  type ZodTypeProvider,
} from "fastify-type-provider-zod";
import { randomUUID } from "node:crypto";
import { appConfig } from "./config.js";
import authPlugin from "./plugins/auth.js";
import dbPlugin from "./plugins/db.js";
import errorHandler from "./plugins/error-handler.js";
import webPlugin from "./plugins/web.js";
import { crawlRoutes } from "./routes/crawl.js";
import { exposureRoutes } from "./routes/exposures.js";
import { healthRoutes } from "./routes/health.js";
import { incidentRoutes } from "./routes/incidents.js";
import { iocRoutes } from "./routes/iocs.js";
import { leakRoutes } from "./routes/leaks.js";
import { mobileRoutes } from "./routes/mobile.js";
import { searchRoutes } from "./routes/search.js";
import { sourceRoutes } from "./routes/sources.js";
import { statsRoutes } from "./routes/stats.js";
import { streamRoutes } from "./routes/stream.js";
import { watchlistRoutes } from "./routes/watchlist.js";

export async function buildApp(): Promise<FastifyInstance> {
  const app = Fastify({
    logger: {
      level: appConfig.LOG_LEVEL,
      // Human-readable locally, JSON in production so a log shipper can parse it.
      ...(appConfig.isProduction
        ? {}
        : {
            transport: {
              target: "pino-pretty",
              options: { translateTime: "HH:MM:ss", ignore: "pid,hostname" },
            },
          }),
      // Never log credentials or session cookies, in any environment.
      redact: {
        paths: [
          "req.headers.authorization",
          "req.headers.cookie",
          "res.headers['set-cookie']",
        ],
        remove: true,
      },
    },
    // A request id on every log line and every error response, so a user-reported failure
    // can be traced to its logs.
    genReqId: () => randomUUID(),
    trustProxy: appConfig.TRUST_PROXY,
    // Reject oversized bodies before they're parsed.
    bodyLimit: 1_048_576, // 1 MB
  }).withTypeProvider<ZodTypeProvider>();

  // Zod owns both validation and response serialization.
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  await app.register(errorHandler);
  await app.register(sensible);

  await app.register(helmet, {
    // Off, as it was when nginx served the app. A CSP for the dashboard needs its own pass
    // over what the React build loads; a guessed one would break pages silently.
    contentSecurityPolicy: false,
  });

  await app.register(cors, {
    origin: appConfig.CORS_ORIGINS,
    // Required for the session cookie to be sent cross-origin from the Vite dev server.
    credentials: true,
    methods: ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
  });

  await app.register(rateLimit, {
    max: 300,
    timeWindow: "1 minute",
    // Auth endpoints get their own, much tighter limit where they're defined.
    keyGenerator: (request) => request.ip,
  });

  await app.register(dbPlugin);
  await app.register(authPlugin);

  // --- routes ---
  // Each is registered in its own scope, so the `preHandler` guard inside the protected
  // ones cannot leak into the health checks.
  await app.register(healthRoutes);
  await app.register(leakRoutes);
  await app.register(statsRoutes);
  await app.register(sourceRoutes);
  await app.register(crawlRoutes);
  await app.register(incidentRoutes);
  await app.register(iocRoutes);
  await app.register(mobileRoutes);
  await app.register(exposureRoutes);
  await app.register(watchlistRoutes);
  await app.register(searchRoutes);
  await app.register(streamRoutes);

  // Last: its catch-all must not shadow anything above.
  await app.register(webPlugin);

  return app;
}
