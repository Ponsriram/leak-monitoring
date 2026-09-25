ALTER TABLE "leaks" ADD COLUMN "summary" text;--> statement-breakpoint
ALTER TABLE "leaks" ADD COLUMN "incident_types" text[] DEFAULT '{}'::text[] NOT NULL;--> statement-breakpoint
CREATE INDEX "leaks_incident_types_idx" ON "leaks" USING gin ("incident_types");--> statement-breakpoint
-- Existing rows get the types their structured fields already guarantee, mirroring
-- `classify_fields` in services/intel/intel/extract/describe.py. Types that need the
-- listing's own words come from `intel backfill-descriptions`, which reads raw_pages.
UPDATE "leaks" SET "incident_types" = array_remove(ARRAY[
  CASE WHEN "leak_type" IS NOT NULL AND "leak_type" <> '' THEN "leak_type" END,
  CASE WHEN "leak_size_bytes" IS NOT NULL AND "leak_size_bytes" > 0 THEN 'data_breach' END,
  CASE WHEN "status" = 'published' THEN 'data_leak' END,
  CASE WHEN "status" = 'sold' THEN 'sale' END,
  CASE WHEN "status" IN ('countdown', 'negotiating') THEN 'extortion' END
]::text[], NULL);