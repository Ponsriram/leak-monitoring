CREATE TYPE "public"."mirror_status" AS ENUM('candidate', 'self_declared', 'approved', 'rejected');--> statement-breakpoint
ALTER TYPE "public"."leak_status" ADD VALUE 'negotiating' BEFORE 'unknown';--> statement-breakpoint
CREATE TABLE "source_mirrors" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "source_mirrors_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_id" bigint NOT NULL,
	"url" text NOT NULL,
	"onion_host" text NOT NULL,
	"discovered_from_url" text,
	"status" "mirror_status" DEFAULT 'candidate' NOT NULL,
	"times_seen" integer DEFAULT 1 NOT NULL,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_ok_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "sources" ADD COLUMN "active_url" text;--> statement-breakpoint
ALTER TABLE "source_mirrors" ADD CONSTRAINT "source_mirrors_source_id_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."sources"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "source_mirrors_source_host_key" ON "source_mirrors" USING btree ("source_id","onion_host");--> statement-breakpoint
CREATE INDEX "source_mirrors_status_idx" ON "source_mirrors" USING btree ("status");