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

Already running — nothing to do. The worker sweeps every 5 minutes for sources whose interval
has elapsed, and every 10 seconds for anything the **Sync now** button has queued.

Watch it:

```powershell
npm run infra:logs
```

Or trigger a crawl from the UI with **Sync now** on the Leaks page.

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
