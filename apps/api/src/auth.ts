import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { createDb, account, session, user, verification } from "@leak/db";
import { appConfig } from "./config.js";

/**
 * Better Auth owns credentials, sessions and password hashing.
 *
 * Deliberately not hand-rolled. The old server stored plaintext passwords and compared them
 * with `findOne({ username, password })`; the replacement should be a maintained library, not
 * our own JWT plumbing. Better Auth hashes with scrypt by default and manages session
 * lifetime, rotation and revocation.
 *
 * Its own connection: auth runs outside the request lifecycle (the CLI, background session
 * cleanup), so it must not depend on a Fastify instance existing.
 */
const { db } = createDb(appConfig.DATABASE_URL, { max: 5 });

export const auth = betterAuth({
  secret: appConfig.AUTH_SECRET,
  baseURL: appConfig.AUTH_URL,
  basePath: "/api/auth",

  database: drizzleAdapter(db, {
    provider: "pg",
    // Explicit mapping so a rename in our schema file surfaces as a type error here
    // rather than a runtime "table not found".
    schema: { user, session, account, verification },
  }),

  emailAndPassword: {
    enabled: true,
    // No public sign-up flow for a threat-intel console — accounts are provisioned.
    // Flip this to false once you add an invite flow; leaving it true is what lets you
    // create the first account.
    disableSignUp: false,
    minPasswordLength: 12,
    requireEmailVerification: false,
  },

  session: {
    expiresIn: 60 * 60 * 24 * 7, // 7 days
    updateAge: 60 * 60 * 24, // refresh the expiry at most once a day
  },

  advanced: {
    defaultCookieAttributes: {
      httpOnly: true,
      sameSite: "strict",
      // Browsers reject Secure cookies over plain http://localhost, so only in production.
      secure: appConfig.isProduction,
    },
  },

  trustedOrigins: appConfig.CORS_ORIGINS,
});

export type AuthSession = typeof auth.$Infer.Session;
