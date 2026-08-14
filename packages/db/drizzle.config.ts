import { config } from "dotenv";
import { defineConfig } from "drizzle-kit";

// Load the repo-root .env so drizzle-kit and the API read the same DATABASE_URL.
config({ path: "../../.env" });

const url = process.env.DATABASE_URL;
if (!url) {
  throw new Error(
    "DATABASE_URL is not set. Copy .env.example to .env at the repo root and fill it in.",
  );
}

export default defineConfig({
  schema: "./src/schema/index.ts",
  out: "./migrations",
  dialect: "postgresql",
  dbCredentials: { url },
  verbose: true,
  strict: true,
});
