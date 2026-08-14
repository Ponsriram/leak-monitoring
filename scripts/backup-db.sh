#!/usr/bin/env bash
#
# Postgres backup with retention.
#
#   bash scripts/backup-db.sh              # write a dump to ./backups
#   BACKUP_DIR=/mnt/x bash scripts/...     # somewhere else
#   RETAIN_DAYS=30 bash scripts/...        # keep longer (default 14)
#
# Runs pg_dump INSIDE the container, so no local postgresql-client is needed and the
# client version always matches the server.
#
# Custom format (-Fc), not plain SQL: it is compressed, and pg_restore can then restore a
# single table or reorder objects — neither of which a plain dump allows.

set -euo pipefail

CONTAINER="${CONTAINER:-leakmon-postgres}"
DB_USER="${DB_USER:-leak}"
DB_NAME="${DB_NAME:-leakmon}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "error: container '$CONTAINER' not found. Start it with: npm run infra:up" >&2
  exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
  echo "error: container '$CONTAINER' is not running." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/leakmon-${STAMP}.dump"

echo "Backing up ${DB_NAME} -> ${TARGET}"

# Write to a .partial first and rename on success, so an interrupted run never leaves a
# truncated file that looks like a valid backup.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner --no-acl \
  > "${TARGET}.partial"

mv "${TARGET}.partial" "$TARGET"

SIZE="$(du -h "$TARGET" | cut -f1)"
echo "Wrote ${TARGET} (${SIZE})"

# Verify the dump is readable rather than trusting that pg_dump exited 0.
if docker exec -i "$CONTAINER" pg_restore --list < "$TARGET" >/dev/null 2>&1; then
  echo "Verified: pg_restore can read the archive."
else
  echo "WARNING: pg_restore could not read ${TARGET} — treat this backup as suspect." >&2
  exit 1
fi

echo "Pruning backups older than ${RETAIN_DAYS} days…"
find "$BACKUP_DIR" -name 'leakmon-*.dump' -type f -mtime "+${RETAIN_DAYS}" -print -delete

echo
echo "Restore with:"
echo "  docker exec -i ${CONTAINER} pg_restore -U ${DB_USER} -d ${DB_NAME} --clean --if-exists < ${TARGET}"
