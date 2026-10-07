CREATE TYPE "public"."crawl_cycle_status" AS ENUM('running', 'completed', 'failed', 'abandoned');--> statement-breakpoint
CREATE TYPE "public"."crawl_url_kind" AS ENUM('listing', 'link');--> statement-breakpoint
CREATE TYPE "public"."crawl_url_status" AS ENUM('queued', 'running', 'retry', 'succeeded', 'failed', 'skipped');--> statement-breakpoint
CREATE TABLE "crawl_cycles" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "crawl_cycles_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"status" "crawl_cycle_status" DEFAULT 'running' NOT NULL,
	"trigger" text NOT NULL,
	"request_id" bigint,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"heartbeat_at" timestamp with time zone DEFAULT now() NOT NULL,
	"completed_at" timestamp with time zone,
	"total_urls" integer DEFAULT 0 NOT NULL,
	"successful_urls" integer DEFAULT 0 NOT NULL,
	"failed_urls" integer DEFAULT 0 NOT NULL,
	"skipped_urls" integer DEFAULT 0 NOT NULL,
	"new_pages" integer DEFAULT 0 NOT NULL,
	"changed_pages" integer DEFAULT 0 NOT NULL,
	"unchanged_pages" integer DEFAULT 0 NOT NULL,
	"leaks_found" integer DEFAULT 0 NOT NULL,
	"duration_ms" bigint,
	"error" text
);
--> statement-breakpoint
CREATE TABLE "crawl_urls" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "crawl_urls_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_id" bigint NOT NULL,
	"cycle_id" bigint,
	"parent_id" bigint,
	"url" text NOT NULL,
	"url_normalized" text NOT NULL,
	"kind" "crawl_url_kind" NOT NULL,
	"page_no" integer,
	"depth" integer DEFAULT 0 NOT NULL,
	"status" "crawl_url_status" DEFAULT 'queued' NOT NULL,
	"result" text,
	"next_crawl_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_crawled_at" timestamp with time zone,
	"failure_count" integer DEFAULT 0 NOT NULL,
	"last_error" text,
	"content_sha256" text,
	"http_status" integer,
	"response_ms" integer,
	"attempt" integer DEFAULT 0 NOT NULL,
	"leased_until" timestamp with time zone,
	"leased_by" text,
	"leaks_found" integer DEFAULT 0 NOT NULL,
	"discovered_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "crawl_runs" ADD COLUMN "cycle_id" bigint;--> statement-breakpoint
ALTER TABLE "crawl_cycles" ADD CONSTRAINT "crawl_cycles_request_id_crawl_requests_id_fk" FOREIGN KEY ("request_id") REFERENCES "public"."crawl_requests"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "crawl_urls" ADD CONSTRAINT "crawl_urls_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "crawl_urls" ADD CONSTRAINT "crawl_urls_cycle_id_crawl_cycles_id_fk" FOREIGN KEY ("cycle_id") REFERENCES "public"."crawl_cycles"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "crawl_urls" ADD CONSTRAINT "crawl_urls_parent_id_crawl_urls_id_fk" FOREIGN KEY ("parent_id") REFERENCES "public"."crawl_urls"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "crawl_cycles_one_running" ON "crawl_cycles" USING btree ("status") WHERE "crawl_cycles"."status" = 'running';--> statement-breakpoint
CREATE INDEX "crawl_cycles_started_at_idx" ON "crawl_cycles" USING btree ("started_at" DESC NULLS LAST);--> statement-breakpoint
CREATE UNIQUE INDEX "crawl_urls_source_url_uq" ON "crawl_urls" USING btree ("source_id","url_normalized");--> statement-breakpoint
CREATE INDEX "crawl_urls_claim_idx" ON "crawl_urls" USING btree ("next_crawl_at","page_no") WHERE "crawl_urls"."status" in ('queued', 'retry');--> statement-breakpoint
CREATE INDEX "crawl_urls_leases_idx" ON "crawl_urls" USING btree ("leased_until") WHERE "crawl_urls"."status" = 'running';--> statement-breakpoint
CREATE INDEX "crawl_urls_cycle_idx" ON "crawl_urls" USING btree ("cycle_id","status");--> statement-breakpoint
ALTER TABLE "crawl_runs" ADD CONSTRAINT "crawl_runs_cycle_id_crawl_cycles_id_fk" FOREIGN KEY ("cycle_id") REFERENCES "public"."crawl_cycles"("id") ON DELETE set null ON UPDATE no action;