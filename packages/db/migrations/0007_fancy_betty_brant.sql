CREATE TYPE "public"."ioc_type" AS ENUM('ip', 'domain', 'url', 'file_hash', 'email');--> statement-breakpoint
CREATE TABLE "iocs" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "iocs_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"value" text NOT NULL,
	"ioc_type" "ioc_type" NOT NULL,
	"host" text,
	"tags" text[],
	"threat" text,
	"note" text,
	"confidence" integer,
	"feed" text NOT NULL,
	"feed_ref" text,
	"reporter" text,
	"reported_at" timestamp with time zone,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX "iocs_value_type_key" ON "iocs" USING btree ("value","ioc_type");--> statement-breakpoint
CREATE INDEX "iocs_reported_at_idx" ON "iocs" USING btree ("reported_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "iocs_first_seen_at_idx" ON "iocs" USING btree ("first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "iocs_type_reported_idx" ON "iocs" USING btree ("ioc_type","reported_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "iocs_host_idx" ON "iocs" USING btree ("host") WHERE "iocs"."host" is not null;--> statement-breakpoint
CREATE INDEX "iocs_tags_idx" ON "iocs" USING gin ("tags");--> statement-breakpoint
CREATE INDEX "iocs_value_trgm_idx" ON "iocs" USING gin ("value" gin_trgm_ops);