#!/bin/zsh
set -euo pipefail

RUNTIME_ROOT="${CRYPTO_AUTOBOT_HOME:-$HOME/Library/Application Support/CryptoAutobot}"
BOT_DIR="$RUNTIME_ROOT/crypto_autobot"
PYTHON="$RUNTIME_ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python environment is missing. Run install_macos_demo_service.command again." >&2
  exit 1
fi

export BINANCE_DEMO_API_KEY="$(/usr/bin/security find-generic-password \
  -a "$USER" -s "crypto-autobot-binance-demo-key" -w)"
export BINANCE_DEMO_API_SECRET="$(/usr/bin/security find-generic-password \
  -a "$USER" -s "crypto-autobot-binance-demo-secret" -w)"
export PORT="${CRYPTO_AUTOBOT_PORT:-8091}"

cd "$RUNTIME_ROOT"
exec /usr/bin/caffeinate -i "$PYTHON" "$BOT_DIR/bot.py" \
  --config "$BOT_DIR/config.demo.asymmetric-15m.example.json" \
  --enable-orders
