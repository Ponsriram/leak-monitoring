# Expose the local full stack on a FREE public Cloudflare "quick tunnel" URL.
#
#   powershell -File infra/tunnel.ps1
#
# What it does:
#   1. Starts a Cloudflare quick tunnel pointed at the web container (http://localhost:WEB_PORT).
#   2. Captures the public https://<random>.trycloudflare.com URL it hands back.
#   3. Writes that URL into .env as APP_URL and recreates the api container so Better Auth
#      trusts the new origin (without this, login is rejected as a cross-origin request).
#   4. Prints the public URL and keeps running. Ctrl-C to stop the tunnel (site goes offline).
#
# The URL is NOT stable: every run gives a new address. For a fixed URL you need a domain on
# Cloudflare (see DEPLOY-CLOUDFLARE.md, "Stable URL").

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo ".env"
$compose = Join-Path $repo "infra/docker-compose.yml"

# Resolve cloudflared. A just-installed cloudflared isn't always on PATH in the current
# shell, so fall back to the known install locations.
$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) {
  $cloudflared = Get-ChildItem `
    'C:\Program Files (x86)\cloudflared\cloudflared.exe', `
    'C:\Program Files\cloudflared\cloudflared.exe' `
    -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $cloudflared) {
  Write-Host "!! cloudflared not found. Install it: winget install Cloudflare.cloudflared" -ForegroundColor Red
  exit 1
}

# WEB_PORT from .env, default 8080
$port = 8080
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^\s*WEB_PORT\s*=\s*(\d+)' | Select-Object -First 1
  if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

# Sanity: is the web container answering locally?
try {
  Invoke-WebRequest -Uri "http://localhost:$port" -UseBasicParsing -TimeoutSec 5 | Out-Null
} catch {
  Write-Host "!! Nothing is answering on http://localhost:$port" -ForegroundColor Yellow
  Write-Host "   Start the stack first:  npm run infra:up:full" -ForegroundColor Yellow
  exit 1
}

# Start the quick tunnel, capturing its output so we can read the URL back.
$out = Join-Path $env:TEMP "cloudflared-leakmon.out"
$err = Join-Path $env:TEMP "cloudflared-leakmon.err"
Remove-Item $out, $err -ErrorAction SilentlyContinue

Write-Host "Starting Cloudflare quick tunnel -> http://localhost:$port ..." -ForegroundColor Cyan
# --protocol http2 forces the tunnel over TCP:443 instead of QUIC (UDP:7844). Many home
# routers, VPNs and campus/corporate networks drop outbound UDP, which makes the default
# QUIC transport hang and visitors get "Error 1033". http2 avoids that.
$proc = Start-Process -FilePath $cloudflared `
  -ArgumentList @("tunnel", "--url", "http://localhost:$port", "--protocol", "http2") `
  -PassThru -NoNewWindow -RedirectStandardOutput $out -RedirectStandardError $err

# Poll the output for the trycloudflare.com URL (usually appears within a few seconds).
$url = $null
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Seconds 1
  foreach ($f in @($err, $out)) {
    if (Test-Path $f) {
      $hit = Select-String -Path $f -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($hit) { $url = $hit.Matches[0].Value; break }
    }
  }
  if ($url) { break }
}

if (-not $url) {
  Write-Host "!! Timed out waiting for the tunnel URL. Last output:" -ForegroundColor Red
  Get-Content $err, $out -ErrorAction SilentlyContinue | Select-Object -Last 20
  if ($proc -and -not $proc.HasExited) { $proc.Kill() }
  exit 1
}

Write-Host "Public URL: $url" -ForegroundColor Green

# Wire the URL into .env as APP_URL (compose derives CORS_ORIGINS + AUTH_URL from it).
$lines = if (Test-Path $envFile) { Get-Content $envFile } else { @() }
if ($lines -match '^\s*APP_URL\s*=') {
  $lines = $lines | ForEach-Object { if ($_ -match '^\s*APP_URL\s*=') { "APP_URL=$url" } else { $_ } }
} else {
  $lines += "APP_URL=$url"
}
Set-Content -Path $envFile -Value $lines -Encoding utf8
Write-Host "Wrote APP_URL=$url into .env" -ForegroundColor Cyan

# Recreate the api container so it picks up the new origin. (web is same-origin, no change.)
Write-Host "Recreating the api container with the new origin ..." -ForegroundColor Cyan
& docker compose --env-file $envFile -f $compose --profile full up -d api

Write-Host ""
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host " LIVE:  $url" -ForegroundColor Green
Write-Host " Anyone with this link can reach the app from any device." -ForegroundColor Green
Write-Host " Keep this window open. Ctrl-C stops the tunnel and takes it offline." -ForegroundColor Green
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host ""

# Stream tunnel logs / keep the process in the foreground until Ctrl-C.
try {
  while (-not $proc.HasExited) { Start-Sleep -Seconds 2 }
} finally {
  if ($proc -and -not $proc.HasExited) { $proc.Kill() }
  Write-Host "Tunnel stopped. The public URL is now dead." -ForegroundColor Yellow
}
