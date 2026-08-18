# Leak Monitoring — Rebuild Roadmap

Progress tracker for the rebuild described in the architecture review.
Update the checkboxes as work lands. **One line per unit of work that can be verified on its own.**

## Status at a glance

| Phase | Scope | Status |
|---|---|---|
| 0 | Contain — secrets and auth | ⬜ Not started |
| 1 | Foundation — monorepo, infra, database | ✅ Done — migrations applied to Dockerised Postgres |
| 2 | Backend — Fastify API | ✅ Done — 36/36 smoke checks passing |
| 3 | Frontend — React + TanStack | ✅ Done — verified against the live API |
| 4 | Intel — Python collection pipeline | ✅ Built, 79 tests · live crawl not run (see below) |
| 5 | Operate — CI, logging, backups | ✅ Done — whole stack runs in Docker |
| 6 | Retire the old codebase | ✅ Done — legacy tree removed 2026-08-14 |

Legend: ⬜ not started · 🟡 in progress · ✅ done · ⛔ blocked

---

## Phase 0 — Contain

Security work that is independent of the rebuild. Do this regardless of everything else.

- [ ] **Revoke the leaked Gmail App Password.** It is hardcoded at `server/alert.js:67` and is in git
      history. Revoke it in the Google account's App Passwords page.
- [ ] Purge it from git history (`git filter-repo`) before this repo is pushed anywhere public.
- [ ] Confirm no other credential is committed (`git log -p -S "pass" -- server/`).

> Nothing in the rebuild depends on these, but they are live exposure. Do them first.

---

## Phase 1 — Foundation

- [x] Monorepo skeleton — npm workspaces, shared `tsconfig.base.json`, `.gitignore`, `.env.example`
- [x] `infra/docker-compose.yml` — Postgres 18 + Redis 8, healthchecked
- [x] `packages/db` — Drizzle schema for all 10 tables, typechecks clean
- [x] Initial migration generated (`migrations/0000_lucky_maelstrom.sql`) with the unique
      constraint on `leaks.dedupe_hash`, the GIN index on victim fields, and 6 enums
- [x] Migration **applied and verified against a real Postgres engine** — PGlite (Postgres
      compiled to WASM) in `packages/db/test/schema.test.ts`. 9/9 passing. This is not a mock:
      it is Postgres, so enums, identity columns, FKs, and GIN/tsvector all behave for real.
- [x] Migration applied to the **Dockerised** Postgres — 10 tables, 7 indexes on `leaks`
- [x] Seed script — 6 sources, 144 leaks, **idempotent** (verified: 3 runs, still 144)

**What the schema tests actually prove** (each maps to a defect from the review):

| Test | Defect it prevents from recurring |
|---|---|
| duplicate `dedupe_hash` rejected | Pipeline re-runs doubling the dataset |
| upsert keeps `first_seen_at`, advances `last_seen_at` | "What's new since yesterday" being unanswerable |
| invalid `leak_status` rejected | Free-text status values |
| orphan `source_id` rejected | Leaks with no provenance |
| duplicate `(alert_id, leak_id)` rejected | The same alert emailed twice on a worker retry |
| duplicate `user.email` rejected | Two accounts sharing an address |
| GIN/tsvector query matches | Search hitting an index instead of the browser |

### ✅ Docker blocker — resolved

Resolved. Two further issues surfaced and were fixed:

1. **Postgres 18 changed the data path convention.** The image expects a single mount at
   `/var/lib/postgresql` and places the cluster in a version subdirectory below it. Mounting
   `/var/lib/postgresql/data` (the pre-18 convention) made the container crash-loop.
2. **A native `postgresql-x64-18` Windows service already owns port 5432.** Host connections
   silently reached *that* instance instead of the container, producing
   `password authentication failed` rather than a connection error. The container is now
   published on **5433** and `DATABASE_URL` points there. Nothing was changed about the
   native service.

---

## Phase 2 — Backend (`apps/api`)

### 2a. Skeleton
- [x] Fastify 5 + TypeScript, typechecks clean
- [x] `config.ts` — zod-validated env that **fails loudly at boot** on a missing var
- [x] pino structured logging with request ids; `authorization`/`cookie` headers redacted
- [x] Centralised error handler — no stack traces leaked to clients in production
- [x] `helmet`, `@fastify/rate-limit`, CORS locked to an allowlist from env
- [x] `GET /healthz` (liveness) and `GET /readyz` (checks the DB)
- [x] Graceful shutdown on SIGTERM/SIGINT with a 10s force-exit backstop
- [x] Confirmed booting against the Dockerised Postgres (`/readyz` reports `database: up`)

### 2b. Auth
- [x] Better Auth mounted with the Drizzle adapter
- [x] Schema reconciled against `npx @better-auth/cli generate` — this found two real gaps
      (missing `user_id` indexes on `session`/`account`, and `updatedAt` never advancing
      without `$onUpdate`). Both fixed in migration `0001`.
- [x] Database-backed sessions, `httpOnly; SameSite=Strict` cookie (`Secure` in production)
- [x] `requireAuth` guard applied to **every** `/api/*` route
- [x] Auth endpoints rate-limited to 20/min, separate from the global 300/min
- [x] Verified: unauthenticated requests to `/api/leaks`, `/api/stats/*`, `/api/sources`,
      `/api/alerts` all return 401; `/healthz` stays public

### 2c. Routes
- [x] `GET /api/leaks` — server-side pagination, filtering, sorting, full-text search.
      Verified: reports `total: 144` while returning only 25 rows; `limit=1000` is rejected.
- [x] `GET /api/leaks/:id`
- [x] `GET /api/sources` + `/api/sources/stats` — replaces the 10 hardcoded rows
- [x] `GET /api/stats/leaks-per-day` — aggregates on indexed `first_seen_at`, zero-filled
      with `generate_series` so gaps don't draw a misleading straight line
- [x] `GET /api/stats/leaks-per-group`
- [x] `GET /api/stats/summary`
- [x] `POST/GET/PATCH/DELETE /api/alerts` — every query scoped by `ownerId` in the WHERE
- [x] Alert matchers are **typed** — `matchKind: "regex"` is rejected with a 400
- [x] `GET /api/alerts/events` — replaces the always-zero trigger counter

### 2d. Verification — ✅ green
- [x] `npm run typecheck` clean
- [x] `npm run build` clean (both workspaces emit)
- [x] `npm test -w @leak/db` — 9/9 schema constraint tests
- [x] `bash scripts/smoke-api.sh` — **36/36** against live Postgres with seeded data
- [x] Auth guard confirmed returning 401 on all four route groups
- [x] Pagination confirmed bounded

**Gate:** passed. Phase 3 may start.

---

## Phase 3 — Frontend (`apps/web`)

- [x] Vite 8 + React 19 + TypeScript, strict
- [x] Single `lib/api.ts` client reading `VITE_API_URL` — **zero hardcoded hostnames**.
      Vite proxies `/api` in dev, so there is no CORS preflight and the session cookie
      stays first-party.
- [x] TanStack Query with `refetchInterval` (60s) on the live dashboard queries
- [x] `<ProtectedRoute>` wrapping the layout route — verified: `/dashboard` redirects to
      `/login` when signed out, so a new page cannot be added and left unprotected
- [x] Layout route with `<Outlet/>` — the sidebar mounts once
- [x] Leaks table, fully server-driven: search (debounced 300ms), group/status filters,
      sortable columns, pagination. Verified 144 → 18 records on a search.
- [x] Charts fed by the real stats endpoints
- [x] Sources page fed by `/api/sources` with derived health
- [x] Loading (skeleton) / empty / error states on every view; errors surface the
      server's `requestId` so a user report maps to a log line
- [x] One styling system — plain CSS with tokens, both themes at token level
- [x] Charts code-split (`React.lazy`): main bundle 992 kB → 595 kB

**Deviation from the plan:** TanStack Table is *not* used. The table is server-driven, so
there is no client-side sorting/grouping/virtualisation for a library to provide — the
component is ~40 lines of markup over the API response. Revisit if column resizing,
reordering, or pinning is ever wanted.

**Not done** (deliberately deferred — no equivalent exists in the new UI yet):
- [ ] Videos served from `public/` — the new app ships no video; the 128 MB of `.mp4` still
      lives in the old `client/` tree and goes away with it in phase 6.

### 3a. Verification
- [x] `npm run typecheck -w @leak/web` clean
- [x] `npm run build -w @leak/web` clean
- [x] Signed in through the real login form against the live API
- [x] Dashboard, Leaks, Sources, Alerts all render real data; browser console clean
- [x] Alert create → list refresh verified end-to-end through the UI
- [ ] **Visual/layout review not done** — the automated browser could not capture a
      screenshot in this environment. Structure and data were verified via DOM extraction;
      styling and layout still want a human look.

---

## Phase 4 — Intel pipeline (`services/intel`)

- [x] Python package with `uv` + `pyproject.toml`, installable, `intel` console script
- [x] `sources.yaml` — all **83 onion URLs** moved out of notebook cells into config
- [x] Async httpx-over-Tor collector with round-robin SOCKS ports and exponential backoff
- [x] Playwright collector with **one persistent browser context** (the old code launched a
      fresh Firefox process per page request)
- [x] selectolax parsing (~10–30× faster than BeautifulSoup)
- [x] Content-hash short circuit — unchanged pages cost one fetch and stop
- [x] Pluggable extraction: `RulesExtractor` (default, no ML) and `GlinerExtractor`
      (zero-shot, behind the `ml` extra)
- [x] **The entity linker — one copy, 12 tests.** Replaces five divergent copies.
- [x] Normalizers: dates → aware UTC `timestamptz`, `"1.2 TB"` → bytes, status → enum
- [x] Pydantic `ExtractedLeak` — one schema everything validates against
- [x] Upsert on `dedupe_hash`; `first_seen_at` written once, `last_seen_at` advances
- [x] typer CLI: `intel run / status / extract-file / sources sync|list|enable|disable`
- [x] arq worker with built-in cron (no separate beat process)
- [x] Event-driven alert matching — typed matchers in SQL, never a user-supplied regex;
      idempotent via `UNIQUE (alert_id, leak_id)`

### 4a. Verification
- [x] **79 tests passing**, ruff clean
- [x] Full extraction chain verified offline against a fixture page via
      `intel extract-file` — no database, no network
- [ ] 5 storage integration tests written but **skipped** — they need Postgres, and Docker
      Desktop's engine stopped mid-phase and would not restart. Run
      `npm run infra:up` then `cd services/intel && uv run pytest` to execute them.
- [ ] `intel sources sync` not yet run against the live database (same reason)

### 4b. Deliberate design decisions

**Extraction is pluggable, and the default needs no ML.** GLiNER pulls torch + transformers
(~2 GB). Hard-wiring that in would mean the pipeline could not run — and its tests could not
execute — without an ML stack present. `RulesExtractor` is the default and is genuinely
useful (leak listings are highly templated); `GlinerExtractor` is one flag away:

```
uv sync --extra ml
intel run --extractor gliner
```

**Every source ships `enabled: false`.** Syncing `sources.yaml` does not start crawling.
Reaching these hosts means running Tor and connecting to live ransomware infrastructure —
a deliberate decision about your legal and operational position, not something that should
begin as a side effect of loading a config file. `intel sources enable --all` when ready.

**No live crawl has been performed.** The collectors are built and unit-tested against
fixtures; nothing has been fetched from a real onion service.

**`collector` values in sources.yaml are pessimistic.** 81 of 83 are marked `browser`
because the old `crawler.ipynb` routed everything through Selenium. `browser` is far more
expensive than `http` — worth testing each source and flipping the ones that don't need JS.

---

## Phase 5 — Operate

- [x] Dockerfiles for api, web (nginx) and the intel worker; multi-stage, non-root, healthchecked
- [x] Tor sidecar — built rather than pulled, so `MaxCircuitDirtiness 180` and three SOCKS
      ports are version-controlled tuning decisions rather than inherited defaults.
      **Not published to the host**: only the compose network can route through it.
- [x] Compose profiles — `up` still starts datastores only (the dev default, unchanged);
      `--profile full` runs all six services
- [x] `npm run infra:up:full` / `infra:down` / `infra:logs` / `infra:backup`
- [x] GitHub Actions CI: four jobs — node (typecheck/build/schema tests), api (smoke test
      against a Postgres service), python (ruff + pytest, and **fails if the storage tests
      skip** — a skip in CI means the DB was unreachable and would mask a real regression),
      docker (all four images build)
- [x] `scripts/backup-db.sh` — pg_dump custom format, writes `.partial` then renames so an
      interrupted run never leaves a truncated file that looks valid, verifies with
      `pg_restore --list`, prunes past a retention window
- [x] Structured logging already in place (pino JSON in the API, structlog JSON in the worker)

### 5a. Verification — all six containers healthy

```
leakmon-postgres  healthy      leakmon-api     healthy
leakmon-redis     healthy      leakmon-web     healthy
leakmon-tor       healthy      leakmon-worker  up (arq, 4 functions + cron)
```

Verified through nginx on :8080 — unauthenticated `/api/leaks` → 401, sign-in → 200,
authenticated → 200, `?group=lockbit&limit=3` preserved end to end, pagination bounded at
144 total / 5 returned. Worker reaches Postgres (144 leaks, 87 sources) and all three Tor
SOCKS ports. Backup script run for real: 48K dump, verified readable.

### 5b. Five bugs the containerised run exposed

None of these were visible when running on the host — each is worth knowing about.

| Bug | Why it only appeared in Docker |
|---|---|
| `@types/node` missing from `apps/web` | It resolved on the host by npm hoisting it from `apps/api`. The web image installs the web workspace alone, so the hoist disappeared. A genuine missing dependency. |
| `intel/config.py` used `parents[3]` | Correct at `services/intel/intel/config.py`; the container copies the package to `/app/intel/`, where index 3 raises IndexError before a single setting is read. Now walks up looking for a marker file. |
| `npm prune --omit=dev` with `--workspace` | Produced a tree wrong in both directions — dropped `better-auth`, kept `@electric-sql/pglite`. Replaced with a dedicated `npm ci --omit=dev` stage. |
| Root-only `node_modules` copy | npm does **not** hoist everything: `better-auth` lands in `apps/api/node_modules/`. Copying only the root tree silently dropped it while 127 other packages were present. |
| nginx cached the API's IP | `proxy_pass http://api:5000` resolves once at config load. Recreating the API container 502s every request until nginx restarts too. Now resolves per-request via Docker DNS, so the stack survives restarting the API alone. |
| `redis_settings` as `@staticmethod` | arq reads it as an attribute and got the function object: `'staticmethod' object has no attribute 'host'`. |

### 5c. Also fixed while here

- Integration tests were leaving `test-*` rows in the dev database. They now clean up in a
  fixture teardown; verified the database is byte-identical after a run.
- Seed sources shipped `enabled: true` with illustrative `.onion` addresses, so `intel run`
  would have burned its retry budget on four hosts that do not exist. Demo data is now
  inserted disabled.
- `npm run infra:up` needed `--env-file .env`: compose reads `.env` relative to the compose
  file, not the repo root, so `AUTH_SECRET` interpolation failed.

---

## First live crawl — 2026-08-14

Ran against real onion services for the first time. **The infrastructure works; extraction
quality does not yet.**

### Verified working against live sites

| | |
|---|---|
| Sources reached | 5 of 9 (lockbit, lockbit-2, rhysida, inc-ransom, akira) |
| Entities extracted from one LockBit page | 772 |
| Leaks loaded | 726 |
| **Idempotency on a re-run** | **772 found, 0 new, 772 seen again — row count unchanged** |
| Failure bookkeeping | `crawl_runs.status=failed`, `consecutive_failures` incremented, error stored |

Tor (3 SOCKS ports), both collectors, parsing, normalisation, dedupe and upsert all work at
scale. The idempotency result is the important one — it is the old pipeline's worst defect,
proven dead on real data.

### ⚠️ Extraction quality is not production-usable

Of 721 LockBit leaks, 350 got a domain and 348 a date — but spot-checking shows **names and
domains are frequently paired from different listings**:

```
DM Merchandising                  | sagaciousresearch.com   <- unrelated
The Albert                        | heinrichseegers.de      <- unrelated
Fabbrica Automatismi Apertura Ca  | worldlearning.org       <- unrelated
C.so Europa                       | c.so                    <- Italian street address as a domain
TX. We                            | champion.com.co         <- sentence fragment
```

**Root cause.** The linker assumes "a victim span opens a record, following attributes
attach to it". That holds for a page with a handful of listings. A LockBit index page holds
*hundreds*, and once the HTML is flattened to text the boundaries between them are gone — so
attributes attach to whichever name happened to precede them.

Per-page extraction fixed the old per-corpus problem, but a dense index page needs a further
step down.

### Fixes, in order of value

1. **Segment per listing, not per page.** Extract from DOM subtrees (each listing is a card
   or table row) rather than flattened page text. This restores the boundaries the linker
   needs and is likely to fix most of the mis-pairing on its own.
2. **Switch to GLiNER.** `uv sync --extra ml`, then `intel run --extractor gliner`. Better
   at telling an organisation from a nav link, which is the rules extractor's known weakness.
3. **Confidence gating.** Route low-confidence extractions to a review queue instead of
   loading them silently.
4. Expand `_NOT_AN_ORG` as more site chrome shows up. Already extended once from this run
   ("How To Buy Bitcoin", "File Name", "Affiliate Rules").

### Bug fixed during this run

**Playwright was missing from the worker image.** `uv sync --no-dev` skips optional extras,
so 81 of 83 sources — everything marked `collector: browser` — died at fetch time with
`ModuleNotFoundError`. The image now installs the `browser` extra and Firefox with
`--with-deps`. Verified: Firefox 153 launches inside the container.

### Also learned

`akira` was reachable but yielded 0 entities; `bianlian`, `funksec` and `hunters` failed to
connect. Several of the 83 URLs are simply dead — leak sites rotate addresses and get
seized. That is expected, not a pipeline defect: `consecutive_failures` surfaces it on the
Sources page.

---

## Operational fixes — 2026-08-14 (second session)

Two issues surfaced from real use.

### `docker compose run` leaves a half-started stack

Running `npm run infra:down` and then any `intel` command via a raw
`docker compose run --rm worker` shortcut brought up **only** postgres, redis and tor —
`run` starts a service's `depends_on`, not the whole profile. The database worked; the
website returned "failed to load" with nothing explaining why.

Fixed with an `npm run intel` wrapper that does `up -d` on the full profile first. START.md
now documents this and the raw-shortcut trap, and the troubleshooting section names the
exact symptom.

### Every source switched from `browser` to `http`

29 of 32 sources were marked `collector: browser`, inherited from the old Selenium crawler.
Measured over the same 32 sources:

| | `browser` | `http` |
|---|---|---|
| Wall clock | 434 s | **160 s** |
| Failures | 6 | **1** |

The six browser failures — cloak, funksec, ijzn3sicrcy7, om6q4a6cyipx, ransomhouse,
z3wqggtxft7i — were all sites that plain HTTP had *already fetched successfully* during the
reachability probe. Playwright was timing out on `page.goto` where one async request
succeeded, and those five contributed 125 leaks once switched.

`akira` is the only remaining failure and returns HTTP 403 — it is actively blocking, which
no collector choice fixes.

Only move a source back to `browser` if its listings genuinely render client-side and come
back empty over HTTP.

---

## Phase 6 — Retire the old codebase

**Only after phases 2–4 are verified working.**

- [ ] Migrate existing `Organisations` documents from MongoDB into `leaks`
      (parse dates, compute `dedupe_hash`, set `first_seen_at`)
- [ ] Confirm row counts and spot-check records
- [ ] Delete `server/`
- [ ] Delete `client/`
- [ ] Delete `intelligence_engine/` — **first** extract anything not reproducible:
      the trained `model-best/` weights and any scrape data not yet loaded into Postgres
- [ ] Update `README.md` for the new architecture

---

## Throughput, syncing and tags — 2026-08-18

Three things the system was doing badly, and what changed.

### Pages of one source are now fetched together

`crawl_source` walked pages strictly in order with a `request_delay_seconds` sleep between
each. Cross-source concurrency did nothing for a source that was simply deep: ten pages meant
ten sequential Tor round trips at 20-30s apiece.

Pages now go out in **doubling waves** — `[1]`, `[2-5]`, `[6-13]`, … — stopping at the first
page that comes back empty (`intel/scheduling.py`). Reaching the end of a P-page listing
costs O(log P) round trips instead of O(P), and because only the last wave can overshoot,
never more than about 2P pages are requested. That over-fetch is unavoidable: a listing's
length is not knowable until a page comes back empty. It is counted, reported by `intel run`,
and bounded by `CRAWL_PAGE_WAVE_CAP`.

Page 1 is still fetched alone. It decides reachability, mirror failover and challenge
detection, and a failover changes the address every other page would have come from.

- [x] `intel/scheduling.py` — wave planner, 8 tests
- [x] `crawl_source` rewritten around it, 11 tests with a fake collector that measures
      in-flight overlap and asserts the walk stays logarithmic
- [x] `CRAWL_PAGE_CONCURRENCY`, `CRAWL_PAGE_WAVE_CAP`, `CRAWL_MAX_INFLIGHT`
- [x] One run-wide fetch budget, so per-source and per-page concurrency cannot multiply into
      64 simultaneous circuits

### Scheduled syncing actually respects each source's interval

`sources.crawl_interval_seconds` was written by every operator and read by nobody: the only
schedule was one hourly `crawl_all`. A source set to 15 minutes refreshed hourly, and 30
stable sources were refetched on that same hour whether or not anything had changed.

- [x] `crawl_due` + `due_sources()` — sweeps every 5 minutes, crawls only what is due
- [x] `intel run --due-only` runs the same selection by hand

### Syncing on demand, from the app

There was no way to ask for a crawl from the UI, and the API cannot enqueue an arq job — arq
pickles its payloads and the API is TypeScript. A `crawl_requests` row is the handoff instead:
the API writes, a 10-second worker tick claims it under the existing advisory lock.

- [x] `crawl_requests` table (migration `0003`), claimed with `for update skip locked`
- [x] `drain_crawl_requests` — drains the queue while holding the crawl lock, and expires
      requests stranded by a worker that died mid-crawl
- [x] `POST /api/crawl`, `GET /api/crawl/status`, `GET /api/crawl/requests`
- [x] **Sync now** on the Leaks page: real queued → running → succeeded states, and the table
      refreshes when the crawl *finishes* rather than when the click lands
- [x] **Latest arrivals** strip — newest listings by `first_seen_at`, with the ones this sync
      found marked new

### Location and sector are extracted at last

`victim_country` and `victim_sector` had been columns since migration `0000` with nothing
writing to them, so the table could only ever answer "who", never "where".

- [x] `intel/extract/gazetteer.py` — country aliases, ccTLD inference, sector keywords
- [x] `location` and `sector` labels through rules, GLiNER prompts and the linker
- [x] ccTLD inference excludes globally-sold codes (`.io`, `.co`, `.ai`, `.me`) — a wrong
      country is worse than a null one, because once written the two are indistinguishable
- [x] Country names rejected as victim organisations — "United States" was opening a record
      of its own and stealing the real victim's domain
- [x] Partial indexes on both columns (migration `0004`), `country` / `sector` filters on
      `/api/leaks`, `/api/stats/leaks-per-tag` for the dropdowns
- [x] Tags column on the Leaks page, rendered as outlined chips: these are inferred, and
      should not look as certain as a status the site printed itself
- [x] `upsert_leaks` now coalesces both columns on conflict — they were written on insert
      only, so no existing row would ever have gained them
- [x] Seed data moved onto the same canonical vocabulary ("United States", not "US")

Tests: 150 Python, 9 schema. Full workspace typecheck clean.

---

## Demo data removed — 2026-08-18

The dashboard was showing 144 invented victims mixed into 1,310 collected ones, and nothing
in the UI distinguished them.

`npm run db:seed` wrote the Microsoft sample-company set — Northwind Logistics, Contoso
Manufacturing, Fabrikam Health — as real `leaks` rows. They counted toward every total on the
Overview page, every point on the per-day chart, and every option in the group, location and
sector dropdowns. Nothing marked them, so a number that was 90% real and 10% invented looked
exactly like a real one.

- [x] 144 fixture rows deleted from the development database (backup first:
      `backups/pre-mock-removal-20260818-152652.sql`)
- [x] `src/seed.ts` → `test/fixture-seed.ts` — out of the package's shipped surface
- [x] `npm run db:seed` removed from the root scripts entirely
- [x] The fixture now **refuses to run** against a database containing any leak it did not
      write, so it cannot reach a real dataset even when invoked directly
- [x] CI calls it explicitly (`npm run fixture:ci -w @leak/db`), which is the only place it
      may run: the smoke test asserts exact counts and CI has no Tor, so it cannot collect

**Why it was kept at all.** `scripts/smoke-api.sh` checks that pagination caps a real result
set, that a group filter returns only that group, that full-text search matches, and that
per-source `leakCount` correlates — the last of which once passed while every count was
wrong. None of those can be checked against an empty database, and CI cannot crawl.

Not removed, and not mock data: `services/intel/tests/fixtures/sample_leak_page.txt` (an
extractor test input — it never reaches the database) and the `FakeCollector`/`FakeStorage`
doubles in `test_pipeline_concurrency.py` (test doubles, so the crawl tests need no Tor).

---

## Decisions log

Record anything we change our mind about, so the reasoning survives.

| Date | Decision | Why |
|---|---|---|
| 2026-08-13 | PostgreSQL over MongoDB | The original bug was a missing constraint across three components. Data is relational; native upsert; Drizzle has no Mongo support. |
| 2026-08-13 | Fastify over Express 5 | Schema validation and serialization built in, closing the validation gap structurally. |
| 2026-08-13 | Better Auth over hand-rolled JWT | Lucia deprecated, Auth.js frozen to security patches. |
| 2026-08-13 | GLiNER over fine-tuned spaCy | Zero-shot — sidesteps the missing `ransomwaredata.json` training set entirely. |
| 2026-08-13 | arq over Celery | Async-native to match httpx/Playwright; cron built in, no separate beat process. |
| 2026-08-13 | Build alongside, delete later | The old code stays until the replacement is verified. |
| 2026-08-14 | PGlite for schema tests (test harness only) | Docker is blocked on a missing WSL2 backend. PGlite is real Postgres in WASM — no daemon, no admin rights — so constraints can be verified now. It is a **test harness only**; `infra/docker-compose.yml` stays the real dev/prod path. |
| 2026-08-14 | Recharts over Chart.js | React-native composition, and themeable directly from CSS custom properties so both light and dark come from one token set. |
| 2026-08-14 | No TanStack Table | The table is server-driven; the library's value is client-side sorting/grouping, which this table does not do. |
| 2026-08-14 | Single series colour on both charts | Both show one measure. Colouring group bars individually would encode rank, not identity, and would repaint when a filter reorders them. |
| 2026-08-14 | Rules extractor as the default, GLiNER optional | A 2 GB torch dependency must not be required to run the pipeline or its tests. |
| 2026-08-14 | Sources ship disabled | Crawling live ransomware infrastructure should be an explicit act, not a side effect of `sources sync`. |
| 2026-08-14 | Actor group comes from the source, not the text | We know which site we crawled. Guessing it from prose is what forced the old `orphan_entries` machinery. |
| 2026-08-14 | Extraction is per page, not per corpus | The old code ran NER over every site concatenated, then reassociated entities by position. Per page, a victim and the date beside it are unambiguous. |
| 2026-08-18 | Doubling page waves over "fetch all max_pages" | A listing's length is unknown until a page comes back empty. Waves get O(log P) round trips while over-fetching at most ~2x, where firing all `max_pages` would burn a circuit per page on sources that have three. |
| 2026-08-18 | `crawl_requests` table over the API enqueuing arq | arq pickles its job payloads; producing one from TypeScript means maintaining a pickle encoder against a library the API does not depend on. Both processes already share Postgres, and a row is inspectable when a sync appears to do nothing. |
| 2026-08-18 | Country inferred from ccTLD, but never from `.io`/`.co`/`.ai` | These are national codes sold worldwide. A wrong country in `victim_country` is indistinguishable from a right one and silently skews every filter built on it. |
| 2026-08-18 | Tags rendered as outlined chips, status as filled | Status is what the leak site said. Location and sector are our inference from a gazetteer and a domain suffix. They should not carry equal visual authority. |
| 2026-08-18 | Demo data deleted; the fixture kept but made unreachable | Invented victims in `leaks` are indistinguishable from collected ones and silently inflate every aggregate built on that table. CI still needs rows to test an API against, and cannot crawl — so the fixture survives under `test/`, refuses any database holding a real leak, and is no longer one word away from a developer's own. |
