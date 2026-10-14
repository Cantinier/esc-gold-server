#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
[[ -f state/published.sqlite3 ]] || { echo 'No initialized database.' >&2; exit 1; }
install -d -m 0700 backups
archive="$ROOT/backups/state-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
# Stop writes while copying SQLite and its credentials together.
trap 'docker compose up -d --wait --wait-timeout 90' EXIT
docker compose stop relay
tar -czf "$archive" state .env
echo "$archive"
