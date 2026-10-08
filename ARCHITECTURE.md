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
| `intel/crawl/manager.py` | **The cycle manager**: starts, joins, seeds, serves and finalizes a crawl cycle |
| `intel/crawl/queue.py` | Every SQL statement of the URL queue: claim, lease, retry, prune, discover, finalize |
| `intel/crawl/worker.py` | The claim loops; turns a fetch outcome into a queue transition |
| `intel/crawl/fetch.py` | One URL: single fetch attempt, content checks, SHA-256 compare, then `ingest_page` |
| `intel/crawl/seeding.py`, `frontier.py` | Eager pagination seeding; URL normalization, allowlist, what ends a listing |
| `intel/crawl/report.py` | Per-source verdicts, cycle status, the end-of-cycle report |
| `intel/pipeline.py` | `ingest_page` (store + extract, shared by both crawlers) and the legacy source-at-a-time crawler |
| `intel/collectors/` | `tor_http.py` (async httpx, streamed and size-capped), `tor_browser.py` (Playwright); both return a classified `FetchResult` |
| `intel/extract/linker.py` | **Spans → discrete leaks.** The core logic. |
| `intel/extract/rules.py` | The extractor: patterns and word lists, no ML. |
| `intel/extract/normalize.py` | Dates → `timestamptz`, `"1.2 TB"` → bytes |
| `intel/extract/gazetteer.py` | Country and sector lookup — fills `victim_country` / `victim_sector` |
| `intel/extract/describe.py` | Each listing's summary text, and its incident types |
| `intel/enrich_sweep.py` | Background WHOIS/DNS/status for victim domains; WHOIS-only for IOC hosts |
| `intel/scheduling.py` | Legacy crawler only: page waves and per-source time budgets |
| `intel/models.py` | Pydantic `ExtractedLeak` — validates everything |
| `intel/storage.py` | The only module that speaks SQL |
| `intel/tasks.py` | arq worker: the cron that starts cycles, the Sync request drain, enrichment, feeds |
| `intel/feeds/` | Public feeds: URLhaus, ThreatFox, TweetFeed (-> `iocs`), ransomware.live (-> `leaks`), scam-number reports (-> `mobile_numbers`) |
| `intel/extract/phones.py` | Phone-number regex, libphonenumber validation, threat-type / audience classification |
| `intel/extract/secrets.py` | Finds credentials, API keys, password hashes and card numbers; returns masked previews and keyed fingerprints, never stores the value |
| `sources.yaml` | The monitored sites. Mounted, not baked in. |

---

## How a leak reaches the dashboard

```
1. SCHEDULE    every CRAWL_SWEEP_INTERVAL_MINUTES (5): start a crawl cycle if any URL is due
                 └── or a person clicks Sync, which writes a crawl_requests row that the
                     worker's 10-second drain attaches to the running (or a new) cycle
2. FETCH       workers claim ONE URL at a time from crawl_urls and make one attempt over
               Tor; transient failures go back to the queue with a delay (see "Crawl cycles")
3. HASH        sha256 of the cleaned text
                 └── seen this hash before? STOP. Nothing downstream runs.
4. PARSE       selectolax → clean text; a listing laid out as repeated tiles keeps one
               block of text per tile (see "Tile listings" below)
5. EXTRACT     extractor → labelled spans (victim, url, date, size, status, location, sector)
6. LINK        linker groups spans into discrete leaks — one per tile on a tiled listing
7. NORMALIZE   dates → timestamptz, sizes → bytes, groups → slugs,
               country aliases + ccTLD → one canonical country, name words → sector
8. VALIDATE    Pydantic ExtractedLeak, or it does not proceed
9. UPSERT      ON CONFLICT (dedupe_hash) DO UPDATE
                 ├── INSERT: first_seen_at set once, forever
                 └── UPDATE: last_seen_at advances; first_seen_at untouched
10. SERVE      API queries indexed columns; dashboard polls every 60s
```

**Step 2 is where the wall-clock time went** (in the legacy crawler; see "Crawl cycles" below for
the queue engine). Pages used to be walked one at a time with a
politeness sleep between each, so a ten-page listing cost ten sequential Tor round trips at
20-30 seconds apiece however many sources ran in parallel. Galloping waves reach the end of
a P-page listing in about log2(P) rounds and never request more than roughly 2P pages,
because a listing's length is not knowable until a page comes back empty. `CRAWL_MAX_INFLIGHT`
is the run-wide ceiling that stops per-source and per-page concurrency multiplying.

**Step 3 is what makes repeat crawls cheap.** An unchanged page costs one fetch and stops.

**Step 9 is what makes the system correct.** The unique constraint on `dedupe_hash` is why
re-running never duplicates. `first_seen_at` is written once and never updated, which is what
makes "what's new since yesterday" answerable at all.

### Crawl cycles: the URL is the unit of work

With `CRAWL_ENGINE=queue` (the default) collection runs as **cycles** over a URL queue in Postgres
(`crawl_urls`), not source by source. The legacy source-at-a-time crawler remains behind
`CRAWL_ENGINE=legacy` and is what the `intel run` CLI always uses.

```
arq cron (every CRAWL_SWEEP_INTERVAL_MINUTES)  or  a Sync request
        |                                  only decides WHEN; it never fetches
   CycleManager.run()   -- one running cycle at a time (unique index), across all processes
        |  seed what is due: listing pages, followed pages whose interval has elapsed
   crawl_urls (queued / running / retry / succeeded / failed / skipped)
        |  workers claim ONE URL at a time: FOR UPDATE SKIP LOCKED + a lease
   fetch (one attempt) -> clean text -> SHA-256 -> unchanged? skip : ingest_page() (existing extractor)
        |  transient failure -> back to the queue with a delay; the worker moves on
   finalize (atomic): crawl_runs per source, sources health, crawl_cycles + report
```

- **Concurrency is system-wide.** `CRAWL_WORKERS` is enforced in the database at claim time, so three
  worker processes share one limit instead of tripling it. It is not derived from the number of Tor
  SOCKS ports (one Tor process; the ports add no throughput).
- **Retries never hold a worker.** A transient failure (timeout, 5xx, 429, Tor circuit) becomes a
  `retry` row due after a backoff; permanent ones (404/410/403, gate pages, files) are not retried.
- **Leases make a crashed worker harmless.** An expired lease is recovered by the reaper; a cycle is
  never finalized while any URL is still queued, running or waiting to retry.
- **Pagination is declared, so every page is queued up front** (`seeding.py`): `max_pages` the first
  time, then `min(max_pages, deepest page seen + 2)`. Page 1 gates the rest (`CRAWL_PAGE1_FIRST`), and
  an empty page, a 404/410, or a failed page 1 skips the pages behind it. A timeout or a 5xx never
  does: only evidence of an end prunes.
- **Link following** (`CRAWL_FOLLOW_LINKS`): from a page whose content is new or changed, up to
  `CRAWL_LINKS_PER_PAGE` new same-host links, `CRAWL_LINK_DEPTH` deep, `CRAWL_LINK_MAX_PAGES` per source
  per cycle; never a file, an account page, another host or an internal address. On a tiled listing
  the links inside new or changed tiles are offered first and links inside unchanged tiles not at all,
  so the per-page budget goes on new victims' pages rather than the menu. Followed pages are stored,
  scanned for exposures, and read as one victim's page (see "Detail pages" below) — never run through
  the listing extractor. They are recrawled when `CRAWL_LINK_INTERVAL` (a week) has elapsed, without
  their listing having to change.
- **Reporting** reuses existing tables: `crawl_runs` per source (what the Sources page reads), and
  `crawl_cycles` with a stored `summary` (sources, URL outcomes, retries, links, HTTP and error
  histograms). The Sync button's `crawl_requests` lifecycle is unchanged.

The cycle's per-source verdict mirrors the old meaning: **a source fails exactly when its page 1
fails**. A cycle is `failed` only when nothing worked; a source down while others succeed is
`completed`, with the failure named in the report.

The older time-budget settings (`CRAWL_RUN_WINDOW`, `CRAWL_SOURCE_*_BUDGET`) and doubling page waves
belong to the legacy crawler only.

### Tile listings: one tile, one victim

Many leak sites lay their listing out as repeated tiles, cards or table rows. Flattened to one
text, the tile boundaries were gone, and the linker — which attributes by reading order — folded
any tile whose name had no legal suffix and no nearby domain ("Grupo Caberj", "Wavecrest HFA")
into the tile before it. inc-ransom stored one leak whose summary was four victims' names and
their icon labels.

`collectors/html.py` now finds the repeated blocks before flattening (`listing_blocks`): the
element whose children mostly share one tag + class signature, three or more of them with real
text, scored by total text so a menu never wins, and only when they hold a good share of the
page. A grid's rows are stepped through to the tiles inside them. `to_text` then writes the
text before the list, each block, and the text after, separated by a record-separator line
(`BLOCK_BREAK`). Text is what is stored and hashed, so the boundaries travel with it.

`pipeline.extract_page` extracts each block on its own, at most one leak per block: the name is
the block's first line that reads as a name (no suffix or domain needed inside a tile), and the
domain, date, size, status, country — including a flag image, icon class or emoji — sector and
summary come from that block alone. Text outside the blocks makes no leaks. A page with no
repeated structure produces exactly the text it always did and goes through the whole-page
linker unchanged. `item_selector` in `sources.yaml` overrides detection for one source.

Rows written by the whole-page linker are corrected, not duplicated: identity is unchanged
(`dedupe_hash` is group + domain-or-name), and the first time a tile record (`extraction.mode =
"tile"`) meets such a row it replaces the row's summary, status, date, size, country, sector and
incident types outright instead of coalescing with them.

### Detail pages fill in their victim, and never create one

A page reached by following a link (`crawl_urls.kind = 'link'`, or the legacy crawler's link walk)
is read as one victim's own page (`collectors.detail_page`, `pipeline.extract_detail`): name
candidates from its `<h1>`, `<h2>`, title-like elements and `<title>`, and every other field —
domain, country, size, date, status, and revenue and description into the summary — from its main
text with the header, nav and footer removed. `Storage.enrich_leak` then finds the leak this source
already holds for that victim, by domain or by name (lowercased, punctuation collapsed), and fills
only its empty fields. A page that matches no listed victim creates nothing and is logged: only the
listing says who is a victim. `dedupe_hash` is never touched, so the listing keeps finding the row.

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

**Extraction quality on dense pages without tiles.** A listing laid out as repeated tiles,
cards or rows is extracted one block at a time (see "Tile listings"). A listing that is one
long run of text — no repeated elements — still goes through the whole-page linker, which
assumes "a victim span opens a record, following attributes attach to it" and needs a legal
suffix or a nearby domain before it accepts a name.

**Location and sector are inferred, not reported.** No leak site publishes either as a
field. `victim_country` comes from a gazetteer match on the listing text, or — far more
often — from the victim domain's ccTLD, which is silent for `.com` and deliberately silent
for globally-sold codes like `.io` and `.co`. `victim_sector` is read from words in the
victim's own name. Both are therefore null for a large share of rows, and both are a good
enough signal to filter on but not a claim to cite. The UI renders them as outlined chips
rather than solid ones for exactly that reason.

**Speculative page fetches (legacy crawler).** A doubling wave cannot know a listing has ended
until a page comes back empty, so the wave containing the end always over-fetches. `intel run`
reports the count; lower `CRAWL_PAGE_CONCURRENCY` for sources much shallower than `max_pages`.
The queue engine instead queues a source's declared pages up front and prunes the ones behind an
empty page or a 404.

**No source paginates today.** Every source in `sources.yaml` declares `pagination_style: none`,
so eager pagination is dormant until one declares `query`, `path` or `offset`. Link following is
what grows the frontier in practice.

**The queue engine does not fail over to mirrors.** It records onion addresses a page announces
(`CRAWL_DISCOVER_MIRRORS`) but never switches to one; `CRAWL_MIRROR_FAILOVER` affects only the
legacy crawler. An operator can promote one with `intel mirrors use`.

**Detail pages only enrich.** A victim's own page fills empty fields of the leak its listing
created; it never creates a leak, so a victim that appears only on a detail page (not on any
listing) is not collected. There is no revenue column: revenue is kept in the summary text.

**Out-of-range pages that repeat page 1** (some sites do this) are not detected as the end of a
listing; they read as unchanged duplicates.

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
