/**
 * Environment configuration, validated once at boot.
 *
 * The point of this file is that the process refuses to start on a bad config rather than
 * failing later at an arbitrary request. The old server hardcoded its Mongo URI and SMTP
 * credentials in source and had no notion of environments at all.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { z } from "zod";

const here = path.dirname(fileURLToPath(import.meta.url));
// Repo-root .env, so the API, drizzle-kit and the worker all read one file.
loadDotenv({ path: path.resolve(here, "../../../.env") });

/**
 * Comma-separated env var -> string[].
 *
 * The default has to be applied to the *string* before the transform, not after it —
 * `.default()` on a pipe expects the output type, which would mean passing an array here.
 */
const csvList = (fallback: string) =>
  z
    .string()
    .default(fallback)
    .transform((value) =>
      value
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean),
    );

const envSchema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  LOG_LEVEL: z
    .enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"])
    .default("info"),

  API_HOST: z.string().default("0.0.0.0"),
  API_PORT: z.coerce.number().int().min(1).max(65535).default(5000),

  /**
   * Explicit allowlist. The old code shipped `cors()` with no options plus a literal
   * `Access-Control-Allow-Origin: *`, which is what made every endpoint readable by any page.
   */
  CORS_ORIGINS: csvList("http://localhost:5173"),

  DATABASE_URL: z.string().min(1, "DATABASE_URL is required"),
  DATABASE_POOL_MAX: z.coerce.number().int().min(1).max(100).default(10),

  AUTH_SECRET: z
    .string()
    .min(32, "AUTH_SECRET must be at least 32 characters — generate one, don't invent it"),
  AUTH_URL: z.url("AUTH_URL must be an absolute URL, e.g. http://localhost:5000"),
});

export type AppConfig = z.infer<typeof envSchema> & { isProduction: boolean };

function load(): AppConfig {
  const parsed = envSchema.safeParse(process.env);

  if (!parsed.success) {
    // Print every problem at once — a config fix loop that surfaces one error per restart
    // is miserable.
    const lines = parsed.error.issues.map(
      (issue) => `  • ${issue.path.join(".") || "(root)"}: ${issue.message}`,
    );
    console.error(
      ["Invalid environment configuration:", ...lines, "", "See .env.example."].join("\n"),
    );
    process.exit(1);
  }

  return { ...parsed.data, isProduction: parsed.data.NODE_ENV === "production" };
}

export const appConfig = load();
