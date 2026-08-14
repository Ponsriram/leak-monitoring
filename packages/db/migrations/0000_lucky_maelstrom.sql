CREATE TYPE "public"."collector_kind" AS ENUM('http', 'browser');--> statement-breakpoint
CREATE TYPE "public"."crawl_status" AS ENUM('running', 'succeeded', 'failed', 'partial');--> statement-breakpoint
CREATE TYPE "public"."leak_status" AS ENUM('published', 'countdown', 'sold', 'removed', 'unknown');--> statement-breakpoint
CREATE TYPE "public"."alert_channel" AS ENUM('email', 'webhook');--> statement-breakpoint
CREATE TYPE "public"."delivery_status" AS ENUM('pending', 'sent', 'failed');--> statement-breakpoint
CREATE TYPE "public"."match_kind" AS ENUM('exact', 'domain', 'substring', 'actor_group');--> statement-breakpoint
CREATE TABLE "account" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"account_id" text NOT NULL,
	"provider_id" text NOT NULL,
	"access_token" text,
	"refresh_token" text,
	"id_token" text,
	"access_token_expires_at" timestamp with time zone,
	"refresh_token_expires_at" timestamp with time zone,
	"scope" text,
	"password" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "session" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"token" text NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"ip_address" text,
	"user_agent" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "user" (
	"id" text PRIMARY KEY NOT NULL,
	"name" text NOT NULL,
	"email" text NOT NULL,
	"email_verified" boolean DEFAULT false NOT NULL,
	"image" text,
	"role" text DEFAULT 'analyst' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "verification" (
	"id" text PRIMARY KEY NOT NULL,
	"identifier" text NOT NULL,
	"value" text NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "sources" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "sources_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"slug" text NOT NULL,
	"name" text NOT NULL,
	"base_url" text NOT NULL,
	"collector" "collector_kind" DEFAULT 'http' NOT NULL,
	"pagination_style" text DEFAULT 'none' NOT NULL,
	"max_pages" integer DEFAULT 10 NOT NULL,
	"crawl_interval_seconds" integer DEFAULT 3600 NOT NULL,
	"request_delay_seconds" integer DEFAULT 10 NOT NULL,
	"enabled" boolean DEFAULT true NOT NULL,
	"last_crawl_at" timestamp with time zone,
	"last_success_at" timestamp with time zone,
	"consecutive_failures" integer DEFAULT 0 NOT NULL,
	"notes" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "crawl_runs" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "crawl_runs_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_id" bigint NOT NULL,
	"status" "crawl_status" DEFAULT 'running' NOT NULL,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"finished_at" timestamp with time zone,
	"pages_fetched" integer DEFAULT 0 NOT NULL,
	"pages_changed" integer DEFAULT 0 NOT NULL,
	"bytes_fetched" bigint DEFAULT 0 NOT NULL,
	"error" text
);
--> statement-breakpoint
CREATE TABLE "raw_pages" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "raw_pages_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_id" bigint NOT NULL,
	"crawl_run_id" bigint,
	"url" text NOT NULL,
	"page_no" integer DEFAULT 1 NOT NULL,
	"content_sha256" text NOT NULL,
	"text" text NOT NULL,
	"byte_size" integer DEFAULT 0 NOT NULL,
	"fetched_at" timestamp with time zone DEFAULT now() NOT NULL,
	"extracted_at" timestamp with time zone
);
--> statement-breakpoint
CREATE TABLE "leaks" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "leaks_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"dedupe_hash" text NOT NULL,
	"victim_name" text,
	"victim_domain" text,
	"victim_country" text,
	"victim_sector" text,
	"actor_group" text NOT NULL,
	"source_id" bigint,
	"source_url" text,
	"published_at" timestamp with time zone,
	"published_at_raw" text,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"status" "leak_status" DEFAULT 'unknown' NOT NULL,
	"leak_type" text DEFAULT 'ransomware' NOT NULL,
	"leak_size_bytes" bigint,
	"extraction" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "alert_events" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "alert_events_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"alert_id" bigint NOT NULL,
	"leak_id" bigint NOT NULL,
	"matched_on" text NOT NULL,
	"channel" "alert_channel" NOT NULL,
	"target" text NOT NULL,
	"status" "delivery_status" DEFAULT 'pending' NOT NULL,
	"error" text,
	"attempts" bigint DEFAULT 0 NOT NULL,
	"sent_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "alerts" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "alerts_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"owner_id" text NOT NULL,
	"name" text NOT NULL,
	"match_kind" "match_kind" NOT NULL,
	"match_value" text NOT NULL,
	"channel" "alert_channel" NOT NULL,
	"target" text NOT NULL,
	"enabled" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "account" ADD CONSTRAINT "account_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "session" ADD CONSTRAINT "session_user_id_user_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "crawl_runs" ADD CONSTRAINT "crawl_runs_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "raw_pages" ADD CONSTRAINT "raw_pages_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "raw_pages" ADD CONSTRAINT "raw_pages_crawl_run_id_crawl_runs_id_fk" FOREIGN KEY ("crawl_run_id") REFERENCES "public"."crawl_runs"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "leaks" ADD CONSTRAINT "leaks_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "alert_events" ADD CONSTRAINT "alert_events_alert_id_alerts_id_fk" FOREIGN KEY ("alert_id") REFERENCES "public"."alerts"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "alert_events" ADD CONSTRAINT "alert_events_leak_id_leaks_id_fk" FOREIGN KEY ("leak_id") REFERENCES "public"."leaks"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "alerts" ADD CONSTRAINT "alerts_owner_id_user_id_fk" FOREIGN KEY ("owner_id") REFERENCES "public"."user"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "session_token_key" ON "session" USING btree ("token");--> statement-breakpoint
CREATE UNIQUE INDEX "user_email_key" ON "user" USING btree ("email");--> statement-breakpoint
CREATE UNIQUE INDEX "sources_slug_key" ON "sources" USING btree ("slug");--> statement-breakpoint
CREATE INDEX "sources_enabled_last_crawl_idx" ON "sources" USING btree ("enabled","last_crawl_at");--> statement-breakpoint
CREATE INDEX "crawl_runs_source_started_idx" ON "crawl_runs" USING btree ("source_id","started_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "raw_pages_source_hash_idx" ON "raw_pages" USING btree ("source_id","content_sha256");--> statement-breakpoint
CREATE INDEX "raw_pages_fetched_at_idx" ON "raw_pages" USING btree ("fetched_at");--> statement-breakpoint
CREATE INDEX "raw_pages_pending_extract_idx" ON "raw_pages" USING btree ("extracted_at");--> statement-breakpoint
CREATE UNIQUE INDEX "leaks_dedupe_hash_key" ON "leaks" USING btree ("dedupe_hash");--> statement-breakpoint
CREATE INDEX "leaks_first_seen_at_idx" ON "leaks" USING btree ("first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "leaks_actor_group_first_seen_idx" ON "leaks" USING btree ("actor_group","first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "leaks_published_at_idx" ON "leaks" USING btree ("published_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "leaks_source_idx" ON "leaks" USING btree ("source_id");--> statement-breakpoint
CREATE INDEX "leaks_victim_search_idx" ON "leaks" USING gin (to_tsvector('english', coalesce("victim_name", '') || ' ' || coalesce("victim_domain", '')));--> statement-breakpoint
CREATE UNIQUE INDEX "alert_events_alert_leak_key" ON "alert_events" USING btree ("alert_id","leak_id");--> statement-breakpoint
CREATE INDEX "alert_events_created_at_idx" ON "alert_events" USING btree ("created_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "alert_events_status_idx" ON "alert_events" USING btree ("status");--> statement-breakpoint
CREATE INDEX "alerts_enabled_idx" ON "alerts" USING btree ("enabled");--> statement-breakpoint
CREATE INDEX "alerts_owner_idx" ON "alerts" USING btree ("owner_id");