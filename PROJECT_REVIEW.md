# Ransomware Leak-Site Monitoring — Project Review Document

*A complete walk-through of what the system is, how it is built, and how every process
works — written for a technical review.*

---

## Table of contents

1. [What the project is](#1-what-the-project-is)
2. [Tools and technologies used](#2-tools-and-technologies-used)
3. [High-level architecture](#3-high-level-architecture)
4. [Repository / folder structure](#4-repository--folder-structure)
5. [The database — the single source of truth](#5-the-database--the-single-source-of-truth)
6. [How crawling works](#6-how-crawling-works)
7. [How it finds "leaked data" (extraction)](#7-how-it-finds-leaked-data-extraction)
8. [Deduplication and storage](#8-deduplication-and-storage)
9. [How requests and communication happen](#9-how-requests-and-communication-happen)
10. [Scheduling and the worker](#10-scheduling-and-the-worker)
11. [Alerting](#11-alerting)
12. [End-to-end: the life of a leak](#12-end-to-end-the-life-of-a-leak)
13. [Security and safety design](#13-security-and-safety-design)
14. [Key design decisions](#14-key-design-decisions)
15. [Known limitations](#15-known-limitations)
16. [How to run it](#16-how-to-run-it)

---

## 1. What the project is

**Leak-monitoring** is a *threat-intelligence platform* that automatically watches ransomware
gangs' "leak sites" on the dark web, extracts structured records of the victims those gangs
have published, stores them in a database, and presents them on a live analyst dashboard with
search, filtering, a geographic map, and alerting.

In plain terms: ransomware crews run websites (hosted as Tor `.onion` hidden services) where
they name the companies they have hacked and threaten to publish stolen data. This system
visits those sites continuously, reads them, turns the messy HTML into clean rows of "this
gang leaked this company on this date", and lets an analyst monitor it all from a web app.

It is built as a **monorepo** with four running services and two datastores, and ships with
**41 real leak-site sources** already configured (all disabled by default for safety).

---

## 2. Tools and technologies used

| Layer | Technology | Why it was chosen |
|---|---|---|
| **Database** | PostgreSQL 18 | Real constraints enforce correctness at the storage layer |
| **ORM / schema** | Drizzle (TypeScript) | Generated types shared with the API; SQL stays visible |
| **Backend API** | Fastify 5 (Node 22, TypeScript) | Built-in schema validation + fast JSON serialization |
| **API validation** | Zod + `fastify-type-provider-zod` | One schema validates input *and* serializes output |
| **Authentication** | Better Auth | Maintained library; scrypt hashing, session rotation |
| **Frontend** | React 19 + Vite 8 (TypeScript) | Authenticated SPA — no SSR needed |
| **Server state** | TanStack Query | Caching + `refetchInterval` for live data |
| **Charts** | Recharts | Themeable directly from CSS custom properties |
| **Collection pipeline** | Python 3.12 | Strong NLP/extraction ecosystem |
| **HTTP crawling** | httpx (async, SOCKS) | Async — enables crawling many sources at once |
| **Browser crawling** | Playwright (Firefox) | For sites whose listings render via JavaScript |
| **HTML parsing** | selectolax | Very fast HTML-to-text parsing |
| **Extraction (default)** | Rule-based regex (`RulesExtractor`) | Zero ML dependency; works on a clean install |
| **Extraction (optional)** | GLiNER (zero-shot NER) | Names orgs in prose; behind an `ml` extra (~2 GB) |
| **Data validation** | Pydantic 2 | Nothing reaches the DB without passing validation |
| **DB access (Python)** | asyncpg (raw SQL) | Keeps the "Drizzle owns the schema" boundary obvious |
| **Job queue / scheduler** | arq (async Redis queue) | Async-native, cron built in, no separate beat process |
| **Anonymity network** | Tor (client-only sidecar) | The only way to reach `.onion` leak sites |
| **Containerization** | Docker Compose | Six-container stack, one command to start |
| **Reverse proxy** | nginx (in the web container) | Serves the SPA and proxies `/api` → API (one origin) |
| **CI** | GitHub Actions | Typecheck, build, and schema-constraint tests |
| **Python tooling** | uv (deps), ruff (lint), pytest | Fast, modern Python toolchain |

---

## 3. High-level architecture

The system is **four moving parts plus two datastores**. A core architectural decision is that
**the API does no background work** — all crawling and alert-matching lives in a separate
Python worker.

```
                    ┌──────────────┐
   Tor network ────▶│  tor         │  SOCKS proxy, 3 independent circuit pools
                    │  (sidecar)   │  internal only — never exposed to the host
                    └──────┬───────┘
                           │ socks5
                    ┌──────▼───────┐
                    │  worker      │  Python. Crawls, extracts, loads, matches alerts.
                    │  services/   │  arq scheduler: sweeps every 5 min, drains every 10s
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
                    │  web         │  React SPA served by nginx  → :8080
                    │  apps/web    │
                    └──────────────┘
```

**Why this separation?** The API can be restarted, scaled, or redeployed at any moment without
interrupting a crawl, and a slow crawl over Tor can never block a dashboard request. The two
sides share a database and nothing else.

The six Docker containers (`npm run infra:up:full`):

1. **postgres** — the canonical record
2. **redis** — the arq job queue (persisted so a restart doesn't drop scheduled crawls)
3. **tor** — client-only Tor daemon exposing three SOCKS ports (circuit pools)
4. **worker** — the Python collection pipeline
5. **api** — the Fastify REST API (internal; reached through nginx)
6. **web** — nginx serving the React app and proxying `/api`

---

## 4. Repository / folder structure

The project is an **npm-workspaces monorepo** tying together `apps/*` and `packages/*`, with
the Python service (`services/intel`) as a separate `uv`-managed project.

```
leak-monitoring/
├── apps/
│   ├── api/            Fastify REST API (TypeScript)
│   │   └── src/
│   │       ├── config.ts        Zod-validated env — refuses to boot on bad config
│   │       ├── app.ts           Builds the Fastify instance, registers everything
│   │       ├── server.ts        Boot + graceful shutdown
│   │       ├── auth.ts          Better Auth configuration
│   │       ├── plugins/         db pool, auth guard, error handler
│   │       └── routes/          leaks, sources, stats, alerts, crawl, health
│   └── web/            React dashboard (TypeScript + Vite)
│       └── src/
│           ├── lib/api.ts       The ONLY place that knows where the API is
│           ├── lib/queries.ts   One typed hook per endpoint (TanStack Query)
│           ├── components/      AppLayout, ProtectedRoute, chips, states
│           └── features/        auth, dashboard, leaks, sources, alerts, map
├── packages/
│   └── db/             Drizzle schema + migrations — OWNS the database
│       ├── src/schema/ leaks, sources, crawls, alerts, auth
│       └── test/       Constraint tests against PGlite (real Postgres in WASM)
├── services/
│   └── intel/          Python collection pipeline
│       ├── intel/
│       │   ├── cli.py              intel run / status / sources / extract-file
│       │   ├── pipeline.py         fetch → hash → parse → extract → dedupe → upsert
│       │   ├── scheduling.py       page "waves" (galloping search)
│       │   ├── collectors/         tor_http.py (httpx), tor_browser.py (Playwright)
│       │   ├── extract/            rules.py, linker.py, gazetteer.py, normalize.py, gliner
│       │   ├── models.py           Pydantic ExtractedLeak — validates everything
│       │   ├── storage.py          The only module that speaks SQL
│       │   └── tasks.py            arq worker: sweep, drain, alert matching
│       └── sources.yaml            The 41 monitored sites (mounted, not baked in)
├── infra/
│   ├── docker-compose.yml          Six-service stack, "full" profile
│   └── tor/                        Tor sidecar image + torrc
├── scripts/                        backup-db.sh, smoke-api.sh
└── .github/workflows/              CI
```

**Ownership boundary:** `packages/db` is the *single source of truth for the database schema*.
The API imports its generated TypeScript types; the Python worker reads the same tables with
raw SQL but **never migrates them**. Migrations only ever come from Drizzle.

---

## 5. The database — the single source of truth

The whole system's correctness rests on the database schema. The core entity is `leaks`.

### Data model

```
sources ──┬──< crawl_runs
          ├──< raw_pages
          └──< leaks >──< alert_events >──< alerts >── user

crawl_requests   (standalone — the API writes, the worker claims)
```

| Table | Holds |
|---|---|
| `sources` | Monitored sites, crawl cadence, health (`consecutive_failures`) |
| `crawl_runs` | One row per crawl attempt — provenance |
| `crawl_requests` | Syncs asked for by a person; the API↔worker handoff |
| `raw_pages` | Fetched page text + `content_sha256` (the short-circuit) |
| `leaks` | **The canonical entity** — one victim listing |
| `alerts` | Typed match rules, owned by a user |
| `alert_events` | Deliveries, unique per (alert, leak) |
| `user` / `session` / `account` / `verification` | Better Auth's four tables |

### The three load-bearing columns on `leaks`

```
dedupe_hash   UNIQUE — so the loader can upsert instead of creating duplicates.
              = sha256(actor_group | victim_domain-or-name)
published_at  a real timestamptz — so date-range charts work correctly.
first_seen_at set on INSERT only — this is what makes "new since yesterday" a real question.
```

### first_seen vs last_seen vs published

| Column | Written | Answers |
|---|---|---|
| `first_seen_at` | Once, on insert | "What's new since yesterday?" |
| `last_seen_at` | Every sighting | "Is this listing still up?" |
| `published_at` | From the page text | "When did they *claim* to publish it?" |

### Indexing

The schema carries deliberate indexes for the exact dashboard queries: `first_seen_at DESC`
(default ordering), a composite `(actor_group, first_seen_at)`, `published_at` for the
time-series chart, **partial** indexes on `victim_country`/`victim_sector` (which are mostly
null, so the index skips the nulls), and a **GIN full-text index** over victim name + domain
so the search box hits an index instead of scanning every row.

### Cascade rules

Deleting a source cascades to its `crawl_runs` and `raw_pages`, but `leaks.source_id` is
`ON DELETE SET NULL` — **pruning a dead leak site never destroys the intelligence collected
from it.**

---

## 6. How crawling works

Crawling lives entirely in the Python worker (`services/intel`). Here is the full mechanism.

### 6.1 Reaching the sites — Tor

Leak sites are `.onion` Tor hidden services; they cannot be reached over the normal internet.
The `tor` container runs a **client-only** Tor daemon (`ClientOnly 1`, `ExitRelay 0`,
`ORPort 0`) exposing **three SOCKS ports** (9050, 9051, 9052). Each port is an independent
circuit pool, and the crawler round-robins between them, which triples the effective
circuit-rotation rate. Circuits rotate every 3 minutes (`MaxCircuitDirtiness 180`) so no
single relay observes much of the crawl pattern.

The Tor port is **never published to the host** — only containers on the Docker network can
reach it, so nothing else on the machine can accidentally route through it.

### 6.2 The two collectors

Each source declares a `collector` in `sources.yaml`:

- **`http`** (`collectors/tor_http.py`) — one async httpx request through the Tor SOCKS proxy.
  Fast, no JavaScript. This is what **nearly all** sources use. It sends realistic Firefox
  headers because a few sites refuse non-browser clients.
- **`browser`** (`collectors/tor_browser.py`) — a Playwright Firefox instance that waits on
  page lifecycle events. Used only for the handful of sites whose listings genuinely render
  client-side (they return empty over plain HTTP).

The HTTP collector has careful retry logic: only *retryable* statuses (408, 425, 429, 5xx) are
retried with **exponential backoff long enough for a Tor circuit to actually rebuild** (a 403,
by contrast, means "blocked on purpose" and is not retried — the fix is switching that source
to `browser`).

### 6.3 The clever part — "galloping wave" pagination

The single biggest cost in a crawl is wall-clock time: **one page fetch over Tor is 10–30
seconds**, and everything after it (parsing, extraction) is milliseconds.

The obstacle to just fetching all pages at once is that **you don't know how many pages there
are** — a listing "ends" when a page comes back empty, and you only learn that *after* fetching.

The solution (`scheduling.py → page_waves`) is a **galloping/exponential search**: fetch pages
in waves whose size doubles, and stop at the first wave that hits the end.

```python
>>> list(page_waves(10, width=4))
[[1], [2, 3, 4, 5], [6, 7, 8, 9, 10]]
```

- **Page 1 is always fetched alone, first.** It decides whether the source is reachable,
  whether to fail over to a mirror, and whether what came back is a real listing or a JS
  challenge — all of which change the address the other pages would be fetched from.
- Then waves double: `width`, `2·width`, `4·width`… capped at 16 requests per wave (so a
  doubling burst never becomes a 64-request hammer on a small site).
- Reaching page P costs **O(log P)** sequential rounds instead of O(P), and because only the
  last wave can overshoot, no more than ~2P pages are ever requested.

Pages within a wave are fetched **concurrently** but staggered slightly so the site still sees
requests arrive at its configured polite rate. A run-wide semaphore (`CRAWL_MAX_INFLIGHT` /
fetch budget) caps total simultaneous fetches so per-source and per-page concurrency don't
multiply and overwhelm Tor.

### 6.4 The content short-circuit

Every fetched page's cleaned text is hashed with SHA-256 (`content_sha256` on `raw_pages`).
**If that hash has been seen before for this source, everything downstream stops** — no
parsing, no extraction, no database writes. This is what makes repeat crawls of an unchanged
site nearly free.

### 6.5 Failure honesty

A lot of care went into *not lying about success*:

- A page that fetches but returns fewer than 50 characters of text is a challenge page or JS
  shell, **not** a healthy crawl. On page 1 this is recorded as a **failure** with a hint to
  switch to the browser collector.
- If a crawl is **cancelled** (worker shutdown / job timeout), it is recorded as failed and
  re-raised — not silently written as a successful crawl of zero pages.
- The `crawl_runs` finalize write is **shielded** against cancellation so a run can never be
  left stuck at `running` forever by an interrupted cleanup.

### 6.6 Mirror discovery and failover (optional)

While crawling, the pipeline can note every `.onion` address a page mentions. Addresses a site
presents as *its own* ("our mirror", "we have moved to") are recorded as `self_declared`;
everything else is a plain `candidate`. If a source's primary address goes dark, failover will
try only its **approved / self-declared** mirrors (never an arbitrary address, so a crawled
host can't redirect the crawler anywhere it likes), and every switch is logged and written to
`sources.active_url` where an operator can see and undo it.

---

## 7. How it finds "leaked data" (extraction)

This is the "does it find leaked data or not" question. A fetched page is just HTML text; the
extraction pipeline turns it into structured `leaks` rows. It runs in stages:

```
PARSE      selectolax turns HTML → clean visible text
EXTRACT    an extractor scans the text → labelled "spans"
LINK       the linker groups spans into discrete leak records
NORMALIZE  dates → timestamptz, "1.2 TB" → bytes, country aliases → canonical, etc.
VALIDATE   Pydantic ExtractedLeak — or it does not proceed
```

### 7.1 Extraction — turning text into labelled spans

Extraction is **pluggable**. Two extractors implement the same interface:

**`RulesExtractor` (default, no ML).** Leak-site listings are highly templated, so regular
expressions catch most of it. It labels spans as:

- **DATE** — several date formats (`2025-01-10`, `10 Feb 2025`, `Feb 10, 2025`…)
- **SIZE** — `1.2 TB`, `500 GB`, etc.
- **STATUS** — `published`, `leaked`, `sold`, `countdown`, `negotiating`, `Status: …`
- **VICTIM_URL** — domain names (with a denylist that removes the crew's own infrastructure)
- **VICTIM** — Title-Case company names, optionally with a legal suffix (Inc, LLC, GmbH…)
- **LOCATION / SECTOR** — country and industry mentions (from a gazetteer)

Crucially, it has **false-positive defenses**. A denylist (`_NOT_AN_ORG`) rejects page
furniture that leak-site pages produce as fake "victims" — "How To Buy Bitcoin", "File Name",
"Affiliate Rules", table headers, navigation. An ALL-CAPS banner ("LOCKBIT LEAKED DATA") is
rejected; a bare country name is rejected (it's a location column, not a company); and a
candidate company name is only accepted if it has a legal suffix *or* a domain nearby, so not
every capitalized word becomes a victim.

Country/sector spans are only kept if they sit **within 250 characters of a real victim or
domain** — otherwise the footer's "© 2026… United States" would get attached to some listing.

**`GlinerExtractor` (optional, zero-shot NER).** Behind the `ml` extra (torch + transformers,
~2 GB, imported lazily). It uses a zero-shot named-entity model to name organizations in prose
that carries no domain — exactly where regex is weak. Because it is optional and lazy-loaded,
**nothing loads torch unless you ask for it**, and the whole pipeline + its tests run on a
clean install with no ML stack.

### 7.2 Linking — spans into discrete leaks

This (`extract/linker.py`) is the core business logic. The rule: **a VICTIM span opens a new
record; the attribute spans that follow attach to it.** Attributes seen *before* the first
victim are held and applied to the first record (some sites print the date above the company
name).

Two subtleties make it correct:

1. **Extraction is per-page.** A victim span and the date next to it on the same page are
   unambiguously related, so no cross-page bookkeeping is needed.

2. **The victim's URL binds to the *nearest* victim, not the next one.** Many sites print the
   link *above* the company it belongs to. A naive "attributes follow the victim" rule would
   make each listing steal the *next* listing's domain — and since `victim_domain` is half of
   `dedupe_hash`, that would silently file every victim under another company's identity. The
   linker measures the character gap to victims above and below and picks the closest.

3. **The actor group comes from the source, not the text.** We already know which site we
   crawled, so the gang name is known rather than guessed. (A group span in the text can still
   override it, for sites that republish other crews' listings.)

### 7.3 Normalization

`extract/normalize.py` and `extract/gazetteer.py` turn raw text into canonical values:

- **Dates** → real `timestamptz` (with the original text preserved in `published_at_raw` so a
  bad parse can be audited, not guessed at).
- **Sizes** → integer bytes (`"1.2 TB"` → `1200000000000`).
- **Actor group** → a normalized slug (`"LockBit"`, `"lockbit3.0"`, `"LOCKBIT"` all become
  `lockbit`, so the dashboard's group filter doesn't show one actor three times).
- **Status** → one of a fixed enum (`published | countdown | sold | removed | negotiating |
  unknown`), weighing *all* the status phrases on a listing rather than the first one seen.
- **Country** → resolved from explicit text mentions first, else the victim domain's **ccTLD**
  (`.de` → Germany). Deliberately silent for `.com`, `.io`, `.co`.
- **Sector** → read from words in the victim's *own name* ("Northwind **Medical** Group",
  "Fairview Unified **School** District").

### 7.4 Validation gate

Every extracted record becomes a Pydantic **`ExtractedLeak`**. Nothing reaches the database
without passing this model — it enforces types, computes the `dedupe_hash`, and defines
`is_usable` (a record with no victim name *and* no domain cannot be deduplicated or shown, so
it is dropped). This is the single schema every extractor and the storage layer agree on.

**So, "does it find leaked data or not?"** — Yes: for each page it produces zero or more
validated leak records (gang, victim company/domain, date, size, status, country, sector),
each with a stable identity and a confidence score. The known weak spot is very dense index
pages with hundreds of listings, where the flat-text linker loses record boundaries (see
[Limitations](#15-known-limitations)).

---

## 8. Deduplication and storage

`storage.py` is the only module in the pipeline that speaks SQL. The heart of it is the
**upsert**, which is what makes re-running safe.

```sql
insert into leaks (dedupe_hash, victim_name, ..., first_seen_at, last_seen_at, ...)
values (...)
on conflict (dedupe_hash) do update set
    -- first_seen_at is DELIBERATELY ABSENT
    last_seen_at   = excluded.last_seen_at,
    victim_name    = coalesce(excluded.victim_name, leaks.victim_name),
    victim_domain  = coalesce(excluded.victim_domain, leaks.victim_domain),
    ...
    status = case when excluded.status = 'unknown' then leaks.status
                  else excluded.status end,   -- never downgrade a known status
returning (xmax = 0) as was_inserted, id
```

Key properties:

- **`ON CONFLICT (dedupe_hash) DO UPDATE`** — the second time the same (gang, victim) is seen,
  it *updates* the existing row instead of inserting a duplicate.
- **`first_seen_at` is never touched on update** — so "new since yesterday" stays meaningful.
- **`last_seen_at` advances on every sighting** — so "is this listing still up?" stays meaningful.
- **`coalesce` on mutable fields** — a later, richer extraction can *fill in* a null country or
  size, but a sparser one can never *blank* a value that was already found.
- **Status never silently downgrades** to `unknown` just because a page edit moved the status
  wording out of the extractor's reach.
- **`returning (xmax = 0)`** — a Postgres trick to tell inserts from updates in one round trip,
  so the pipeline knows exactly which leak IDs are *genuinely new* (only those get run through
  alert matching, so a re-crawl can never re-notify anyone).

Two crawls must never run at once against one Tor daemon (they'd fight for circuits and both
get slower), so every crawl path acquires a **Postgres advisory lock** (`pg_try_advisory_lock`)
first. It's a Postgres lock rather than a Redis one because both the worker *and* the CLI have
a database connection but only the worker has Redis — this is the one lock both can share.

---

## 9. How requests and communication happen

There are two very different communication paths: **browser ↔ API** (reads), and
**UI-triggered crawls** (the API↔worker handoff).

### 9.1 One origin in the browser (no CORS)

The React app **never contains a hostname.** `apps/web/src/lib/api.ts` is the only place that
knows how to reach the API, and it uses an empty base URL by default:

- **Development:** Vite dev server proxies `/api` → the API on port 5000.
- **Production:** nginx (inside the web container) proxies `/api` → the API container.

Either way the browser talks to exactly **one origin** — so there is no CORS preflight, and the
session cookie stays first-party. Every request sends `credentials: "include"` so the session
cookie rides along.

### 9.2 The REST API

Fastify serves a small, typed REST API. Every route group registers a `requireAuth` preHandler
(nothing is public except health checks). Routes:

| Route | Purpose |
|---|---|
| `GET /api/leaks` | Paginated, filtered leak listing (group, status, country, sector, search, date range, sort) |
| `GET /api/leaks/:id` | A single leak |
| `GET /api/stats/*` | Aggregates for the dashboard (counts, per-day series, top groups) |
| `GET /api/sources` | Monitored sites and their crawl health |
| `GET/POST /api/alerts` | Alert rules (typed matchers) |
| `POST /api/crawl` | Queue a "sync now" |
| `GET /api/crawl/status` | Is collection running? what did the last sync do? |
| `GET/POST /api/auth/*` | Better Auth (login, session, logout) |

Every endpoint is defined with a **Zod schema** that validates the query/body *and* serializes
the response — one schema, both directions. Filtering and pagination happen **in Postgres** (the
client never receives more than `limit` rows). Free-text search uses `plainto_tsquery` against
the GIN index, which treats input as literal words — no query-syntax injection is possible.

Cross-cutting protections registered in `app.ts`: **helmet** (security headers), **rate
limiting** (300/min globally, tighter on auth and crawl endpoints), a **1 MB body limit**, a
**request ID on every log line and error response** for tracing, and **log redaction** so
credentials and cookies are never logged.

### 9.3 The "Sync now" handoff — how the UI triggers a crawl

This is a deliberate design point. When an analyst clicks **Sync now**, the API does **not**
start a crawl. Instead:

```
1. Browser → POST /api/crawl
2. API writes a row into `crawl_requests` (status: queued)
      └── if a request is already queued/running, it returns THAT one
          instead of stacking a second crawl (three impatient clicks ≠ three crawls)
3. The worker's "drain" tick (every 10s) notices the row, claims it, runs the crawl
4. Worker updates the row: running → succeeded (with counts of new/updated leaks)
5. Browser polls GET /api/crawl/status and shows the lifecycle
```

**Why a row instead of a direct job enqueue?** The queue is arq, which **pickles** its job
payloads in Python. A `crawl_requests` row is language-neutral, costs nothing, is inspectable
with `psql` when a sync seems stuck, and gives the UI a real lifecycle to poll
(`queued → running → succeeded`). The worker owns Tor and the advisory lock; the API owns the
session and HTTP surface; they share only the database.

Robustness detail: if a worker is killed mid-crawl, rows can be stranded at `running` forever.
Both the API's status endpoint and the worker's drain tick treat rows older than the job
timeout (1 hour) as abandoned, so a crash can't permanently disable the Sync button.

### 9.4 Live dashboard

Because this is a monitoring console, TanStack Query hooks carry a `refetchInterval` — the
dashboard **re-polls every 60 seconds**, so numbers stay live instead of frozen at page load.

---

## 10. Scheduling and the worker

The worker (`services/intel/intel/tasks.py`) runs under **arq**, which has cron built in — so
there is no separate "beat" process (a reason it was chosen over Celery). It runs two schedules:

- **Due-source sweep — every 5 minutes** (`crawl_due`). Crawls only the sources whose own
  `crawl_interval_seconds` has elapsed. The tick is cheap when nothing is due, and each source's
  configured cadence is what decides when it actually runs.
- **Request drain — every 10 seconds** (`drain_crawl_requests`). Picks up whatever the UI's
  Sync button queued into `crawl_requests`. An empty tick is a single indexed lookup, so running
  it every 10 seconds is essentially free while still making a Sync click feel immediate.

Both take the advisory crawl lock, so a scheduled run and a manual sync can never fight for Tor
circuits — whichever is second steps aside and its work is picked up on the next tick. The job
timeout is set to a full hour because a full crawl of 32 sources takes ~15 minutes;
`max_tries = 1` because a crawl that timed out will just time out again, and the next scheduled
run *is* the retry.

---

## 11. Alerting

Analysts create **alerts** with a **typed matcher** — never a regex. `match_kind` is one of
four fixed behaviours:

| `match_kind` | Fires when… |
|---|---|
| `exact` | victim name equals the value |
| `domain` | victim domain equals the value, or is a subdomain of it |
| `substring` | value appears in the victim name or domain |
| `actor_group` | the leak's gang equals the value |

There is **no field where a user can supply a pattern**, so no pathological/injection input is
possible. Matching runs **in SQL against indexed columns, driven by new leak IDs only** — not
on a timer, so the cost is proportional to what actually arrived.

Delivery is **idempotent by construction**: `UNIQUE (alert_id, leak_id)` on `alert_events` means
a retry or a duplicate queue message can never notify the same person about the same leak twice.

> **Current status:** alert events are *recorded* (`status: pending`) but not yet *sent* — no
> SMTP/webhook delivery is wired up.

---

## 12. End-to-end: the life of a leak

Putting it all together, here is exactly how one leak travels from an onion site to the
dashboard:

```
 1. SCHEDULE   Every 5 min the worker crawls sources whose interval elapsed
                 └── or a person clicks "Sync now" → crawl_requests row → 10s drain picks it up
 2. FETCH      Pages fetched over Tor in doubling waves (1, then 4, then 8…) until one is empty
 3. HASH       sha256 of the cleaned page text
                 └── seen this hash before? STOP — nothing downstream runs
 4. PARSE      selectolax → clean visible text
 5. EXTRACT    extractor → labelled spans (victim, url, date, size, status, location, sector)
 6. LINK       linker groups spans into discrete leak records
 7. NORMALIZE  dates → timestamptz, sizes → bytes, group → slug, ccTLD → country, name → sector
 8. VALIDATE   Pydantic ExtractedLeak — or it does not proceed
 9. UPSERT     ON CONFLICT (dedupe_hash) DO UPDATE
                 ├── INSERT: first_seen_at set once, forever
                 └── UPDATE: last_seen_at advances; first_seen_at untouched
10. ALERT      new leak IDs matched against typed alert rules → alert_events (idempotent)
11. SERVE      API queries indexed columns; the dashboard polls every 60s and re-renders
```

---

## 13. Security and safety design

Because this system deliberately connects to live criminal infrastructure, safety was a
first-class concern:

- **Every source ships disabled.** Loading `sources.yaml` does *not* start crawling. Reaching
  these hosts requires an explicit, deliberate `sources enable` — it's framed as a legal/
  operational decision, not a side effect of starting the stack.
- **Tor is client-only and never exposed to the host.** No exit relay, no ORPort, port not
  published — so nothing else on the machine can accidentally route through it, and there's no
  accidental clearnet leak path.
- **No demo/seed data.** Everything on the dashboard is genuinely collected. An empty dashboard
  means collection hasn't run, not that something is broken.
- **Passwords hashed by Better Auth (scrypt)**, sessions are `httpOnly` + `sameSite=strict`
  cookies, `Secure` in production. Minimum 12-character passwords.
- **API hardening:** helmet headers, per-IP rate limits (tighter on auth/crawl), 1 MB body cap,
  log redaction of credentials and cookies, request IDs for tracing.
- **Config refuses to boot on bad input** — the API's Zod-validated `config.ts` will not start
  with a missing `AUTH_SECRET` or malformed env, rather than starting insecurely.

---

## 14. Key design decisions

1. **Identity is `(actor_group, victim)`, never a timestamp.** Status, size, and dates all
   change as a listing progresses; the victim and the crew that took them do not — so they alone
   form the dedupe key.
2. **The API does no background work.** Crawling and matching live in the worker so the API can
   restart/scale freely and a slow Tor crawl can never block a dashboard request.
3. **The database schema owns itself** (Drizzle). One source of truth; the API gets generated
   types, the worker reads raw SQL but never migrates.
4. **Extraction is pluggable and ML is optional.** The default rules extractor needs no ML, so
   the pipeline and its tests run on a clean install; GLiNER is lazy-loaded behind an extra.
5. **Typed alert matchers, never regex** — no user-supplied patterns, matching runs in SQL.
6. **One origin in the browser** — nginx/Vite proxy `/api`, so no CORS and a first-party cookie;
   no component contains a hostname.
7. **arq over Celery** — async-native and cron built in, so no separate scheduler process.

---

## 15. Known limitations

Stated honestly:

- **Extraction on dense index pages.** The linker assumes "a victim span opens a record,
  following attributes attach to it." That holds for a page with a handful of listings; an index
  page with hundreds loses its boundaries once flattened to text, and attributes attach to the
  wrong victim. The proper fix is per-listing DOM segmentation.
- **Location and sector are inferred, not reported.** No leak site publishes either as a field.
  `victim_country` comes from a gazetteer match or (more often) the domain's ccTLD — silent for
  `.com`/`.io`/`.co`; `victim_sector` is read from the victim's name. Both are null for many
  rows — good enough to *filter* on, not a claim to *cite* (the UI renders them as outlined
  chips to signal exactly that).
- **Speculative over-fetching.** A doubling wave can't know a listing ended until a page comes
  back empty, so the last wave always over-fetches; `intel run` reports the count.
- **Sources decay.** Leak sites rotate addresses and get seized. `consecutive_failures` surfaces
  this on the Sources page.
- **No sign-up gate yet.** Anyone who can reach the app can register — flip `disableSignUp` in
  `apps/api/src/auth.ts` before exposing it beyond localhost.
- **Notifications recorded, not sent.** `alert_events` are written `pending`; no SMTP wired up.

---

## 16. How to run it

Full stack (six containers), from the repo root:

```bash
npm run infra:up:full
```

Then open **http://localhost:8080** and log in. Sources ship disabled; enable and crawl:

```bash
npm run intel -- sources enable lockbit
npm run intel -- run --source lockbit
npm run intel -- status
```

New leaks then appear on the Overview, Leaks, and Map tabs (the dashboard refreshes every 60s).
Automatic crawling is already running — the worker sweeps every 5 minutes for due sources and
every 10 seconds for anything the **Sync now** button has queued.

Development mode (datastores in Docker, API + web on the host with hot reload) and the full
command reference are in **[START.md](START.md)**; the authoritative architecture reference is
**[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

*Document generated from a full read of the source tree: the Python collection pipeline
(`services/intel`), the Fastify API (`apps/api`), the React dashboard (`apps/web`), the Drizzle
schema (`packages/db`), and the Docker/Tor infrastructure (`infra`).*
