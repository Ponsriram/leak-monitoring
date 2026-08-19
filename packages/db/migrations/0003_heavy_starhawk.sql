CREATE TYPE "public"."crawl_request_status" AS ENUM('queued', 'running', 'succeeded', 'failed', 'skipped');--> statement-breakpoint
CREATE TABLE "crawl_requests" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "crawl_requests_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_slug" text,
	"status" "crawl_request_status" DEFAULT 'queued' NOT NULL,
	"requested_by" text,
	"requested_at" timestamp with time zone DEFAULT now() NOT NULL,
	"started_at" timestamp with time zone,
	"finished_at" timestamp with time zone,
	"sources_crawled" integer DEFAULT 0 NOT NULL,
	"new_leaks" integer DEFAULT 0 NOT NULL,
	"updated_leaks" integer DEFAULT 0 NOT NULL,
	"failed_sources" integer DEFAULT 0 NOT NULL,
	"error" text
);
--> statement-breakpoint
CREATE INDEX "crawl_requests_pending_idx" ON "crawl_requests" USING btree ("status","requested_at");--> statement-breakpoint
CREATE INDEX "crawl_requests_requested_at_idx" ON "crawl_requests" USING btree ("requested_at" DESC NULLS LAST);