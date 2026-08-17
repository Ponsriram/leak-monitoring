#!/usr/bin/env bash
# Backend smoke test. Every check maps to a defect from the architecture review.
API=http://localhost:5000
JAR=$(mktemp)
pass=0; fail=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1 (expected '$2', got '$3')"; fail=$((fail+1)); fi
}

status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== auth guard =="
check "GET /api/leaks unauthenticated -> 401"        401 "$(status $API/api/leaks)"
check "GET /api/stats/summary unauth   -> 401"       401 "$(status $API/api/stats/summary)"
check "GET /api/sources unauth         -> 401"       401 "$(status $API/api/sources)"
check "GET /api/alerts unauth          -> 401"       401 "$(status $API/api/alerts)"
check "GET /healthz stays public       -> 200"       200 "$(status $API/healthz)"

echo "== registration =="
SIGNUP=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/api/auth/sign-up/email \
  -H 'content-type: application/json' \
  -d '{"email":"analyst@example.com","password":"correct-horse-battery","name":"Analyst"}')
echo "  sign-up returned $SIGNUP (200 first run, 4xx if already exists)"

check "short password rejected" 400 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/api/auth/sign-up/email \
  -H 'content-type: application/json' -d '{"email":"weak@example.com","password":"short","name":"W"}')"

echo "== sign in =="
LOGIN=$(status -c "$JAR" -X POST $API/api/auth/sign-in/email \
  -H 'content-type: application/json' \
  -d '{"email":"analyst@example.com","password":"correct-horse-battery"}')
check "sign-in -> 200" 200 "$LOGIN"
check "session cookie set" "yes" "$(grep -q 'session_token' "$JAR" && echo yes || echo no)"

check "wrong password rejected" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/api/auth/sign-in/email \
  -H 'content-type: application/json' -d '{"email":"analyst@example.com","password":"wrong-password-here"}')"

echo "== leaks =="
check "GET /api/leaks authenticated -> 200" 200 "$(status -b "$JAR" $API/api/leaks)"
TOTAL=$(curl -s -b "$JAR" "$API/api/leaks?limit=25" | node -pe 'JSON.parse(require("fs").readFileSync(0)).pagination.total')
RETURNED=$(curl -s -b "$JAR" "$API/api/leaks?limit=25" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.length')
check "total reports 144"            144 "$TOTAL"
check "but only 25 rows returned"    25  "$RETURNED"
check "limit=1000 rejected (cap)"    400 "$(status -b "$JAR" "$API/api/leaks?limit=1000")"
check "page 2 differs from page 1"   "yes" "$( [ "$(curl -s -b "$JAR" "$API/api/leaks?page=1&limit=5" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data[0].id')" != \
                                                "$(curl -s -b "$JAR" "$API/api/leaks?page=2&limit=5" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data[0].id')" ] && echo yes || echo no)"
check "filter by group works" "yes" "$(curl -s -b "$JAR" "$API/api/leaks?group=lockbit&limit=100" | node -pe 'const d=JSON.parse(require("fs").readFileSync(0)).data; d.length>0 && d.every(r=>r.actorGroup==="lockbit") ? "yes":"no"')"
check "full-text search works" "yes" "$(curl -s -b "$JAR" "$API/api/leaks?q=northwind&limit=50" | node -pe 'const d=JSON.parse(require("fs").readFileSync(0)).data; d.length>0 && d.every(r=>/northwind/i.test(r.victimName)) ? "yes":"no"')"
check "bad sort value rejected" 400 "$(status -b "$JAR" "$API/api/leaks?sort=drop_table")"
check "GET /api/leaks/999999 -> 404" 404 "$(status -b "$JAR" $API/api/leaks/999999)"

echo "== stats (the endpoints that used to return nothing) =="
check "leaks-per-day returns 30 zero-filled days" 30 "$(curl -s -b "$JAR" "$API/api/stats/leaks-per-day?days=30" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.length')"
check "leaks-per-day has non-zero totals" "yes" "$(curl -s -b "$JAR" "$API/api/stats/leaks-per-day?days=60" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.some(d=>d.total>0)?"yes":"no"')"
check "leaks-per-group returns 6 groups" 6 "$(curl -s -b "$JAR" "$API/api/stats/leaks-per-group" | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.length')"
# Checked before the field assertions below. When the response fails its own schema the
# route returns 500, and every field check then reports "expected 144, got undefined" —
# which reads like a counting bug rather than the serialization error it actually is.
check "summary responds 200" 200 "$(status -b "$JAR" $API/api/stats/summary)"
check "summary.totalLeaks = 144" 144 "$(curl -s -b "$JAR" $API/api/stats/summary | node -pe 'JSON.parse(require("fs").readFileSync(0)).totalLeaks')"
# lastCollectionAt is a timestamptz pulled through raw SQL, which skips Drizzle's column
# decoding and arrives as a string; it must still serialize as a date.
check "summary.lastCollectionAt serializes" "yes" "$(curl -s -b "$JAR" $API/api/stats/summary | node -pe 'const v=JSON.parse(require("fs").readFileSync(0)).lastCollectionAt; (v===null||!isNaN(Date.parse(v)))?"yes":"no"')"
check "summary.trackedGroups = 6"  6  "$(curl -s -b "$JAR" $API/api/stats/summary | node -pe 'JSON.parse(require("fs").readFileSync(0)).trackedGroups')"

echo "== sources (was 10 hardcoded fake rows) =="
check "GET /api/sources -> 6" 6 "$(curl -s -b "$JAR" $API/api/sources | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.length')"
# akira is seeded with 4 consecutive failures AND enabled=false, so it exercises the
# precedence rule: a source that is not being crawled reports `disabled`, not `failing`.
# A paused source has no current health to report — its failure count is history, and
# showing it as failing would put a permanent red row on the dashboard for a site nobody
# is crawling.
#
# This previously asserted "failing" and had been failing in CI since the seed was changed
# to ship every demo source disabled ("Demo rows must never be crawl targets" — seed.ts).
# The seed and the route agree; the assertion was the stale one.
check "disabled beats failing in health" "disabled" "$(curl -s -b "$JAR" $API/api/sources | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.find(s=>s.slug==="akira").health')"
# ...and the failure count is still reported, so the disabled state hides nothing.
# (Every seeded source is disabled by design, so `healthy`/`degraded`/`failing` cannot be
# exercised here without a fixture that invites the crawler at fake onion addresses. Those
# branches are covered by the source-health query in the Python storage tests instead.)
check "failure count survives being disabled" 4 "$(curl -s -b "$JAR" $API/api/sources | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.find(s=>s.slug==="akira").consecutiveFailures')"
# `every(leakCount > 0)` was the original assertion here and it PASSED while every count was
# wrong (an uncorrelated subquery returned 1 for every source). Assert the exact total instead:
# per-source counts must sum to the number of leaks that have a source.
LEAKCOUNT_SUM=$(curl -s -b "$JAR" $API/api/sources | node -pe 'JSON.parse(require("fs").readFileSync(0)).data.reduce((a,s)=>a+s.leakCount,0)')
TOTAL_LEAKS=$(curl -s -b "$JAR" $API/api/stats/summary | node -pe 'JSON.parse(require("fs").readFileSync(0)).totalLeaks')
check "leakCount sums to the leak total" "$TOTAL_LEAKS" "$LEAKCOUNT_SUM"

echo "== alerts =="
ALERT_ID=$(curl -s -b "$JAR" -X POST $API/api/alerts -H 'content-type: application/json' \
  -d '{"name":"Northwind watch","matchKind":"substring","matchValue":"NORTHWIND","channel":"email","target":"analyst@example.com"}' \
  | node -pe 'JSON.parse(require("fs").readFileSync(0)).id')
check "created alert has an id" "yes" "$( [ -n "$ALERT_ID" ] && [ "$ALERT_ID" != "undefined" ] && echo yes || echo no)"
check "matchValue normalised to lowercase" "northwind" "$(curl -s -b "$JAR" $API/api/alerts | node -pe 'JSON.parse(require("fs").readFileSync(0)).data[0].matchValue')"
check "email channel rejects a URL target" 400 "$(status -b "$JAR" -X POST $API/api/alerts -H 'content-type: application/json' \
  -d '{"name":"bad","matchKind":"exact","matchValue":"x","channel":"email","target":"http://not-an-email"}')"
check "unknown matchKind rejected" 400 "$(status -b "$JAR" -X POST $API/api/alerts -H 'content-type: application/json' \
  -d '{"name":"bad","matchKind":"regex","matchValue":"(a+)+$","channel":"email","target":"a@b.co"}')"
check "PATCH own alert -> 200" 200 "$(status -b "$JAR" -X PATCH $API/api/alerts/$ALERT_ID -H 'content-type: application/json' -d '{"enabled":false}')"
check "PATCH nonexistent -> 404" 404 "$(status -b "$JAR" -X PATCH $API/api/alerts/999999 -H 'content-type: application/json' -d '{"enabled":false}')"
check "GET /api/alerts/events -> 200" 200 "$(status -b "$JAR" $API/api/alerts/events)"
check "DELETE own alert -> 204" 204 "$(status -b "$JAR" -X DELETE $API/api/alerts/$ALERT_ID)"
check "DELETE again -> 404"     404 "$(status -b "$JAR" -X DELETE $API/api/alerts/$ALERT_ID)"

echo "== error shape =="
check "404 body has requestId" "yes" "$(curl -s $API/api/nope | node -pe 'JSON.parse(require("fs").readFileSync(0)).requestId?"yes":"no"')"

rm -f "$JAR"
echo
echo "PASS: $pass   FAIL: $fail"
[ "$fail" -eq 0 ]
