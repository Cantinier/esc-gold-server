#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
docker compose version >/dev/null
umask 077
