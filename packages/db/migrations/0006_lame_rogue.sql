CREATE TYPE "public"."crawl_depth" AS ENUM('shallow', 'deep');--> statement-breakpoint
ALTER TABLE "sources" ADD COLUMN "deep_crawl_interval_seconds" integer DEFAULT 21600 NOT NULL;--> statement-breakpoint
ALTER TABLE "sources" ADD COLUMN "last_deep_crawl_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "crawl_runs" ADD COLUMN "depth" "crawl_depth" DEFAULT 'deep' NOT NULL;