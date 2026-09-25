<div align="center">

# LEAK MONITORING

### *Ransomware leak-site monitoring and exposure intelligence*

[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18-4169E1?style=flat&logo=postgresql&logoColor=white)]()
[![Fastify](https://img.shields.io/badge/Fastify-5-000000?style=flat&logo=fastify&logoColor=white)]()
[![React](https://img.shields.io/badge/React-19-20232A?style=flat&logo=react&logoColor=61DAFB)]()
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat&logo=python&logoColor=white)]()
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat&logo=docker&logoColor=white)]()

</div>

---

## ▶ Run it

```bash
npm run infra:up:full
```

Open **http://localhost:8080**. Full walkthrough, credentials and crawling instructions:
**[START.md](START.md)**

| Document | What's in it |
|---|---|
| **[START.md](START.md)** | Run the app, log in, turn on crawling, troubleshooting |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | How it works, folder by folder, why, and known limitations |

---

## What it does

Monitors ransomware leak sites on Tor, extracts victim disclosures, and surfaces them
through a threat-intel console.

- **Automated collection** — crawls monitored onion services on a schedule, over Tor
- **Content-hash short circuit** — unchanged pages cost one fetch and stop there
- **Deduplication** — re-running never duplicates; `first_seen_at` is written once, so
  "what's new" is actually answerable
- **Dashboard** — leak volume over time, activity by group, live source health
- **World Incidents** — General, Ransomware and Dark Web tables, enriched with WHOIS, site
  status and web technologies; a live threat map
- **Bulk Intelligence** — company search with an on-demand passive lookup, IOC feeds
  (abuse.ch ThreatFox and URLhaus) with registry WHOIS
- **Incident summaries & types** — each listing's own description from the leak site, and
  multi-valued incident types (Ransomware, Data Leak, Data Breach, Hacked, Sale, …) derived
  from its status, stated size and wording

## How it's built

```
apps/web      React 19 + Vite + TanStack Query      the dashboard
apps/api      Fastify 5 + Better Auth               stateless REST API
packages/db   Drizzle + PostgreSQL 18               schema, migrations, types
services/intel  Python + httpx/Playwright + arq     crawl, extract, load
infra/        Docker Compose + Tor sidecar          runs the whole thing
```

One command brings up six containers: Postgres, Redis, Tor, API, web, and the collection
worker. See [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow and design decisions.

## Status

Working and verified end to end — the pipeline crawls live onion services, deduplicates
correctly, and serves the results.

**Extraction quality is the open problem.** On dense index pages the linker mis-pairs victim
names with domains from neighbouring listings, so crawled rows are not yet reliable
intelligence. See *Known limitations* in [ARCHITECTURE.md](ARCHITECTURE.md).

## ⚠️ Before you expose this

- **Accounts are provisioned, not signed up.** Public sign-up is disabled; create accounts
  with `npm run user:provision -w @leak/api` (see [START.md](START.md)).
- **Change the defaults** — `POSTGRES_PASSWORD` and `AUTH_SECRET` in `.env`, and replace the
  local test account.
- **Sources ship disabled.** Crawling live criminal infrastructure over Tor is a deliberate
  decision about your legal and operational position. Nothing is fetched until you enable it.

## Ethical use

For authorised cybersecurity research and defensive threat intelligence only. You are
responsible for compliance with local law, your organisation's policy, and the terms of any
source you monitor.
