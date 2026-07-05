#!/usr/bin/env bash
# Weekly cron: apply frontfile in place, then stream-compress a backup.
# The serving LMDB is updated directly; no copy, no swap, no restart needed.
# LMDB's MVCC ensures readers see a consistent snapshot during the write.
#
# Rollback: tar xf /srv/docdb/docdb-<date>.tar.gz -C /srv/docdb
set -euo pipefail

# cron (like any non-interactive ssh invocation) never sources ~/.bashrc, so
# uv - installed at /root/.local/bin/uv - would otherwise be invisible here.
export PATH="/root/.local/bin:$PATH"

# shellcheck source=/dev/null
source /etc/docdb/credentials

SERVE_DIR="/srv/docdb"
LMDB_PATH="$SERVE_DIR/docdb.lmdb"
TIMESTAMP=$(date +%Y%m%d)
NEW_ARCHIVE="$SERVE_DIR/docdb-$TIMESTAMP.tar.gz"
KEEP_ARCHIVES=1

echo "[update_db] applying frontfile to $LMDB_PATH..."
# Runs on the host, not inside the api container: the api service mounts its
# LMDB volume read-only (docker-compose.yml), and docdb-apply-frontfile is
# only the last step of the pipeline anyway (download + sort still need to
# happen first). `make apply-frontfile` drives the whole thing and refuses
# to touch the backfile chain if the alias DB isn't already loaded.
make -C /opt/docdb apply-frontfile LMDB_OUT="$LMDB_PATH"
echo "[update_db] frontfile applied"

echo "[update_db] streaming backup → $NEW_ARCHIVE..."
tar -c -C "$SERVE_DIR" "$(basename "$LMDB_PATH")" | pigz -9 > "$NEW_ARCHIVE"
echo "[update_db] backup done ($(du -sh "$NEW_ARCHIVE" | cut -f1))"

find "$SERVE_DIR" -maxdepth 1 -name "docdb-*.tar.gz" \
    | sort -r \
    | tail -n +$((KEEP_ARCHIVES + 1)) \
    | xargs -r rm -f
echo "[update_db] old archives pruned (kept $KEEP_ARCHIVES)"

echo "[update_db] done"
curl -fsS -m 10 --retry 5 -o /dev/null "$HEALTHCHECKS_URL"
