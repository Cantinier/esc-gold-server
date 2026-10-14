#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
[[ -z $(git status --porcelain) ]] || { echo 'Commit or discard local source edits before updating.' >&2; exit 1; }
git fetch origin
old=$(git rev-parse HEAD)
bash scripts/backup.sh
git merge --ff-only origin/main
docker compose build
docker compose up -d --wait --wait-timeout 90
printf 'Updated. Previous code revision: %s\n' "$old"
