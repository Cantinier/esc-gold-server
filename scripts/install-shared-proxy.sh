#!/usr/bin/env bash
source "$(dirname -- "$0")/common.sh"
[[ -f /backends/proxy/Caddyfile && -f /backends/proxy/compose.yaml ]] || {
    echo 'Требуется существующий Caddy в /backends/proxy (см. README).' >&2; exit 1;
}
docker network inspect backends-edge >/dev/null
[[ -f .env ]] || { echo 'Сначала выполните scripts/configure.sh'; exit 1; }
python3 - <<'PY'
from pathlib import Path
values=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
if any(values.get(k)!=v for k,v in {'BIND_ADDRESS':'127.0.0.1','ADMIN_PORT':'14115','WS_PORT':'14116'}.items()):
    raise SystemExit('Для текущего VPS настройте 127.0.0.1, ADMIN_PORT=14115 и WS_PORT=14116 через configure.sh.')
PY
cat >compose.override.yaml <<'EOF'
services:
  gateway:
    networks:
      default:
      edge:
        aliases: [mttrader-gateway]
networks:
  edge:
    external: true
    name: backends-edge
EOF
python3 scripts/configure-proxy.py
docker exec proxy-caddy-1 caddy validate --config /etc/caddy/Caddyfile
docker compose up -d --wait --wait-timeout 90
(cd /backends/proxy && docker compose up -d && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile)
ufw allow 4115:4117/tcp
echo 'HTTPS: https://admin.130.49.163.172.sslip.io:4115/'
echo 'WSS: wss://ws.130.49.163.172.sslip.io:4116/ws'
