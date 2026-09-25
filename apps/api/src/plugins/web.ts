import fastifyStatic from "@fastify/static";
import type { FastifyPluginAsync } from "fastify";
import { existsSync } from "node:fs";
import path from "node:path";
import { appConfig } from "../config.js";

/**
 * The dashboard, served by the API itself.
 *
 * In the container the built React app ships inside the API image and is served from the
 * same origin as `/api`, so the browser talks to one origin: no CORS preflight, and the
 * session cookie stays first-party. This replaced a separate nginx container that did nothing
 * but serve these files and proxy `/api` here.
 *
 * Off unless WEB_DIST_DIR is set. In development Vite serves the app on :5173 with hot reload
 * and proxies `/api` to this process instead.
 */
const webPlugin: FastifyPluginAsync = async (fastify) => {
  const root = appConfig.WEB_DIST_DIR;
  if (!root) return;

  // Fail at boot, not on the first page load, when the build is missing.
  if (!existsSync(path.join(root, "index.html"))) {
    throw new Error(`WEB_DIST_DIR has no index.html: ${root}`);
  }

  await fastify.register(fastifyStatic, {
    root,
    // One route per file, registered at boot — the build never changes inside the image.
    // The default catch-all route would claim `/*` and leave no room for the SPA fallback.
    wildcard: false,
    // The image build writes a .gz beside each asset; browsers that accept gzip get that.
    preCompressed: true,
    globIgnore: ["**/*.gz"],
    cacheControl: false,
    setHeaders: (reply, filePath) => {
      // Hashed asset filenames are immutable. index.html must never be cached, or users get
      // a stale shell pointing at assets that no longer exist.
      const immutable = filePath.split(path.sep).includes("assets");
      reply.header(
        "cache-control",
        immutable ? "public, max-age=31536000, immutable" : "no-cache, no-store, must-revalidate",
      );
    },
  });

  // Deep links such as /leaks or /incidents are routes inside the React app, not files, so a
  // refresh on an inner page must get index.html rather than a 404. API paths and missing
  // assets still 404: an unknown endpoint answering 200 with HTML would hide the mistake.
  fastify.get("/*", (request, reply) => {
    if (request.url.startsWith("/api/") || request.url.startsWith("/assets/")) {
      return reply.callNotFound();
    }
    return reply.sendFile("index.html");
  });
};

export default webPlugin;
