# Deploy on a free Cloudflare public URL

Run the whole stack on your laptop and expose it to the internet through a **Cloudflare
Tunnel** — no server to rent, no ports to open, automatic HTTPS. Anyone with the link can
reach the app from any device, for **$0**.

**How it works:** `cloudflared` runs on your laptop and dials *out* to Cloudflare. Cloudflare
gives you a public URL and forwards visitors down that connection to your local `web`
container (port 8080), whose nginx already serves the frontend and proxies `/api` to the API.
One tunnel exposes the whole app; Postgres, Redis and Tor stay private on your machine.

---

## Prerequisites (one time)

- **Docker Desktop** installed and running.
- **cloudflared** installed — already done via `winget install Cloudflare.cloudflared`.
  Verify: `cloudflared --version`.
- A real `AUTH_SECRET` in `.env` (already set).

---

## Run it

### 1. Start Docker Desktop
Wait until it says "Engine running."

### 2. Start the full stack
```powershell
npm run infra:up:full
```
Confirm all six containers are healthy:
```powershell
npm run infra:ps
```
Check it works locally at **http://localhost:8080** before exposing it.

### 3. Go public
```powershell
npm run tunnel
```
This starts the tunnel, grabs the public URL, writes it into `.env` as `APP_URL`, and
recreates the `api` container so login works on the new address. When it finishes it prints:

```
LIVE:  https://<random-words>.trycloudflare.com
```

That is your public link. **Keep the window open** — closing it (or Ctrl-C) takes the site
offline. Share the URL; it works from any device, anywhere.

### 4. Stop
- Ctrl-C in the tunnel window → site goes offline (containers keep running).
- `npm run infra:down` → stop the whole stack.

---

## Before you share the link — close public sign-up

Right now **anyone who opens the link can register an account** (`apps/api/src/auth.ts`,
`disableSignUp: false`). Lock it down once your own account exists:

1. Start the stack and open the app (locally or via the tunnel).
2. Create *your* account (**Create one** on the sign-in screen, password ≥ 12 chars).
3. Edit `apps/api/src/auth.ts` and set:
   ```ts
   disableSignUp: true,
   ```
4. Rebuild the API so the change takes effect:
   ```powershell
   docker compose --env-file .env -f infra/docker-compose.yml --profile full up -d --build api
   ```

Now the registration endpoint is closed and only accounts you already created can log in.

---

## Important limits of the free setup

- **Your laptop is the server.** The site is up only while the laptop is on, awake, online,
  and the tunnel window is open. Sleep or shutdown = site down. (In Windows power settings,
  set the laptop to stay awake if you want it reachable while unattended.)
- **The URL changes every run.** Each `npm run tunnel` gives a brand-new
  `*.trycloudflare.com` address. The script rewrites `.env` for you, so login keeps working —
  but anyone holding the old link loses access. Fine for demos and sharing on the spot; not
  for a permanent address.
- **It's genuinely public.** Anyone with the link can reach it. Close sign-up (above) before
  sharing, and remember the app drives live crawls of criminal infrastructure over Tor.

---

## Troubleshooting

**Visitors see "Error 1033 — Cloudflare Tunnel error."**
The public hostname exists but no `cloudflared` is connected. On this machine the cause is a
blocked transport: cloudflared's default **QUIC** protocol needs outbound **UDP port 7844**,
which this network drops (`Failed to dial a quic connection: timeout`). The fix — already
baked into `infra/tunnel.ps1` — is to force **HTTP/2 over TCP:443**:
```powershell
cloudflared tunnel --url http://localhost:8080 --protocol http2
```
A healthy start logs `Initial protocol http2` and `Registered tunnel connection`.

**Login fails / "invalid origin" after the URL changed.**
`APP_URL` in `.env` no longer matches the live tunnel URL. Re-run `npm run tunnel` (it
rewrites `APP_URL` and recreates `api`), or set it by hand and
`docker compose --env-file .env -f infra/docker-compose.yml --profile full up -d api`.

## Stable URL (optional, ~$10/yr — the only paid part)

A fixed address like `leaks.yourdomain.com` needs a domain on Cloudflare:

1. Buy/transfer a domain into your Cloudflare account (Cloudflare Registrar is at cost).
2. Authenticate: `cloudflared tunnel login`
3. Create a named tunnel: `cloudflared tunnel create leakmon`
4. Route your hostname to it: `cloudflared tunnel route dns leakmon leaks.yourdomain.com`
5. Point it at the web container in `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: leakmon
   credentials-file: C:\Users\ponsr\.cloudflared\<TUNNEL-ID>.json
   ingress:
     - hostname: leaks.yourdomain.com
       service: http://localhost:8080
     - service: http_status:404
   ```
6. Set `APP_URL=https://leaks.yourdomain.com` in `.env` **once**, then
   `npm run infra:up:full`.
7. Run it: `cloudflared tunnel run leakmon` (or install it as a Windows service with
   `cloudflared service install` so it survives reboots).

Because the address never changes, you set `APP_URL` a single time and skip the
`npm run tunnel` helper entirely.
