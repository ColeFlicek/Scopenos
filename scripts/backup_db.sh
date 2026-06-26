#!/usr/bin/env bash
# Backup the production Scopenos database using pg_dump.
#
# Runs pg_dump inside a throwaway Docker container so no Postgres client
# tools need to be installed on the host (tested on Unraid).
#
# Keeps the last BACKUP_KEEP (default 7) dumps in custom pg_dump format
# (.dump), which pg_restore can load selectively by table or function.
#
# Required env:
#   DATABASE_URL   postgresql://user:pass@host/dbname
#                  Use 'localhost' or '127.0.0.1' — the container runs with
#                  --network=host so it shares the host's network stack.
#
# Optional env:
#   BACKUP_DIR     destination directory  (default: /mnt/user/backups/scopenos)
#   BACKUP_KEEP    number of dumps to keep (default: 7)
#   PG_IMAGE       Postgres Docker image   (default: postgres:16-alpine)
#
# Usage:
#   DATABASE_URL="postgresql://scopenos:...@localhost/scopenos" bash scripts/backup_db.sh
#
# Install as daily cron in Unraid's Settings → Scheduled Tasks (or crontab):
#   0 2 * * * DATABASE_URL="postgresql://scopenos:...@localhost/scopenos" \
#             /mnt/user/appdata/scopenos/scripts/backup_db.sh \
#             >> /var/log/scopenos-backup.log 2>&1

set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set}"
BACKUP_DIR="${BACKUP_DIR:-/mnt/user/backups/scopenos}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/scopenos_${TIMESTAMP}.dump"

echo "[backup] $(date -u +%FT%TZ) starting dump → $BACKUP_FILE"

# --network=host lets the container reach Postgres on the host's localhost.
# The dump is written to stdout and redirected to the backup file so nothing
# is left inside the container.
docker run --rm \
    --network=host \
    -e PGPASSWORD \
    "$PG_IMAGE" \
    pg_dump \
        --format=custom \
        --no-acl \
        --no-owner \
        --compress=9 \
        "$DATABASE_URL" \
    > "$BACKUP_FILE"

SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
echo "[backup] done. size=${SIZE}"

# Rotate: delete oldest dumps beyond BACKUP_KEEP
EXCESS=$(ls -1t "$BACKUP_DIR"/scopenos_*.dump 2>/dev/null | tail -n "+$((BACKUP_KEEP + 1))")
if [ -n "$EXCESS" ]; then
    echo "$EXCESS" | xargs rm -v
fi

RETAINED=$(ls -1 "$BACKUP_DIR"/scopenos_*.dump 2>/dev/null | wc -l)
echo "[backup] $RETAINED dump(s) retained in $BACKUP_DIR"
