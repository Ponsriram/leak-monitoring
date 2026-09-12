-- Trigram matching, required by every `gin_trgm_ops` index below. Shipped with Postgres as
-- a contrib module, so this creates it rather than installing anything.
CREATE EXTENSION IF NOT EXISTS pg_trgm;--> statement-breakpoint
CREATE TYPE "public"."search_entity_type" AS ENUM('leak', 'source', 'domain');--> statement-breakpoint
CREATE TYPE "public"."site_status" AS ENUM('not_scanned', 'live', 'down', 'error');--> statement-breakpoint
CREATE TYPE "public"."hunt_finding_kind" AS ENUM('leak_match', 'raw_page_mention', 'registration', 'infrastructure', 'certificate', 'liveness');--> statement-breakpoint
CREATE TYPE "public"."hunt_status" AS ENUM('queued', 'running', 'succeeded', 'partial', 'failed');--> statement-breakpoint
CREATE TABLE "search_documents" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "search_documents_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"entity_type" "search_entity_type" NOT NULL,
	"entity_id" text NOT NULL,
	"title" text NOT NULL,
	"subtitle" text,
	"body" text,
	"tags" text[],
	"occurred_at" timestamp with time zone,
	"tsv" "tsvector" GENERATED ALWAYS AS (setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
          setweight(to_tsvector('english', coalesce(subtitle, '')), 'B') ||
          setweight(to_tsvector('english', coalesce(body, '')), 'C')) STORED,
	"indexed_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "domain_enrichment" (
	"domain" text PRIMARY KEY NOT NULL,
	"registrar" text,
	"whois_contacts" jsonb,
	"registered_at" timestamp with time zone,
	"expires_at" timestamp with time zone,
	"dns" jsonb,
	"status" "site_status" DEFAULT 'not_scanned' NOT NULL,
	"http_status" integer,
	"technologies" text[],
	"page_title" text,
	"error" text,
	"checked_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "hunt_findings" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "hunt_findings_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"job_id" bigint NOT NULL,
	"kind" "hunt_finding_kind" NOT NULL,
	"title" text NOT NULL,
	"detail" jsonb,
	"source_label" text NOT NULL,
	"occurred_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "hunt_jobs" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "hunt_jobs_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"query" text NOT NULL,
	"normalized_query" text NOT NULL,
	"target_domain" text,
	"status" "hunt_status" DEFAULT 'queued' NOT NULL,
	"requested_by" text,
	"requested_at" timestamp with time zone DEFAULT now() NOT NULL,
	"started_at" timestamp with time zone,
	"finished_at" timestamp with time zone,
	"findings_count" bigint DEFAULT 0 NOT NULL,
	"errors" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "hunt_findings" ADD CONSTRAINT "hunt_findings_job_id_hunt_jobs_id_fk" FOREIGN KEY ("job_id") REFERENCES "public"."hunt_jobs"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "search_documents_entity_key" ON "search_documents" USING btree ("entity_type","entity_id");--> statement-breakpoint
CREATE INDEX "search_documents_tsv_idx" ON "search_documents" USING gin ("tsv");--> statement-breakpoint
CREATE INDEX "search_documents_title_trgm_idx" ON "search_documents" USING gin ("title" gin_trgm_ops);--> statement-breakpoint
CREATE INDEX "search_documents_type_occurred_idx" ON "search_documents" USING btree ("entity_type","occurred_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "domain_enrichment_checked_at_idx" ON "domain_enrichment" USING btree ("checked_at");--> statement-breakpoint
CREATE INDEX "domain_enrichment_status_idx" ON "domain_enrichment" USING btree ("status");--> statement-breakpoint
CREATE INDEX "hunt_findings_job_kind_idx" ON "hunt_findings" USING btree ("job_id","kind");--> statement-breakpoint
CREATE INDEX "hunt_jobs_query_requested_idx" ON "hunt_jobs" USING btree ("normalized_query","requested_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "hunt_jobs_status_idx" ON "hunt_jobs" USING btree ("status","requested_at") WHERE "hunt_jobs"."status" in ('queued', 'running');--> statement-breakpoint
CREATE INDEX "raw_pages_text_fts_idx" ON "raw_pages" USING gin (to_tsvector('english', "text"));--> statement-breakpoint
CREATE INDEX "leaks_victim_name_trgm_idx" ON "leaks" USING gin ("victim_name" gin_trgm_ops) WHERE "leaks"."victim_name" is not null;--> statement-breakpoint
CREATE INDEX "leaks_victim_domain_trgm_idx" ON "leaks" USING gin ("victim_domain" gin_trgm_ops) WHERE "leaks"."victim_domain" is not null;
--> statement-breakpoint
-- Live push, instead of the dashboard polling every 60 seconds.
--
-- The API holds one dedicated Postgres connection LISTENing on this channel and forwards
-- what arrives to every open SSE client, so a new leak reaches the browser about a second
-- after the worker writes it. The alternative — every browser polling every table on a
-- timer — costs a full round of queries per client per minute whether or not anything
-- changed, and still shows a row up to a minute late.
--
-- AFTER INSERT, not INSERT OR UPDATE, and that distinction is the whole point: the pipeline
-- upserts on `dedupe_hash`, so an unchanged listing seen again on the next crawl fires an
-- UPDATE. Notifying on those would push a "new leak" event every hour for every listing
-- that is merely still up.
--
-- Fields are truncated because `pg_notify` hard-fails above 8000 bytes and a failed notify
-- aborts the transaction that inserted the leak. Losing a live update is acceptable; losing
-- the leak is not. The payload carries enough to render a feed row, and the client refetches
-- the table for anything more.
CREATE OR REPLACE FUNCTION notify_leak_inserted() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify(
    'leak_inserted',
    json_build_object(
      'id', NEW.id,
      'victimName', left(NEW.victim_name, 200),
      'victimDomain', left(NEW.victim_domain, 200),
      'victimCountry', NEW.victim_country,
      'victimSector', NEW.victim_sector,
      'actorGroup', left(NEW.actor_group, 100),
      'status', NEW.status,
      'firstSeenAt', NEW.first_seen_at
    )::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;--> statement-breakpoint
DROP TRIGGER IF EXISTS leaks_notify_insert ON "leaks";--> statement-breakpoint
CREATE TRIGGER leaks_notify_insert
  AFTER INSERT ON "leaks"
  FOR EACH ROW EXECUTE FUNCTION notify_leak_inserted();--> statement-breakpoint
-- Same channel pattern for hunts, so the results page streams findings as each enricher
-- returns rather than polling the job until it says it is done.
CREATE OR REPLACE FUNCTION notify_hunt_changed() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify(
    'hunt_changed',
    json_build_object('jobId', NEW.id, 'status', NEW.status, 'findings', NEW.findings_count)::text
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;--> statement-breakpoint
DROP TRIGGER IF EXISTS hunt_jobs_notify ON "hunt_jobs";--> statement-breakpoint
CREATE TRIGGER hunt_jobs_notify
  AFTER INSERT OR UPDATE OF status, findings_count ON "hunt_jobs"
  FOR EACH ROW EXECUTE FUNCTION notify_hunt_changed();
