#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
umask 077

current_value() {
  if [[ -f .env ]]; then
    awk -F= -v key="$1" '$1 == key {value=substr($0,length(key)+2)} END {print value}' .env
  fi
}
valid_address() {
  local part
  local -a parts
  [[ $1 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -r -a parts <<< "$1"
  for part in "${parts[@]}"; do
    (( 10#$part <= 255 )) || return 1
    [[ $part == 0 || $part != 0* ]] || return 1
  done
}
address=$(current_value BIND_ADDRESS)
port=$(current_value PORT)
address=${address:-127.0.0.1}
port=${port:-4113}
echo 'Настройка доступа к ESC GOLD SERVER'
echo '0.0.0.0 — доступ по IP с других устройств; 127.0.0.1 — только с VPS (для HTTPS-прокси).'
while true; do
  read -r -p "Адрес прослушивания [$address]: " answer || { echo 'Ввод прерван. Настройки не изменены.' >&2; exit 1; }
  answer=${answer:-$address}
  if valid_address "$answer"; then address=$answer; break; fi
  echo 'Введите IPv4-адрес, например 0.0.0.0 или 127.0.0.1.'
done
while true; do
  read -r -p "Порт [${port}]: " answer || { echo 'Ввод прерван. Настройки не изменены.' >&2; exit 1; }
  answer=${answer:-$port}
  if [[ $answer =~ ^[0-9]{1,5}$ ]] && (( 10#$answer >= 1 && 10#$answer <= 65535 )); then
    port=$((10#$answer)); break
  fi
  echo 'Введите целый номер порта от 1 до 65535.'
done
temp=$(mktemp .env.XXXXXX)
trap 'rm -f -- "$temp"' EXIT
if [[ -f .env ]]; then
  awk '!/^[[:space:]]*(BIND_ADDRESS|PORT)[[:space:]]*=/' .env > "$temp"
fi
printf 'BIND_ADDRESS=%s\nPORT=%s\n' "$address" "$port" >> "$temp"
mv -- "$temp" .env
echo "Настройки сохранены: $address:$port"
