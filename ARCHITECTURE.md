# Architecture

How the system is put together, what each folder does, and how a leak travels from an onion
site to the dashboard.

To *run* it, see **[START.md](START.md)**. Known limitations are at the end of this page.

---

## The shape of it

Three moving parts plus two datastores.

```
                    ┌──────────────┐
   Tor network ────▶│  tor         │  SOCKS proxy, 3 circuit pools
                    │  (sidecar)   │  internal only — not published to the host
                    └──────┬───────┘
                           │ socks5
                    ┌──────▼───────┐
                    │  worker      │  Python. Crawls, extracts, loads.
                    │  services/   │  arq cron: due-source sweep every
                    │    intel     │  5 min, enrichment every minute
                    └──────┬───────┘
                           │ writes
        ┌──────────────────▼──────────────────┐
        │  postgres        │  redis           │
        │  the record      │  arq job queue   │
        └──────────────────┬──────────────────┘
                           │ reads
                    ┌──────▼───────┐
                    │  api         │  Fastify. Auth, queries, aggregates,
                    │  apps/api    │  and the built React app (apps/web)
                    │              │  stateless — no background work
                    └──────┬───────┘
                           │ HTTP, one origin — :8080
                        browser
```

**The API does no background work.** Crawling, enrichment and feed ingestion live in the worker. That
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
| `src/schema/enrichment.ts` | `domain_enrichment` — WHOIS, DNS, site status per domain |
| `src/schema/iocs.ts` | Indicators of compromise from public feeds |
| `src/schema/mobile.ts` | Scam phone-number reports (Bulk Intelligence · Mobile Number) |
| `src/schema/exposures.ts` | Credentials, keys and card numbers found in pages — masked, never in the clear |
| `src/schema/watchlist.ts` | `watchlist_entries` (what to watch for) and `watchlist_matches` (what turned up) |
| `src/schema/auth.ts` | Better Auth's four tables |
| `test/fixture-seed.ts` | CI-only fixture. Refuses to run against a database holding real crawls |
| `test/schema.test.ts` | Constraint tests against PGlite (real Postgres, in WASM) |

### `apps/api` — stateless HTTP

| File | Purpose |
|---|---|
| `src/config.ts` | Zod-validated env. **Refuses to boot on bad config.** |
| `src/app.ts` | Builds the Fastify instance, registers plugins and routes |
| `src/server.ts` | Boot + graceful shutdown |
| `src/auth.ts` | Better Auth configuration |
| `src/plugins/` | db pool, auth guard, error handler, serving the built web app |
| `src/routes/` | leaks, incidents, iocs, exposures, watchlist, search, sources, stats, crawl, stream, health |
| `src/lib/watch.ts` | Normalises what someone types into a watch value (`https://www.Acme.com/x` → `acme.com`) |

### `apps/web` — the dashboard

| Path | Purpose |
|---|---|
| `src/lib/api.ts` | **The only place that knows where the API is.** |
| `src/lib/queries.ts` | One typed hook per endpoint (TanStack Query) |
| `src/components/AppLayout.tsx` | Layout route — sidebar mounts once |
| `src/components/ProtectedRoute.tsx` | Auth gate, wraps the whole dashboard |
| `src/features/` | One folder per feature: auth, dashboard, incidents, iocs, leaks, map, search, sources |
| `src/styles/tokens.css` | Design tokens, light + dark |

### `services/intel` — collection

| Path | Purpose |
|---|---|
| `intel/cli.py` | `intel run / status / sources / extract-file / backfill-descriptions` |
| `intel/pipeline.py` | fetch → hash → parse → extract → dedupe → upsert |
| `intel/collectors/` | `tor_http.py` (async httpx), `tor_browser.py` (Playwright) |
| `intel/extract/linker.py` | **Spans → discrete leaks.** The core logic. |
| `intel/extract/rules.py` | The extractor: patterns and word lists, no ML. |
| `intel/extract/normalize.py` | Dates → `timestamptz`, `"1.2 TB"` → bytes |
| `intel/extract/gazetteer.py` | Country and sector lookup — fills `victim_country` / `victim_sector` |
| `intel/extract/describe.py` | Each listing's summary text, and its incident types |
| `intel/enrich_sweep.py` | Background WHOIS/DNS/status for victim domains; WHOIS-only for IOC hosts |
| `intel/scheduling.py` | Page waves: which pages a crawl fetches together |
| `intel/models.py` | Pydantic `ExtractedLeak` — validates everything |
| `intel/storage.py` | The only module that speaks SQL |
| `intel/tasks.py` | arq worker: due-source sweep, request drain, enrichment, feeds |
| `intel/feeds/` | Public feeds: URLhaus, ThreatFox, TweetFeed (-> `iocs`), ransomware.live (-> `leaks`), scam-number reports (-> `mobile_numbers`) |
| `intel/extract/phones.py` | Phone-number regex, libphonenumber validation, threat-type / audience classification |
| `intel/extract/secrets.py` | Finds credentials, API keys, password hashes and card numbers; returns masked previews and keyed fingerprints, never stores the value |
| `sources.yaml` | The monitored sites. Mounted, not baked in. |

---

## How a leak reaches the dashboard

```
1. SCHEDULE    every 5 min: crawl the sources whose crawl_interval_seconds has elapsed
                 └── or a person clicks Sync, which writes a crawl_requests row that the
                     worker's 10-second drain picks up
2. FETCH       pages are fetched in doubling waves — 1, then 4, then 8 … — until one
               comes back empty. O(log P) round trips over Tor instead of O(P).
3. HASH        sha256 of the cleaned text
                 └── seen this hash before? STOP. Nothing downstream runs.
4. PARSE       selectolax → clean text
5. EXTRACT     extractor → labelled spans (victim, url, date, size, status, location, sector)
6. LINK        linker groups spans into discrete leaks
7. NORMALIZE   dates → timestamptz, sizes → bytes, groups → slugs,
               country aliases + ccTLD → one canonical country, name words → sector
8. VALIDATE    Pydantic ExtractedLeak, or it does not proceed
9. UPSERT      ON CONFLICT (dedupe_hash) DO UPDATE
                 ├── INSERT: first_seen_at set once, forever
                 └── UPDATE: last_seen_at advances; first_seen_at untouched
10. SERVE      API queries indexed columns; dashboard polls every 60s
```

**Step 2 is where the wall-clock time went.** Pages used to be walked one at a time with a
politeness sleep between each, so a ten-page listing cost ten sequential Tor round trips at
20-30 seconds apiece however many sources ran in parallel. Galloping waves reach the end of
a P-page listing in about log2(P) rounds and never request more than roughly 2P pages,
because a listing's length is not knowable until a page comes back empty. `CRAWL_MAX_INFLIGHT`
is the run-wide ceiling that stops per-source and per-page concurrency multiplying.

**Step 3 is what makes repeat crawls cheap.** An unchanged page costs one fetch and stops.

**Step 9 is what makes the system correct.** The unique constraint on `dedupe_hash` is why
re-running never duplicates. `first_seen_at` is written once and never updated, which is what
makes "what's new since yesterday" answerable at all.

### Why the API cannot start a crawl itself

The worker owns Tor and the Postgres advisory lock that keeps two crawls off one Tor daemon;
the API owns the session and the HTTP surface. They share a database and nothing else — arq
serializes its job payloads with pickle, so "the API enqueues an arq job" would mean writing
a Python pickle encoder in TypeScript. A `crawl_requests` row costs nothing, is inspectable
with `psql` when a sync appears to do nothing, and gives the UI a real lifecycle to poll:
queued → running → succeeded.

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

### A crawl that reaches a gate is a failed crawl

A page 1 that is a DDoS access queue, a human check, a maintenance notice or a login wall
answers HTTP 200 with a few hundred characters. It is recorded as a failure with the gate
named, so `consecutive_failures` shows a blocked source instead of a healthy one collecting
nothing. The crawler does not attempt to pass gates.

### Summaries and incident types come from the listing

A listing's summary is the prose the leak site printed under that victim, cut from the page
text between its anchor and the next listing's. Its incident types are multi-valued and each
one needs evidence: the source (Ransomware), a stated size (Data Breach), the status
(`published` → Data Leak, `sold` → Sale, `countdown`/`negotiating` → Extortion), or words in
the summary. A row with no description gets one composed from its fields, marked as such.

### Extraction is rules, not a model

`RulesExtractor` finds names, domains, dates, sizes and status words with patterns, and
country and sector with word lists. There is no ML stack: nothing to download, no GPU, and
the same input always produces the same leaks.

### Exposures are stored masked, never in the clear

`extract/secrets.py` runs on every page whose content hash is new, after its listings are saved.
It looks for email:password lines and labelled username/password pairs, bcrypt / sha-crypt /
md5-crypt / argon2 hashes, vendor-prefixed API keys, private-key blocks, and Luhn-valid card
numbers. Every rule is anchored to something structural, so a page that merely says "password"
produces nothing, and a bare 32-hex string is left to the IOC feeds.

What reaches the database is a masked `preview` (`j***@acme.com:********`, `AKIA…QRST`), the
email's *domain*, and an HMAC-SHA256 `fingerprint` under `EXPOSURE_SALT`. `Storage.upsert_exposures`
has no parameter the secret could travel in, so it cannot reach the table by accident. The domain
is kept in the clear because it names an organisation, not a person, and is what the watchlist
matches on. Detection runs inside a `try` so a bad pattern cannot fail a crawl.

### The watchlist: the API asks, the worker answers

`POST /api/watchlist` only records the question. The worker's `match_watchlist` job (every minute,
off the crawl lock) matches each entry against rows first seen since its `matched_through`
watermark; a new entry has none, so it is matched against *all* of history on the first tick.
That is why an entry reads "matching…" before it reads "none yet". The watermark is set to the
run's *start* minus ten minutes, not its end: a crawl still committing when a run began would
otherwise have its rows skipped forever, and `ON CONFLICT DO NOTHING` makes seeing a row twice
free. A `domain` entry matches itself and its subdomains (never `notacme.com`); a `keyword` is a
substring of a victim name or indicator, with `_` and `%` treated literally.

### One origin in the browser

Dev: Vite proxies `/api`. Production: the API serves the built app itself
(`src/plugins/web.ts`), so the page and `/api` come from the same process. Either way
the browser talks to exactly one origin — no CORS preflight, and the session cookie stays
first-party. This is why no component contains a hostname.

---

## Data model

```
sources ──┬──< crawl_runs
          ├──< raw_pages
          └──< leaks ─ ─ domain_enrichment   (joined on victim_domain)

iocs ─ ─ domain_enrichment                   (joined on host, WHOIS only)
crawl_requests   (standalone — the API writes, the worker claims)
```

| Table | Holds |
|---|---|
| `sources` | Monitored sites, crawl cadence, health |
| `crawl_runs` | One row per attempt — provenance |
| `crawl_requests` | Syncs asked for by a person; the API/worker handoff |
| `raw_pages` | Fetched text + `content_sha256` (the short-circuit) |
| `leaks` | The canonical entity, with `summary` and `incident_types` |
| `domain_enrichment` | WHOIS, DNS, site status and stack, per domain |
| `iocs` | Indicators from public feeds (URLhaus, ThreatFox, TweetFeed) |
| `mobile_numbers` | Scam phone numbers found in public posts — one row per number per post |
| `hunt_jobs` / `hunt_findings` | On-demand company lookups from Search |
| `exposures` | Credentials, keys, hashes and cards found in crawled pages: masked preview + HMAC fingerprint, no plaintext |
| `watchlist_entries` / `watchlist_matches` | Domains and keywords to watch for, and the leaks, indicators and exposures they matched |
| `user` / `session` / `account` / `verification` | Better Auth |

Deleting a source cascades to its `crawl_runs` and `raw_pages`, but `leaks.source_id` is
`ON DELETE SET NULL` — **pruning a dead site never destroys collected intelligence.**

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Database | PostgreSQL 18 | Real constraints — dedupe is a unique index, not a convention |
| ORM | Drizzle | Generated types shared with the API; SQL stays visible |
| API | Fastify 5 | Schema validation and serialisation built in |
| Auth | Better Auth | Lucia is deprecated, Auth.js frozen |
| Frontend | React 19 + Vite 8 | Authenticated dashboard — no SSR needed |
| Server state | TanStack Query | Caching + `refetchInterval` for live data |
| Charts | Recharts | Themeable straight from CSS custom properties |
| Crawling | httpx + Playwright | Async, and one persistent browser context |
| Parsing | selectolax | ~10–30× faster than BeautifulSoup |
| Extraction | Pattern rules | Deterministic, no model to host or train |
| Queue | arq | Async-native, cron built in, no separate beat |

---

## Known limitations

**Extraction quality on dense pages.** The linker assumes "a victim span opens a record,
following attributes attach to it". That holds for a page with a handful of listings; an
index page with hundreds loses its boundaries once flattened to text, and attributes attach
to the wrong victim — and summaries, which are cut along the same boundaries, inherit the
error. The fix is per-listing DOM segmentation. LockBit 5.0's layout is the clearest case:
its index yields almost no listings.

**Location and sector are inferred, not reported.** No leak site publishes either as a
field. `victim_country` comes from a gazetteer match on the listing text, or — far more
often — from the victim domain's ccTLD, which is silent for `.com` and deliberately silent
for globally-sold codes like `.io` and `.co`. `victim_sector` is read from words in the
victim's own name. Both are therefore null for a large share of rows, and both are a good
enough signal to filter on but not a claim to cite. The UI renders them as outlined chips
rather than solid ones for exactly that reason.

**Speculative page fetches.** A doubling wave cannot know a listing has ended until a page
comes back empty, so the wave containing the end always over-fetches. `intel run` reports the
count; lower `CRAWL_PAGE_CONCURRENCY` for sources that are much shallower than their
`max_pages`.

**Sources decay.** Leak sites rotate addresses, get seized, or put a DDoS queue in front of
the listing. `consecutive_failures` surfaces this on the Sources page.

**Exposure detection finds little on ransomware listings.** Leak-site pages are victim names and
countdowns; they rarely publish raw credentials, so the Exposures page is mostly empty until a
source that does (a paste site, a forum, a channel) is added. `intel scan-secrets FILE` runs the
detector on a saved page to show what a finding looks like. Hashes and cards are matched by
format and checksum only, so a card number is a candidate, not a confirmed card: a bare
checksum-valid number scores 50, and one beside "cvv" or "exp" scores 80.

**The watchlist notifies nobody.** Matches appear on the Watchlist page with an unread count; there
is no email, webhook or chat alert, and the alerts tables were removed in migration 0010.
`watchlist_matches` is the hook for adding one.

**Marketplaces and forums are out of scope.** The schema and extractor are built for
ransomware victim disclosure. A drug market or a forum crawled with them produces noise, and
most sit behind an access queue or a login anyway.
