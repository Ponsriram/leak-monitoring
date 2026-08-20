# Start here

Get the app running, log in, turn on crawling, watch data arrive.

- **How it works:** [ARCHITECTURE.md](ARCHITECTURE.md)
- **History & known issues:** [ROADMAP.md](ROADMAP.md)

All commands are PowerShell, run from the repo root (`C:\Users\ponsr\Desktop\leak-monitoring`).

---

## 1. Start everything

```powershell
npm run infra:up:full
```

Starts six containers: Postgres, Redis, Tor, the API, the web app, and the worker. The first
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

Or click **Create one** on the sign-in screen (password ≥ 12 characters). There's no sign-up
gate yet — anyone who can reach the app can register.

---

## 3. Turn on crawling

Sources ship **disabled**. Reaching them means connecting to live criminal infrastructure over
Tor — a deliberate act, not a side effect of starting the stack.

Pipeline commands run through `npm run intel -- <command>`. See what's available:

```powershell
npm run intel -- sources list --all
```

> 41 sources — 32 probed and responding as of 2026-08-14, plus 9 added 2026-08-19 that are
> **pending their first reachability probe** (bottom of `sources.yaml`). Enable one and run it
> to check; `consecutive_failures` climbs immediately for a dead address.

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

Then reload **http://localhost:8080** — new leaks appear on Overview and Leaks, and the **Map**
tab plots them by country. The dashboard refreshes every 60 seconds.

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
- **App shows "failed to load"** — the API/web containers aren't up. Fix with `npm run infra:up:full`.
- **502 from the app** — restart the web container: `npm run infra:restart:web`.
- **`password authentication failed for user "leak"`** — confirm `DATABASE_URL` in `.env` says port **5433**, not 5432.
- **Every source fails to crawl** — check Tor: `npm run infra:ps` (the `tor` row should be healthy).
- **Port already in use** — change `API_PORT` / `WEB_PORT` in `.env`.

---

## Ports

| Service | URL |
|---|---|
| Web app | http://localhost:8080 |
| API (dev only) | http://localhost:5000 — internal in Docker |
| Postgres | `localhost:5433` |
| Redis | `localhost:6379` |
| Tor | internal only |
