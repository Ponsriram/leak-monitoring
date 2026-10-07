ALTER TABLE "crawl_cycles" ADD COLUMN "leaks_updated" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "crawl_cycles" ADD COLUMN "summary" jsonb;--> statement-breakpoint
ALTER TABLE "crawl_urls" ADD COLUMN "leaks_updated" integer DEFAULT 0 NOT NULL;