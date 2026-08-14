import type { FastifyError, FastifyPluginAsync, FastifyReply, FastifyRequest } from "fastify";
import fp from "fastify-plugin";
import {
  hasZodFastifySchemaValidationErrors,
  isResponseSerializationError,
} from "fastify-type-provider-zod";
import { appConfig } from "../config.js";

/** The provider carries the underlying Zod issue in `params.issue`, but types it loosely. */
type ZodIssueLike = { path: PropertyKey[]; message: string };

/**
 * One error shape for the whole API.
 *
 * The old server did `res.status(500).send("Error fetching leaks: " + err.message)`, which
 * leaks driver internals to the client and gives the frontend nothing structured to branch on.
 */
const errorHandlerPlugin: FastifyPluginAsync = async (fastify) => {
  fastify.setErrorHandler(
    (error: FastifyError, request: FastifyRequest, reply: FastifyReply) => {
      // Request body/params failed validation — the client's fault, and safe to describe.
      if (hasZodFastifySchemaValidationErrors(error)) {
        return reply.status(400).send({
          error: "validation_error",
          message: "Request does not match the expected schema.",
          details: error.validation.map((entry) => {
            const issue = entry.params.issue as ZodIssueLike;
            return {
              path: issue.path.map(String).join("."),
              message: issue.message,
            };
          }),
          requestId: request.id,
        });
      }

      // We built a response that doesn't match our own declared schema. That's our bug —
      // never show the client its internals, but make it loud in the logs.
      if (isResponseSerializationError(error)) {
        request.log.error(
          { err: error, route: `${request.method} ${request.url}` },
          "response failed its own schema",
        );
        return reply.status(500).send({
          error: "internal_error",
          message: "The server produced a malformed response.",
          requestId: request.id,
        });
      }

      const status = error.statusCode ?? 500;

      if (status >= 500) {
        request.log.error({ err: error }, "unhandled error");
        return reply.status(status).send({
          error: "internal_error",
          // Only surface the real message outside production.
          message: appConfig.isProduction ? "Something went wrong." : error.message,
          requestId: request.id,
        });
      }

      request.log.warn({ err: error }, "request rejected");
      return reply.status(status).send({
        error: error.code ?? "request_error",
        message: error.message,
        requestId: request.id,
      });
    },
  );

  fastify.setNotFoundHandler((request, reply) => {
    reply.status(404).send({
      error: "not_found",
      message: `No route for ${request.method} ${request.url}`,
      requestId: request.id,
    });
  });
};

export default fp(errorHandlerPlugin, { name: "error-handler" });
