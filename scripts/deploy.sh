#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
case "${1:-}" in
  '') bash scripts/configure.sh ;;
  --non-interactive) [[ -f .env ]] || { echo 'Configure .env first with bash scripts/configure.sh.' >&2; exit 1; } ;;
  *) echo 'Usage: sudo bash scripts/deploy.sh [--non-interactive]' >&2; exit 1 ;;
esac
install -d -m 0700 -o 10001 -g 10001 state
if [[ -f state/telegram.json ]]; then chown 10001:10001 state/telegram.json; fi
docker compose build
docker compose run --rm --no-deps relay python -m relay.setup
docker compose up -d --wait --wait-timeout 90
echo 'Server is healthy. Initial publisher password: sudo cat state/publisher.token'
echo 'Change the password in the admin UI after first login.'
