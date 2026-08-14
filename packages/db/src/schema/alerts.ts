import {
  bigint,
  boolean,
  index,
  pgEnum,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
} from "drizzle-orm/pg-core";
import { user } from "./auth.js";
import { leaks } from "./leaks.js";

/**
 * Typed matchers — deliberately NOT a regex.
 *
 * The old code passed the user's keyword straight into `{ $regex: keyword }`, unescaped, and
 * ran it against every document every five seconds. A keyword like `(a+)+$` was catastrophic
 * backtracking against the whole collection. Enumerating match kinds removes the whole class
 * of problem: none of these can be turned into a pathological pattern.
 */
export const matchKind = pgEnum("match_kind", [
  /** Whole-field equality, case-insensitive. */
  "exact",
  /** Victim domain equality, including subdomains. */
  "domain",
  /** Case-insensitive substring — the common case, implemented with a plain ILIKE. */
  "substring",
  /** Match on the ransomware group rather than the victim. */
  "actor_group",
]);

export const alertChannel = pgEnum("alert_channel", ["email", "webhook"]);

export const deliveryStatus = pgEnum("delivery_status", ["pending", "sent", "failed"]);

export const alerts = pgTable(
  "alerts",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    ownerId: text("owner_id")
      .notNull()
      .references(() => user.id, { onDelete: "cascade" }),

    name: text("name").notNull(),

    matchKind: matchKind("match_kind").notNull(),
    /** Stored lowercased and trimmed by the API so matching never depends on user formatting. */
    matchValue: text("match_value").notNull(),

    channel: alertChannel("channel").notNull(),
    /** Email address or webhook URL, validated at the route boundary before it lands here. */
    target: text("target").notNull(),

    enabled: boolean("enabled").notNull().default(true),

    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // The matcher's hot query: every enabled alert, when a new leak arrives.
    index("alerts_enabled_idx").on(t.enabled),
    index("alerts_owner_idx").on(t.ownerId),
  ],
);

/**
 * One row per (alert, leak) pair that fired. Replaces the old `sentAlerts` collection.
 *
 * The UNIQUE constraint is what makes delivery idempotent: a retry, a worker restart, or a
 * duplicate queue message cannot send the same person the same leak twice.
 */
export const alertEvents = pgTable(
  "alert_events",
  {
    id: bigint("id", { mode: "number" }).primaryKey().generatedAlwaysAsIdentity(),

    alertId: bigint("alert_id", { mode: "number" })
      .notNull()
      .references(() => alerts.id, { onDelete: "cascade" }),
    leakId: bigint("leak_id", { mode: "number" })
      .notNull()
      .references(() => leaks.id, { onDelete: "cascade" }),

    /** Which field actually matched, so a noisy alert can be diagnosed. */
    matchedOn: text("matched_on").notNull(),

    channel: alertChannel("channel").notNull(),
    target: text("target").notNull(),

    status: deliveryStatus("status").notNull().default("pending"),
    error: text("error"),
    attempts: bigint("attempts", { mode: "number" }).notNull().default(0),

    sentAt: timestamp("sent_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    uniqueIndex("alert_events_alert_leak_key").on(t.alertId, t.leakId),
    index("alert_events_created_at_idx").on(t.createdAt.desc()),
    index("alert_events_status_idx").on(t.status),
  ],
);

export type Alert = typeof alerts.$inferSelect;
export type NewAlert = typeof alerts.$inferInsert;
export type AlertEvent = typeof alertEvents.$inferSelect;
export type NewAlertEvent = typeof alertEvents.$inferInsert;
