#!/usr/bin/env bash
#
# Is the collection side healthy? Run this ON the Raspberry Pi, from the repo root:
#
#   bash scripts/pi-health.sh
#
# Read-only. Checks, in the order a problem usually shows up:
#   1. the Pi itself        (temperature, throttling, memory, disk)
#   2. the containers       (redis, tor, worker running and healthy)
#   3. Tor                  (bootstrapped, and an onion service answers through it)
#   4. the worker           (cron ticking, last crawl run, recent errors)
#   5. the database         (recent crawl_runs, from the worker's point of view)
#
# Exits non-zero if anything is wrong, so it can sit in cron or a monitor.

set -uo pipefail

WORKER="${WORKER:-leakmon-worker}"
TOR="${TOR:-leakmon-tor}"
REDIS="${REDIS:-leakmon-redis}"
fail=0

ok()   { printf '  [ ok ] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; fail=1; }

echo "== 1. Pi"
if command -v vcgencmd >/dev/null 2>&1; then
  temp=$(vcgencmd measure_temp | sed "s/[^0-9.]//g")
  if awk "BEGIN{exit !($temp < 75)}"; then ok "temperature ${temp}C"; else bad "temperature ${temp}C (throttles at 80C)"; fi
  throttled=$(vcgencmd get_throttled | cut -d= -f2)
  if [ "$throttled" = "0x0" ]; then ok "no under-voltage or throttling"; else warn "throttled flags $throttled (power supply or heat)"; fi
fi
avail=$(free -m | awk '/^Mem:/{print $7}')
if [ "${avail:-0}" -ge 300 ]; then ok "memory available ${avail}MB"; else warn "memory available only ${avail}MB (browser sources need ~500MB each)"; fi
used=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
if [ "$used" -lt 90 ]; then ok "disk ${used}% used"; else bad "disk ${used}% used"; fi

echo "== 2. Containers"
for name in "$REDIS" "$TOR" "$WORKER"; do
  state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$name" 2>/dev/null || echo n/a)
  if [ "$state" = running ] && { [ "$health" = healthy ] || [ "$health" = n/a ]; }; then
    ok "$name running ($health)"
  else
    bad "$name is $state / $health"
  fi
done
restarts=$(docker inspect -f '{{.RestartCount}}' "$WORKER" 2>/dev/null || echo 0)
[ "${restarts:-0}" -le 3 ] && ok "worker restarts: $restarts" || warn "worker has restarted $restarts times"

echo "== 3. Tor"
if docker logs "$TOR" 2>&1 | grep -q "Bootstrapped 100%"; then ok "Tor bootstrapped"; else bad "Tor has not reached 'Bootstrapped 100%'"; fi
# A real request through the proxy: check.torproject.org says whether the exit is Tor.
via=$(docker exec "$TOR" curl -s --max-time 40 --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip 2>/dev/null || true)
if echo "$via" | grep -q '"IsTor":true'; then ok "traffic leaves through Tor"; else bad "no answer through the Tor proxy"; fi

echo "== 4. Worker"
if docker exec "$REDIS" redis-cli ping 2>/dev/null | grep -q PONG; then ok "redis answers"; else bad "redis does not answer"; fi
recent=$(docker logs --since 30m "$WORKER" 2>&1)
if echo "$recent" | grep -q "run complete"; then
  ok "a crawl run completed in the last 30 minutes"
  echo "$recent" | grep -E "run starting|run complete|overrun" | tail -4 | sed 's/^/         /'
elif echo "$recent" | grep -q "no sources due"; then
  ok "sweeping; nothing was due"
else
  warn "no crawl activity in the last 30 minutes (are any sources enabled? 'intel sources list')"
fi
errors=$(echo "$recent" | grep -ciE '"level": ?"(error|critical)"|Traceback')
if [ "$errors" -eq 0 ]; then ok "no errors in the last 30 minutes"; else warn "$errors error lines in the last 30 minutes: docker logs --since 30m $WORKER | grep -i error"; fi

echo "== 5. Source health (what the dashboard's Sources page shows)"
docker exec "$WORKER" intel sources list 2>/dev/null | head -40 || warn "could not run 'intel sources list'"

echo
if [ "$fail" -eq 0 ]; then echo "Healthy."; else echo "Something needs attention (see [FAIL] above)."; fi
exit "$fail"
