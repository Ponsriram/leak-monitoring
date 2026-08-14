# Architecture

How the system is put together, what each folder does, and how a leak travels from an onion
site to the dashboard.

To *run* it, see **[START.md](START.md)**. For rebuild history and known issues, see
**[ROADMAP.md](ROADMAP.md)**.

---

## The shape of it

Four moving parts plus two datastores.

```
                    ┌──────────────┐
   Tor network ────▶│  tor         │  SOCKS proxy, 3 circuit pools
                    │  (sidecar)   │  internal only — not published to the host
                    └──────┬───────┘
                           │ socks5
                    ┌──────▼───────┐
                    │  worker      │  Python. Crawls, extracts, loads.
                    │  services/   │  arq cron: hourly at :17
                    │    intel     │
                    └──────┬───────┘
                           │ writes
        ┌──────────────────▼──────────────────┐
        │  postgres        │  redis           │
        │  the record      │  arq job queue   │
        └──────────────────┬──────────────────┘
                           │ reads
                    ┌──────▼───────┐
                    │  api         │  Fastify. Auth, queries, aggregates.
                    │  apps/api    │  stateless — no background work
                    └──────┬───────┘
                           │ HTTP (proxied by nginx, same origin)
                    ┌──────▼───────┐
                    │  web         │  React SPA served by nginx
                    │  apps/web    │  :8080
                    └──────────────┘
```

**The API does no background work.** Crawling and alert matching live in the worker. That
separation is deliberate: the API can be restarted, scaled or redeployed at any moment
without interrupting a crawl, and a slow crawl can never block a dashboard request.

---

## Folders

```
leak-monitoring/
├── apps/
│   ├── api/            Fastify REST API (TypeScript)
│   └── web/            React dashboard (TypeScript + Vite)
├── packages/
│   └── db/             Drizzle schema + migrations — owns the database
├── services/
│   └── intel/          Python collection pipeline
├── infra/
│   ├── docker-compose.yml
│   └── tor/            Tor sidecar image + torrc
├── scripts/            backup-db.sh, smoke-api.sh
└── .github/workflows/  CI
```

npm workspaces tie `apps/*` and `packages/*` together; `services/intel` is a separate
Python project managed with `uv`.

### `packages/db` — the schema owns itself

Drizzle schema and versioned migrations. **This package is the single source of truth for
the database.** The API imports its generated types; the Python worker reads the same tables
with raw SQL but never migrates them.

| File | Purpose |
|---|---|
| `src/schema/leaks.ts` | The canonical leak entity |
| `src/schema/sources.ts` | Monitored sites + crawl health |
| `src/schema/crawls.ts` | `crawl_runs` and `raw_pages` — provenance |
| `src/schema/alerts.ts` | Alert rules and delivery events |
| `src/schema/auth.ts` | Better Auth's four tables |
| `src/seed.ts` | Idempotent demo data |
| `test/schema.test.ts` | Constraint tests against PGlite (real Postgres, in WASM) |

### `apps/api` — stateless HTTP

| File | Purpose |
|---|---|
| `src/config.ts` | Zod-validated env. **Refuses to boot on bad config.** |
| `src/app.ts` | Builds the Fastify instance, registers plugins and routes |
| `src/server.ts` | Boot + graceful shutdown |
| `src/auth.ts` | Better Auth configuration |
| `src/plugins/` | db pool, auth guard, error handler |
| `src/routes/` | leaks, sources, stats, alerts, health |

### `apps/web` — the dashboard

| Path | Purpose |
|---|---|
| `src/lib/api.ts` | **The only place that knows where the API is.** |
| `src/lib/queries.ts` | One typed hook per endpoint (TanStack Query) |
| `src/components/AppLayout.tsx` | Layout route — sidebar mounts once |
| `src/components/ProtectedRoute.tsx` | Auth gate, wraps the whole dashboard |
| `src/features/` | One folder per feature: auth, dashboard, leaks, sources, alerts |
| `src/styles/tokens.css` | Design tokens, light + dark |

### `services/intel` — collection

| Path | Purpose |
|---|---|
| `intel/cli.py` | `intel run / status / sources / extract-file` |
| `intel/pipeline.py` | fetch → hash → parse → extract → dedupe → upsert |
| `intel/collectors/` | `tor_http.py` (async httpx), `tor_browser.py` (Playwright) |
| `intel/extract/linker.py` | **Spans → discrete leaks.** The core logic. |
| `intel/extract/rules.py` | Default extractor. No ML required. |
| `intel/extract/gliner_extractor.py` | Zero-shot NER. Optional `ml` extra. |
| `intel/extract/normalize.py` | Dates → `timestamptz`, `"1.2 TB"` → bytes |
| `intel/models.py` | Pydantic `ExtractedLeak` — validates everything |
| `intel/storage.py` | The only module that speaks SQL |
| `intel/tasks.py` | arq worker + cron |
| `sources.yaml` | The monitored sites. Mounted, not baked in. |

---

## How a leak reaches the dashboard

```
1. SCHEDULE    arq cron fires hourly at :17 (or you run `intel run`)
2. FETCH       collector pulls the page over Tor
3. HASH        sha256 of the cleaned text
                 └── seen this hash before? STOP. Nothing downstream runs.
4. PARSE       selectolax → clean text
5. EXTRACT     extractor → labelled spans
6. LINK        linker groups spans into discrete leaks
7. NORMALIZE   dates → timestamptz, sizes → bytes, groups → slugs
8. VALIDATE    Pydantic ExtractedLeak, or it does not proceed
9. UPSERT      ON CONFLICT (dedupe_hash) DO UPDATE
                 ├── INSERT: first_seen_at set once, forever
                 └── UPDATE: last_seen_at advances; first_seen_at untouched
10. SERVE      API queries indexed columns; dashboard polls every 60s
```

**Step 3 is what makes repeat crawls cheap.** An unchanged page costs one fetch and stops.

**Step 9 is what makes the system correct.** The unique constraint on `dedupe_hash` is why
re-running never duplicates. `first_seen_at` is written once and never updated, which is what
makes "what's new since yesterday" answerable at all.

---

## Key design decisions

### Identity is `(actor_group, victim)` — never a timestamp

`dedupe_hash = sha256(actor_group | victim_domain-or-name)`.

Deliberately excludes anything clock-derived. Status, size and dates all change as a listing
progresses; the victim and the crew that took them do not. Two crews listing the same company
are two separate leak events, so the group is part of the key.

### `first_seen_at` vs `last_seen_at`

| Column | Written | Answers |
|---|---|---|
| `first_seen_at` | Once, on insert | "What's new since yesterday?" |
| `last_seen_at` | Every sighting | "Is this listing still up?" |
| `published_at` | From the page | "When did they claim to publish it?" |

`published_at` is a real `timestamptz`; `published_at_raw` keeps the original text so a bad
parse can be audited rather than guessed at.

### Alert matchers are typed, never regex

`match_kind` is one of `exact | domain | substring | actor_group`. There is no field where a
user can supply a pattern. Matching runs in SQL against indexed columns when a new leak
arrives — not on a timer.

`UNIQUE (alert_id, leak_id)` on `alert_events` makes delivery idempotent: a retry or a
duplicate queue message cannot notify the same person about the same leak twice.

### Extraction is pluggable

`RulesExtractor` is the default and needs no ML stack — the pipeline and its tests run on a
clean install. `GlinerExtractor` sits behind the `ml` extra (torch, ~2 GB) and is imported
lazily, so nothing loads torch unless you ask for it.

### One origin in the browser

Dev: Vite proxies `/api`. Production: nginx proxies `/api` to the API container. Either way
the browser talks to exactly one origin — no CORS preflight, and the session cookie stays
first-party. This is why no component contains a hostname.

---

## Data model

```
sources ──┬──< crawl_runs
          ├──< raw_pages
          └──< leaks >──< alert_events >──< alerts >── user
```

| Table | Holds |
|---|---|
| `sources` | Monitored sites, crawl cadence, health |
| `crawl_runs` | One row per attempt — provenance |
| `raw_pages` | Fetched text + `content_sha256` (the short-circuit) |
| `leaks` | The canonical entity |
| `alerts` | Typed match rules, owner-scoped |
| `alert_events` | Deliveries, unique per (alert, leak) |
| `user` / `session` / `account` / `verification` | Better Auth |

Deleting a source cascades to its `crawl_runs` and `raw_pages`, but `leaks.source_id` is
`ON DELETE SET NULL` — **pruning a dead site never destroys collected intelligence.**

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Database | PostgreSQL 18 | Real constraints; the original bug was a missing one |
| ORM | Drizzle | Generated types shared with the API; SQL stays visible |
| API | Fastify 5 | Schema validation and serialisation built in |
| Auth | Better Auth | Lucia is deprecated, Auth.js frozen |
| Frontend | React 19 + Vite 8 | Authenticated dashboard — no SSR needed |
| Server state | TanStack Query | Caching + `refetchInterval` for live data |
| Charts | Recharts | Themeable straight from CSS custom properties |
| Crawling | httpx + Playwright | Async, and one persistent browser context |
| Parsing | selectolax | ~10–30× faster than BeautifulSoup |
| Extraction | rules, or GLiNER | Zero-shot: no training data to maintain |
| Queue | arq | Async-native, cron built in, no separate beat |

---

## Known limitations

**Extraction quality on dense pages.** The linker assumes "a victim span opens a record,
following attributes attach to it". That holds for a page with a handful of listings; an
index page with hundreds loses its boundaries once flattened to text, and attributes attach
to the wrong victim. The fix is per-listing DOM segmentation. See ROADMAP.md.

**Sources decay.** Of 83 legacy URLs probed on 2026-08-14, 30 responded. Leak sites rotate
addresses and get seized. `consecutive_failures` surfaces this on the Sources page.

**No sign-up gate.** Anyone who can reach the app can create an account. Set
`disableSignUp: true` in `apps/api/src/auth.ts` before exposing it beyond localhost.

**Notifications are recorded, not sent.** `alert_events` rows are written with
`status: pending`; no SMTP delivery is wired up yet.
