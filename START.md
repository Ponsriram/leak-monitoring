# Start here

Get the app running, log in, turn on crawling, watch data arrive.

- **How it works, and known limitations:** [ARCHITECTURE.md](ARCHITECTURE.md)

All commands are PowerShell, run from the repo root (`C:\Users\ponsr\Desktop\leak-monitoring`).

---

## 1. Start everything

```powershell
npm run infra:up:full
```

Starts five containers: Postgres, Redis, Tor, the API (which also serves the web app), and the
worker. The first
run builds images and takes a few minutes; after that, seconds.

Check they're healthy:

```powershell
npm run infra:ps
```

> **Docker Desktop must be running first.** If you see `failed to connect to the docker API`,
> start Docker Desktop, wait for it to settle, then retry.

---

## 2. Open the app

**http://localhost:8080**

Log in with the throwaway local account:

| | |
|---|---|
| Email | `analyst@example.com` |
| Password | `correct-horse-battery` |

> ⚠️ Local-only test account. Don't reuse this password, and replace the account before the
> app is reachable by anyone else.

Public sign-up is disabled. To add an account, put its password in `PROVISION_PASSWORD`
(≥ 12 characters — never on the command line) and run:

```powershell
npm run user:provision -w @leak/api -- --email you@example.com --name "Analyst"
```

---

## 3. Turn on crawling

Sources ship **disabled**. Reaching them means connecting to live criminal infrastructure over
Tor — a deliberate act, not a side effect of starting the stack.

Pipeline commands run through `npm run intel -- <command>`. See what's available:

```powershell
npm run intel -- sources list --all
```

> 23 sources are listed in `services/intel/sources.yaml` — only sites that actually yield
> listings. Leak sites move and go offline, and some put a DDoS queue or human check in front of
> the listing — the crawler records those as failures, naming the gate, and
> `consecutive_failures` on the Sources page climbs. A site that stays like that, or turns out
> not to be a leak site, is deleted from the file, and then from the database with:
>
> ```powershell
> npm run intel -- sources sync --prune
> ```
>
> Its collected leaks are kept.

### Enable

One source:

```powershell
npm run intel -- sources enable lockbit
```

All sources:

```powershell
npm run intel -- sources enable --all
```

(Disable the same way: `npm run intel -- sources disable lockbit` or `--all`.)

### Crawl

One source:

```powershell
npm run intel -- run --source lockbit
```

Everything enabled:

```powershell
npm run intel -- run
```

Only the sources that are due:

```powershell
npm run intel -- run --due-only
```

A crawl takes 1–3 minutes over Tor. Check what happened:

```powershell
npm run intel -- status
```

Then reload **http://localhost:8080** — new leaks appear on Dashboard and Leaks, and the **Map**
tab plots them by country. The dashboard refreshes every 60 seconds.

### Reading a JavaScript site's own data (`json_items`)

A `collector: browser` source captures the JSON its page loads from its own host (XHR / fetch)
while it renders. If you tell it where the victim records are in that JSON, leaks are built
straight from them — with fields the page may only show after a click — instead of from the
rendered text. Every source ships **without** a mapping; add one only from what you have seen
the site actually send.

1. Open the listing page in **Tor Browser** and press **F12** → **Network** → filter **XHR**
   (Fetch/XHR). Reload the page.
2. Click each request and look at **Response**. Find the one whose body holds the list of
   victims — an array of objects, one per company.
3. Note a stable part of its URL (e.g. `/api/v1/posts`), the path from the top of the body to the
   array (e.g. `data.posts`; leave it out if the body *is* the array), and, inside one object, the
   key for each field (use dots for nested keys, e.g. `company.title`).
4. Add it to the source in `services/intel/sources.yaml` — `name` or `domain` is required, the
   rest are optional:

   ```yaml
   - slug: example
     collector: browser
     # ...
     json_items:
       match: "/api/v1/posts"     # substring of the request URL
       path: "data.posts"         # dotted path to the array
       name: "company.title"
       domain: "company.website"
       country: "country"         # a name or a two-letter code
       revenue: "revenue"         # kept in the summary as "Revenue: ..."
       description: "description"
       date: "createdAt"          # a date string, or a Unix epoch in s or ms
   ```

5. `npm run intel -- sources sync`, then crawl the source and check `npm run intel -- status`.
   If the response is not seen or the path does not lead to a list, the page is read from its
   text exactly as before — a mapping never makes a source collect *less*.

The collector keeps only same-host JSON, up to `CRAWL_MAX_BYTES` per page in total, and only what
the page loads by itself: it does not click.

### Feeds (no Tor, no enabling needed)

Alongside the crawler, the worker pulls free public feeds on its own schedule:

| Feed | Lands in | How often |
|---|---|---|
| URLhaus, ThreatFox, TweetFeed | Bulk Intelligence · IOC | hourly |
| ransomware.live | World Incidents · Ransomware / General / Dark Web | every 15 minutes |
| Scam phone-number reports (Mastodon hashtags, r/Scams) | Bulk Intelligence · Mobile Number | every 30 minutes |

To fetch them right now instead of waiting:

```powershell
npm run intel -- feeds
```

(`--only iocs`, `--only ransomware` or `--only mobile` for one group.)

---

## 4. Automatic crawling

Already running — nothing to do. Every 5 minutes (`CRAWL_SWEEP_INTERVAL_MINUTES`) the worker
looks for work that is due. If any is, it starts one **crawl cycle**: the due pages are queued in
Postgres, several workers fetch them at once (`CRAWL_WORKERS`, 6 by default, however many worker
processes run), changed pages are extracted, failures are retried later, and the cycle closes with
a report. Only one cycle runs at a time. Every 10 seconds it also checks for **Sync now** clicks;
a click joins the running cycle instead of starting a second one.

How often things are fetched again: page 1 of a listing every `crawl_interval_seconds` (15 minutes),
deeper listing pages every `deep_crawl_interval_seconds` (6 hours), pages reached by following a
link every week (`CRAWL_LINK_INTERVAL`). Set the first two per source in `sources.yaml`.

Watch it (the end-of-cycle report is logged with a `[CRAWL]` prefix):

```powershell
npm run infra:logs
```

Or trigger a crawl from the UI with **Sync now** on the Leaks page. All the settings, with their
defaults, are documented in `.env.example`. `CRAWL_ENGINE=legacy` switches back to the original
source-at-a-time crawler; `npm run intel -- run` always uses that one.

---

## Useful commands

| What | Command |
|---|---|
| Start everything | `npm run infra:up:full` |
| Stop everything | `npm run infra:down` |
| Check containers | `npm run infra:ps` |
| Follow logs | `npm run infra:logs` |
| Crawl | `npm run intel -- run` |
| Enable / disable a source | `npm run intel -- sources enable <slug>` / `disable <slug>` |
| Back up the database | `npm run infra:backup` |
| Fill summaries/types from stored pages | `npm run intel -- backfill-descriptions --apply` |
| Fetch the public feeds now | `npm run intel -- feeds` |
| Remove sources deleted from sources.yaml | `npm run intel -- sources sync --prune` |
| Browse the database | `npm run db:studio` |

> An empty dashboard means collection hasn't run yet, not that the app is broken — there is no
> demo data, everything shown is collected. Enable a source and crawl.

---

## Developing (hot reload)

Run the datastores in Docker, the API and web app on your host.

First time on a clean clone:

```powershell
npm install
```

```powershell
Copy-Item .env.example .env
```

Generate a secret and paste it into `.env` as `AUTH_SECRET=`:

```powershell
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

```powershell
npm run db:migrate
```

Then start the three pieces (each in its own terminal):

```powershell
npm run infra:up
```

```powershell
npm run api:dev
```

```powershell
npm run web:dev
```

API on :5000, web on **http://localhost:5173** with hot reload. Vite proxies `/api` to the API,
so the browser sees one origin.

Verify a change:

```powershell
npm run typecheck
npm run build
npm test -w @leak/db
```

---

## If something's wrong

- **`failed to connect to the docker API`** — Docker Desktop isn't running. Start it, retry.
- **App shows "failed to load" or won't open** — the API container isn't up (it serves the web
  app too). Fix with `npm run infra:up:full`.
- **Sign-in fails with "Invalid origin"** — `APP_URL` in `.env` points somewhere other than the
  address in your browser. Remove it to fall back to http://localhost:8080.
- **`password authentication failed for user "leak"`** — confirm `DATABASE_URL` in `.env` says port **5433**, not 5432.
- **Every source fails to crawl** — check Tor: `npm run infra:ps` (the `tor` row should be healthy).
- **Nothing is being crawled** — sources ship disabled (`sources enable <slug>`), and nothing is
  fetched until a URL is due. Sync now forces everything regardless.
- **Healthcheck on the Raspberry Pi** — run `bash scripts/pi-health.sh` on the Pi.
- **Port already in use** — change `API_PORT` / `WEB_PORT` in `.env`.

---

## Ports

| Service | URL |
|---|---|
| Web app + API | http://localhost:8080 — the API container serves both |
| API (dev only) | http://localhost:5000, with the Vite dev server on :5173 |
| Postgres | `localhost:5433` |
| Redis | `localhost:6379` |
| Tor | internal only |
