import { boolean, index, pgTable, text, timestamp, uniqueIndex } from "drizzle-orm/pg-core";

/**
 * Better Auth's core schema.
 *
 * These four tables are owned by Better Auth, not by us. Reconciled against
 * `npx @better-auth/cli generate` — re-run it after upgrading the library and diff again.
 * Do not rename columns to taste; the adapter resolves fields by these keys.
 *
 * Two deliberate departures from the generated output:
 *   1. `timestamptz` instead of `timestamp`. The generator emits timezone-naive columns;
 *      storing session expiry without a zone is asking for an off-by-hours bug.
 *   2. Extra `role` column on `user`, for authorisation in our own routes.
 *
 * Table name "user" is a reserved word in Postgres. Drizzle quotes identifiers, so it is safe,
 * and matching Better Auth's default naming avoids adapter configuration we'd otherwise have
 * to keep in sync.
 */

export const user = pgTable(
  "user",
  {
    id: text("id").primaryKey(),
    name: text("name").notNull(),
    email: text("email").notNull(),
    emailVerified: boolean("email_verified").notNull().default(false),
    image: text("image"),

    /**
     * Our own addition. Better Auth tolerates extra columns; this drives authorisation
     * in the API (an analyst can read, only an admin can change sources).
     */
    role: text("role").notNull().default("analyst"),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
  },
  // The old code had no unique index on email, so two accounts could share an address.
  (t) => [uniqueIndex("user_email_key").on(t.email)],
);

export const session = pgTable(
  "session",
  {
    id: text("id").primaryKey(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    token: text("token").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    ipAddress: text("ip_address"),
    userAgent: text("user_agent"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
  },
  (t) => [
    uniqueIndex("session_token_key").on(t.token),
    index("session_user_id_idx").on(t.userId),
  ],
);

export const account = pgTable(
  "account",
  {
    id: text("id").primaryKey(),
    userId: text("user_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),
    accountId: text("account_id").notNull(),
    providerId: text("provider_id").notNull(),

    accessToken: text("access_token"),
    refreshToken: text("refresh_token"),
    idToken: text("id_token"),
    accessTokenExpiresAt: timestamp("access_token_expires_at", { withTimezone: true }),
    refreshTokenExpiresAt: timestamp("refresh_token_expires_at", { withTimezone: true }),
    scope: text("scope"),

    /**
     * Password hash, written and verified by Better Auth (scrypt by default).
     * Never a plaintext password — which is exactly what the old `users` collection stored.
     */
    password: text("password"),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow()
      .$onUpdate(() => new Date()),
  },
  (t) => [index("account_user_id_idx").on(t.userId)],
);

export const verification = pgTable("verification", {
  id: text("id").primaryKey(),
  identifier: text("identifier").notNull(),
  value: text("value").notNull(),
  expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow()
    .$onUpdate(() => new Date()),
});

export type User = typeof user.$inferSelect;
export type Session = typeof session.$inferSelect;
