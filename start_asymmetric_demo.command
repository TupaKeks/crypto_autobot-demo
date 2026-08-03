#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="crypto_autobot/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q -r crypto_autobot/requirements.txt

if [[ -z "${BINANCE_DEMO_API_KEY:-}" ]]; then
  read -r "BINANCE_DEMO_API_KEY?Binance Demo API key: "
  export BINANCE_DEMO_API_KEY
fi

if [[ -z "${BINANCE_DEMO_API_SECRET:-}" ]]; then
  read -rs "BINANCE_DEMO_API_SECRET?Binance Demo API secret (hidden): "
  echo
  export BINANCE_DEMO_API_SECRET
fi

# Only replace local profiles that use this dashboard port.
pkill -f 'crypto_autobot/bot.py.*config.paper.asymmetric-15m.example.json' 2>/dev/null || true
pkill -f 'crypto_autobot/bot.py.*config.paper.ensemble-15m.example.json' 2>/dev/null || true
sleep 1

"$VENV/bin/python" crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.asymmetric-15m.example.json \
  --check

exec caffeinate -i "$VENV/bin/python" crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.asymmetric-15m.example.json \
  --enable-orders
