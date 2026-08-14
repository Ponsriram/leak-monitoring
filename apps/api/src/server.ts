import { buildApp } from "./app.js";
import { appConfig } from "./config.js";

const app = await buildApp();

/**
 * Graceful shutdown.
 *
 * Fastify's `close()` stops accepting connections, drains in-flight requests, then runs
 * onClose hooks — which is where the database pool is released. The old server had none of
 * this and relied on the process being killed.
 */
let shuttingDown = false;

async function shutdown(signal: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;

  app.log.info({ signal }, "shutting down");

  // Don't hang forever on a wedged connection.
  const timer = setTimeout(() => {
    app.log.error("shutdown timed out after 10s, forcing exit");
    process.exit(1);
  }, 10_000);
  timer.unref();

  try {
    await app.close();
    app.log.info("shutdown complete");
    process.exit(0);
  } catch (error) {
    app.log.error({ err: error }, "error during shutdown");
    process.exit(1);
  }
}

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => void shutdown(signal));
}

process.on("unhandledRejection", (reason) => {
  app.log.fatal({ err: reason }, "unhandled promise rejection");
  void shutdown("unhandledRejection");
});

try {
  await app.listen({ host: appConfig.API_HOST, port: appConfig.API_PORT });
} catch (error) {
  app.log.fatal({ err: error }, "failed to start");
  process.exit(1);
}
