# Start here

Quick path: get the app running, log in, turn on crawling, watch data arrive.

**How it works and what each folder does:** [ARCHITECTURE.md](ARCHITECTURE.md)
**Rebuild history and known issues:** [ROADMAP.md](ROADMAP.md)

---

## 1. Start everything

From the repo root (`C:\Users\ponsr\Desktop\leak-monitoring`):

```bash
npm run infra:up:full
```

Six containers: Postgres, Redis, Tor, the API, the web app, and the collection worker.
First run builds the images and takes a few minutes; after that it's seconds.

Check they're all up:

```bash
docker compose --env-file .env -f infra/docker-compose.yml --profile full ps
```

You want six rows, each `Up ... (healthy)`.

> **Docker Desktop must be running first.** If you get
> `failed to connect to the docker API`, open Docker Desktop and wait for it to finish
> starting, then retry.

---

## 2. Open the app

**http://localhost:8080**

### Log in

| | |
|---|---|
| Email | `analyst@example.com` |
| Password | `correct-horse-battery` |

> ⚠️ **This is a throwaway local development account**, created while testing the API. It
> only exists in your local Postgres container. Do not reuse this password anywhere, and
> replace this account before the app is reachable by anyone else.

### Or make your own

Click **Create one** on the sign-in screen. Password must be at least 12 characters.

There is no public sign-up gate yet — anyone who can reach the app can create an account.
Before exposing this beyond localhost, set `disableSignUp: true` in
[apps/api/src/auth.ts](apps/api/src/auth.ts) and provision accounts deliberately.

---

## 3. Turn on crawling

Sources ship **disabled**. Nothing is fetched until you enable it — crawling live ransomware
infrastructure over Tor is a deliberate act, not a side effect of starting the stack.

Every `intel ...` command below is run as:

```bash
npm run intel -- <command>
```

The `--` passes the rest through to the CLI. For example:

```bash
npm run intel -- sources list --all
```

> **Why not a bare `docker compose run` shortcut?**
> `docker compose run --rm worker` starts only the worker's `depends_on` services —
> postgres, redis and tor. It does **not** start `api` or `web`. So after
> `npm run infra:down`, a raw shortcut brings the stack back up *half-complete*: the
> database works, the website returns "failed to load", and nothing tells you why.
> `npm run intel` does `up -d` on the full profile first, so the stack is always whole.

<details>
<summary>Prefer a shell shortcut? Make it a safe one</summary>

**PowerShell:**

```powershell
function intel { npm run intel -- @args }
```

**Bash / Git Bash:**

```bash
alias intel='npm run intel --'
```
</details>

### See what's available

```bash
npm run intel -- sources list --all
```

32 sources — all probed and responding as of 2026-08-14. Dead addresses were removed.

### Enable one and crawl it

```bash
npm run intel -- sources enable lockbit
```

```bash
npm run intel -- run --source lockbit
```

Takes 1–3 minutes over Tor. You'll see `page processed found=… new=… seen_again=…`,
then a summary.

### Crawl everything enabled

```bash
npm run intel -- run
```

### Check what happened

```bash
npm run intel -- status
```

Then reload **http://localhost:8080** — new leaks appear on Overview and Leaks. The
dashboard refreshes itself every 60 seconds.

---

## 4. Automatic crawling

Already running. The worker has a cron schedule built in — **every hour at :17** it crawls
every enabled source. You don't need to do anything.

Watch it:

```bash
npm run infra:logs
```

Trigger a full run immediately instead of waiting:

```bash
npm run intel -- run
```

---

## What to expect from a real crawl

**Sources decay.** Of the 83 addresses inherited from the old notebooks, 30 still responded
when probed on 2026-08-14; the other 53 were removed. Leak sites rotate addresses and get
seized, so expect this list to rot. Failures are recorded and surface as `degraded` /
`failing` on the **Sources** page.

**Extraction quality is currently poor.** The pipeline fetches, parses, deduplicates and
loads correctly, but on dense index pages it mis-pairs victim names with domains from
neighbouring listings. Treat the crawled rows as a demonstration that the plumbing works,
not as reliable intelligence yet. See the "First live crawl" section of
[ROADMAP.md](ROADMAP.md) for the cause and the fix.

---

## Useful commands

| What | Command |
|---|---|
| Start everything | `npm run infra:up:full` |
| Stop everything | `npm run infra:down` |
| Follow logs | `npm run infra:logs` |
| Run a pipeline command | `npm run intel -- <cmd>` — brings the full stack up first |
| Restart just the API | `docker compose --env-file .env -f infra/docker-compose.yml --profile full up -d api` |
| Back up the database | `npm run infra:backup` |
| Browse the database | `npm run db:studio` |
| Add demo data | `npm run db:seed` — *adds* 144 demo leaks if absent; safe to re-run, and does not remove crawled rows |

### Enable / disable sources

```bash
npm run intel -- sources enable <slug>
```

```bash
npm run intel -- sources disable <slug>
```

```bash
npm run intel -- sources enable --all
```

### Test extraction without touching Tor

```bash
cd services/intel && uv run intel extract-file tests/fixtures/sample_leak_page.txt --group lockbit
```

---

## If something's wrong

**`failed to connect to the docker API`** — Docker Desktop isn't running. Start it, wait for
the whale icon to settle, retry.

**Website shows "failed to load" after running an `intel` command** — the api and web
containers probably aren't running. `docker compose run` starts only postgres, redis and
tor, so this happens if you ran `infra:down` and then used a raw `docker compose run`
shortcut. Check with `... ps`, and fix with:

```bash
npm run infra:up:full
```

Using `npm run intel` instead of a raw shortcut prevents this.

**Page loads but everything shows an error** — the API is down:

```bash
docker logs leakmon-api --tail 30
```

**502 from the app** — nginx lost the API. Restart the web container:

```bash
docker compose --env-file .env -f infra/docker-compose.yml --profile full up -d web
```

**`password authentication failed for user "leak"`** — something other than the container is
answering on the Postgres port. Confirm `DATABASE_URL` in `.env` says **5433**, not 5432.

**Crawl reports every source failed** — check Tor is healthy:

```bash
docker compose --env-file .env -f infra/docker-compose.yml --profile full ps tor
```

**Port already in use** — change `API_PORT` in `.env`, or the `8080:80` / `5433:5432`
mappings in [infra/docker-compose.yml](infra/docker-compose.yml).

---

## Developing (hot reload)

The Docker stack is for running the app. To *change* it, run the datastores in Docker and the
API and web app on your host:

```bash
npm run infra:up
```

```bash
npm run api:dev
```

```bash
npm run web:dev
```

API on :5000, web on **http://localhost:5173** with hot reload. Vite proxies `/api` to the
API, so there is still only one origin in the browser.

First time on a clean clone:

```bash
npm install && cp .env.example .env
```

Then set `AUTH_SECRET` in `.env`:

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

```bash
npm run db:migrate && npm run db:seed
```

## Verifying

```bash
npm run typecheck && npm run build && npm test -w @leak/db
```

Full API smoke test (start the API first, then in another terminal):

```bash
bash scripts/smoke-api.sh
```

Expect `PASS: 36   FAIL: 0`.

Python side:

```bash
cd services/intel && uv run pytest && uv run ruff check intel tests
```

84 tests. Five need Postgres and skip without it.

## Better extraction

The default extractor is rule-based and needs no ML. For zero-shot NER (pulls torch, ~2 GB):

```bash
cd services/intel && uv sync --extra ml
```

```bash
npm run intel -- run --source lockbit --extractor gliner
```

---

## Ports

| Service | URL |
|---|---|
| Web app | http://localhost:8080 |
| API (direct) | http://localhost:5000 — only in dev mode; in Docker it's internal |
| Postgres | `localhost:5433` |
| Redis | `localhost:6379` |
| Tor | internal only, deliberately not published to the host |
