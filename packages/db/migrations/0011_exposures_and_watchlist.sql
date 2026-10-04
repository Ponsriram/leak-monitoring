CREATE TYPE "public"."exposure_kind" AS ENUM('credential_pair', 'password_hash', 'api_key', 'payment_card', 'private_key');--> statement-breakpoint
CREATE TYPE "public"."match_target" AS ENUM('leak', 'ioc', 'exposure');--> statement-breakpoint
CREATE TYPE "public"."watch_kind" AS ENUM('domain', 'keyword');--> statement-breakpoint
CREATE TABLE "exposures" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "exposures_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"kind" "exposure_kind" NOT NULL,
	"detector" text NOT NULL,
	"fingerprint" text NOT NULL,
	"preview" text NOT NULL,
	"email_domain" text,
	"confidence" integer NOT NULL,
	"source_id" bigint,
	"source_url" text,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "exposures_confidence_range" CHECK ("exposures"."confidence" between 0 and 100),
	CONSTRAINT "exposures_preview_present" CHECK (length(btrim("exposures"."preview")) > 0)
);
--> statement-breakpoint
CREATE TABLE "watchlist_entries" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "watchlist_entries_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"kind" "watch_kind" NOT NULL,
	"value" text NOT NULL,
	"label" text,
	"created_by" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"matched_through" timestamp with time zone,
	CONSTRAINT "watchlist_entries_value_present" CHECK (length(btrim("watchlist_entries"."value")) > 0)
);
--> statement-breakpoint
CREATE TABLE "watchlist_matches" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "watchlist_matches_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"entry_id" bigint NOT NULL,
	"target_type" "match_target" NOT NULL,
	"target_id" bigint NOT NULL,
	"matched_at" timestamp with time zone DEFAULT now() NOT NULL,
	"acknowledged_at" timestamp with time zone,
	"acknowledged_by" text
);
--> statement-breakpoint
ALTER TABLE "exposures" ADD CONSTRAINT "exposures_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "watchlist_entries" ADD CONSTRAINT "watchlist_entries_created_by_user_id_fk" FOREIGN KEY ("created_by") REFERENCES "public"."user"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "watchlist_matches" ADD CONSTRAINT "watchlist_matches_entry_id_watchlist_entries_id_fk" FOREIGN KEY ("entry_id") REFERENCES "public"."watchlist_entries"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "watchlist_matches" ADD CONSTRAINT "watchlist_matches_acknowledged_by_user_id_fk" FOREIGN KEY ("acknowledged_by") REFERENCES "public"."user"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "exposures_fingerprint_kind_key" ON "exposures" USING btree ("fingerprint","kind");--> statement-breakpoint
CREATE INDEX "exposures_first_seen_at_idx" ON "exposures" USING btree ("first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "exposures_kind_first_seen_idx" ON "exposures" USING btree ("kind","first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "exposures_email_domain_idx" ON "exposures" USING btree ("email_domain") WHERE "exposures"."email_domain" is not null;--> statement-breakpoint
CREATE INDEX "exposures_source_idx" ON "exposures" USING btree ("source_id");--> statement-breakpoint
CREATE UNIQUE INDEX "watchlist_entries_kind_value_key" ON "watchlist_entries" USING btree ("kind","value");--> statement-breakpoint
CREATE INDEX "watchlist_entries_unmatched_idx" ON "watchlist_entries" USING btree ("id") WHERE "watchlist_entries"."matched_through" is null;--> statement-breakpoint
CREATE UNIQUE INDEX "watchlist_matches_entry_target_key" ON "watchlist_matches" USING btree ("entry_id","target_type","target_id");--> statement-breakpoint
CREATE INDEX "watchlist_matches_matched_at_idx" ON "watchlist_matches" USING btree ("matched_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "watchlist_matches_unread_idx" ON "watchlist_matches" USING btree ("matched_at" DESC NULLS LAST) WHERE "watchlist_matches"."acknowledged_at" is null;