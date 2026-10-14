#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
[[ $# == 1 ]] || { echo 'Usage: sudo bash scripts/restore.sh /absolute/path/backup.tar.gz' >&2; exit 1; }
archive=$(realpath -- "$1")
[[ -f $archive ]] || exit 1
# Reject paths outside the two expected backup entries.
while IFS= read -r name; do
  case "$name" in .env|state|state/|state/*) ;; *) echo "Invalid archive member: $name" >&2; exit 1;; esac
  [[ $name != *../* ]] || exit 1
done < <(tar -tzf "$archive")
# Only accept regular files and directories, never links/devices.
tar -tvzf "$archive" | awk 'substr($0,1,1)!="-" && substr($0,1,1)!="d" {bad=1} END {exit bad}'
docker compose stop relay
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 0700 backups
[[ ! -d state ]] || mv state "backups/pre-restore-$stamp"
[[ ! -f .env ]] || cp .env "backups/pre-restore-$stamp.env"
tar -xzf "$archive" --no-same-owner
chown -R 10001:10001 state
chmod 0700 state
docker compose up -d --wait --wait-timeout 90
