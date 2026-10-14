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
telegram_token=''
telegram_channel=''
telegram_init=false
if [[ ! -f state/telegram.json && ! -f state/published.sqlite3 ]]; then
  telegram_init=true
  echo 'Telegram: токен бота вводится скрыто. Enter — пока не подключать.'
  while true; do
    read -r -s -p 'Токен Telegram-бота: ' telegram_token || { echo 'Ввод прерван.' >&2; exit 1; }
    printf '\n'
    [[ -z $telegram_token || $telegram_token =~ ^[0-9]{5,20}:[A-Za-z0-9_-]{20,100}$ ]] && break
    echo 'Неверный формат токена. Повторите ввод.'
  done
  if [[ -n $telegram_token ]]; then
    while true; do
      read -r -p 'ID канала (например -1001234567890) или @имя: ' telegram_channel || { echo 'Ввод прерван.' >&2; exit 1; }
      [[ $telegram_channel =~ ^-?[0-9]{1,20}$ || $telegram_channel =~ ^@[A-Za-z][A-Za-z0-9_]{4,31}$ ]] && break
      echo 'Укажите числовой ID канала или @имя.'
    done
  fi
fi
temp=$(mktemp .env.XXXXXX)
trap 'rm -f -- "$temp"' EXIT
if [[ -f .env ]]; then
  awk '!/^[[:space:]]*(BIND_ADDRESS|PORT)[[:space:]]*=/' .env > "$temp"
fi
printf 'BIND_ADDRESS=%s\nPORT=%s\n' "$address" "$port" >> "$temp"
mv -- "$temp" .env
if [[ $telegram_init == true ]]; then
  mkdir -p state
  printf '{"token":"%s","channel":"%s"}\n' "$telegram_token" "$telegram_channel" > state/telegram.json
  chmod 600 state/telegram.json
fi
echo "Настройки сохранены: $address:$port"
