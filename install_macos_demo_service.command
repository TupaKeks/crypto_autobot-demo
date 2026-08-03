#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="$ROOT/crypto_autobot"
RUNTIME_ROOT="$HOME/Library/Application Support/CryptoAutobot"
RUNTIME_BOT="$RUNTIME_ROOT/crypto_autobot"
VENV="$RUNTIME_ROOT/.venv"
LABEL="com.crypto-autobot.asymmetric-demo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$RUNTIME_BOT/data/launchd"

mkdir -p "$RUNTIME_BOT" "$LOG_DIR"
/usr/bin/rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  "$BOT_DIR/" "$RUNTIME_BOT/"

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q -r "$RUNTIME_BOT/requirements.txt"

read -r "DEMO_KEY?Binance Demo API key: "
read -rs "DEMO_SECRET?Binance Demo API secret (hidden): "
echo

export BINANCE_DEMO_API_KEY="$DEMO_KEY"
export BINANCE_DEMO_API_SECRET="$DEMO_SECRET"
cd "$RUNTIME_ROOT"
"$VENV/bin/python" "$RUNTIME_BOT/bot.py" \
  --config "$RUNTIME_BOT/config.demo.asymmetric-15m.example.json" \
  --check

/usr/bin/security add-generic-password -U -a "$USER" \
  -s "crypto-autobot-binance-demo-key" -w "$DEMO_KEY" >/dev/null
/usr/bin/security add-generic-password -U -a "$USER" \
  -s "crypto-autobot-binance-demo-secret" -w "$DEMO_SECRET" >/dev/null
unset DEMO_KEY DEMO_SECRET BINANCE_DEMO_API_KEY BINANCE_DEMO_API_SECRET

mkdir -p "$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST" RUNNER_PATH="$RUNTIME_BOT/run_asymmetric_demo_from_keychain.sh" \
LOG_PATH="$LOG_DIR" LABEL_VALUE="$LABEL" /usr/bin/python3 - <<'PY'
import os
import plistlib
from pathlib import Path

payload = {
    "Label": os.environ["LABEL_VALUE"],
    "ProgramArguments": ["/bin/zsh", os.environ["RUNNER_PATH"]],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "ProcessType": "Background",
    "StandardOutPath": str(Path(os.environ["LOG_PATH"]) / "demo.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOG_PATH"]) / "demo.err.log"),
}
path = Path(os.environ["PLIST_PATH"])
with path.open("wb") as target:
    plistlib.dump(payload, target)
PY

chmod 600 "$PLIST"
pkill -f 'crypto_autobot/bot.py.*config\.(paper|demo)\.' 2>/dev/null || true
/bin/launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID" "$PLIST"
/bin/launchctl kickstart -k "gui/$UID/$LABEL"

READY=false
for _ in {1..30}; do
  if /usr/bin/curl -fsS --max-time 2 http://127.0.0.1:8090/health 2>/dev/null \
    | /usr/bin/grep -q '"mode": "demo"'; then
    READY=true
    break
  fi
  sleep 1
done

if [[ "$READY" != true ]]; then
  echo "Demo service did not become healthy. Last errors:" >&2
  /usr/bin/tail -n 20 "$LOG_DIR/demo.err.log" 2>/dev/null || true
  exit 1
fi

echo
echo "Binance Demo service started. Dashboard: http://127.0.0.1:8090"
echo "Runtime copy: $RUNTIME_ROOT"
echo "It restarts automatically while this Mac is powered on and you are logged in."
