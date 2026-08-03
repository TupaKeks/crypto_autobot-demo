#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${BINANCE_DEMO_API_KEY:-}" ]]; then
  read -r "BINANCE_DEMO_API_KEY?Binance Demo API key: "
  export BINANCE_DEMO_API_KEY
fi

if [[ -z "${BINANCE_DEMO_API_SECRET:-}" ]]; then
  read -rs "BINANCE_DEMO_API_SECRET?Binance Demo API secret (hidden): "
  echo
  export BINANCE_DEMO_API_SECRET
fi

python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.regime-scalp.example.json \
  --check

exec caffeinate -i python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.regime-scalp.example.json \
  --enable-orders
