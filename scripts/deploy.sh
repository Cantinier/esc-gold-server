#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
[[ -f .env ]] || cp .env.example .env
install -d -m 0700 -o 10001 -g 10001 state
docker compose build
docker compose run --rm --no-deps relay python -m relay.setup
docker compose up -d --wait --wait-timeout 90
echo 'Server is healthy. Initial publisher password: sudo cat state/publisher.token'
echo 'Change the password in the admin UI after first login.'
