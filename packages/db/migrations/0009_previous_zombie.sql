CREATE TABLE "mobile_numbers" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "mobile_numbers_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"number" text NOT NULL,
	"number_display" text NOT NULL,
	"number_raw" text NOT NULL,
	"region_code" text,
	"country" text,
	"line_type" text,
	"threat_types" text[] NOT NULL,
	"target_audience" text[] NOT NULL,
	"details" text NOT NULL,
	"source" text NOT NULL,
	"source_url" text NOT NULL,
	"author" text,
	"language" text,
	"reported_at" timestamp with time zone,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "mobile_numbers_threat_types_present" CHECK (cardinality("mobile_numbers"."threat_types") > 0),
	CONSTRAINT "mobile_numbers_target_audience_present" CHECK (cardinality("mobile_numbers"."target_audience") > 0),
	CONSTRAINT "mobile_numbers_details_present" CHECK (length(btrim("mobile_numbers"."details")) > 0)
);
--> statement-breakpoint
CREATE UNIQUE INDEX "mobile_numbers_number_source_key" ON "mobile_numbers" USING btree ("number","source_url");--> statement-breakpoint
CREATE INDEX "mobile_numbers_reported_at_idx" ON "mobile_numbers" USING btree ("reported_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "mobile_numbers_first_seen_at_idx" ON "mobile_numbers" USING btree ("first_seen_at" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "mobile_numbers_number_idx" ON "mobile_numbers" USING btree ("number");--> statement-breakpoint
CREATE INDEX "mobile_numbers_threat_types_idx" ON "mobile_numbers" USING gin ("threat_types");--> statement-breakpoint
CREATE INDEX "mobile_numbers_target_audience_idx" ON "mobile_numbers" USING gin ("target_audience");--> statement-breakpoint
CREATE INDEX "mobile_numbers_details_trgm_idx" ON "mobile_numbers" USING gin ("details" gin_trgm_ops);