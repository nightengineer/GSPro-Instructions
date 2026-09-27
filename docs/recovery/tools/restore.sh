#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="${GSPRO_APP_ROOT:-/opt/gspro}" # CHANGEABLE: restored application path
DB_ADMIN="${GSPRO_DB_ADMIN:-gspro}"     # CHANGEABLE: administrative database role
DB_NAME="${GSPRO_DB_NAME:-gspro}"       # CHANGEABLE: database name
STATE_VOLUME="${GSPRO_STATE_VOLUME:-gspro-stage5-vps_telegram_state}" # CHANGEABLE: target SQLite volume
TOOLS_IMAGE="${GSPRO_TOOLS_IMAGE:-postgres:17-alpine}" # Saved in each full snapshot
SNAPSHOT="${1:?Pass an extracted verified snapshot directory}"
exec 8>"$APP_ROOT/.deploy.lock";flock -n 8 || { echo 'Refused: deployment or backup is active'; exit 1; }
exec 9>/run/lock/gspro-backup.lock;flock -n 9 || { echo 'Refused: backup or recovery is active'; exit 1; }
cd "$SNAPSHOT"
sha256sum -c SHA256SUMS
dc(){ "$APP_ROOT/deploy/dc" "$@"; }
for svc in web telegram-admin telegram-bot; do
 running=$(dc ps --status running -q "$svc")
 [[ -z "$running" ]] || { echo "Refused: $svc is running"; exit 1; }
done
tables=$(dc exec -T db psql -U "$DB_ADMIN" -d "$DB_NAME" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
[[ "$tables" = 0 ]] || { echo 'Refused: database is not empty'; exit 1; }
docker volume create "$STATE_VOLUME" >/dev/null
docker run --rm --network none --entrypoint sh -v "$STATE_VOLUME:/data" "$TOOLS_IMAGE" -c 'listing=$(ls -A /data) && test -z "$listing"' || { echo 'Refused: Telegram volume is not empty or cannot be inspected'; exit 1; }
dc exec -T db pg_restore -U "$DB_ADMIN" -d "$DB_NAME" --no-owner --no-acl --exit-on-error --single-transaction < database.dump
docker run --rm --network none --entrypoint sh -i -v "$STATE_VOLUME:/data" "$TOOLS_IMAGE" -c 'tar -xzf - -C /data && chown -R 1000:1000 /data' < telegram-state.tar.gz
echo 'Database and SQLite restored. Configure runtime DB role and inspect keys/access mappings before starting application services.'
