#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
value() { awk -F= -v key="$1" '$1==key {v=substr($0,length(key)+2)} END {print v}' .env; }
admin=$(value ADMIN_PORT)
ws=$(value WS_PORT)
for port in "$admin" "$ws"; do
  if [[ ! $port =~ ^[0-9]{1,5}$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
    echo 'Configure both ports first: sudo bash scripts/configure.sh' >&2; exit 1
  fi
done
if (( 10#$admin == 10#$ws )); then
  echo 'Admin and WebSocket ports must differ. Run sudo bash scripts/configure.sh' >&2; exit 1
fi
