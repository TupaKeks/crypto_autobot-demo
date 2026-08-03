#!/bin/zsh
set -euo pipefail

LABEL="com.crypto-autobot.asymmetric-demo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

/bin/launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if [[ -f "$PLIST" ]]; then
  /bin/rm "$PLIST"
fi

echo "Crypto Autobot Demo service stopped and removed."
echo "API credentials remain in macOS Keychain until you delete them manually."
echo "Runtime files and Demo statistics remain in ~/Library/Application Support/CryptoAutobot."
