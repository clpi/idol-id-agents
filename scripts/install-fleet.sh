#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PREFIX="${IDOL_FLEET_PREFIX:-$HOME/.local/share/idol-fleet}"
CONFIG="${IDOL_FLEET_CONFIG:-$HOME/.config/idol-fleet}"
STATE="${IDOL_FLEET_STATE:-$HOME/.local/state/idol-fleet}"
IDOL_REPO="${IDOL_REPOSITORY_PATH:-$HOME/x/idol}"
PLIST="$HOME/Library/LaunchAgents/com.idol.fleet.plist"

mkdir -p "$PREFIX" "$CONFIG" "$STATE" "$STATE/logs" "$(dirname "$PLIST")"
chmod 700 "$PREFIX" "$CONFIG" "$STATE" "$STATE/logs"

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/python" -m pip install --no-deps --no-build-isolation "$ROOT"

if [[ ! -e "$CONFIG/policy.json" ]]; then
  cp "$ROOT/config/fleet-policy.example.json" "$CONFIG/policy.json"
fi
if [[ ! -e "$CONFIG/tasks.json" ]]; then
  cp "$ROOT/config/work-orders.example.json" "$CONFIG/tasks.json"
fi
chmod 600 "$CONFIG/policy.json" "$CONFIG/tasks.json"

python3 - "$ROOT/launchd/com.idol.fleet.plist" "$PLIST" <<'PY'
from pathlib import Path
import os, sys
src, dst = map(Path, sys.argv[1:])
config = Path(os.environ.get("IDOL_FLEET_CONFIG", str(Path.home()/".config/idol-fleet")))
state = Path(os.environ.get("IDOL_FLEET_STATE", str(Path.home()/".local/state/idol-fleet")))
prefix = Path(os.environ.get("IDOL_FLEET_PREFIX", str(Path.home()/".local/share/idol-fleet")))
idol = Path(os.environ.get("IDOL_REPOSITORY_PATH", str(Path.home()/"x/idol")))
text = src.read_text()
replacements = {
    "@@PYTHON@@": str(prefix/"venv/bin/python"),
    "@@POLICY@@": str(config/"policy.json"),
    "@@TASKS@@": str(config/"tasks.json"),
    "@@STATE@@": str(state),
    "@@IDOL_REPOSITORY@@": f"clpi/idol={idol}",
    "@@LOG@@": str(state/"logs/fleet.log"),
    "@@ERROR_LOG@@": str(state/"logs/fleet.err"),
}
for key, value in replacements.items():
    text = text.replace(key, value)
dst.write_text(text)
os.chmod(dst, 0o600)
PY

# Installation is deliberately observe-plan only. It never creates apply-enabled.
launchctl bootout "gui/$(id -u)/com.idol.fleet" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.idol.fleet"
launchctl kickstart -k "gui/$(id -u)/com.idol.fleet"

echo "idol-fleet installed in observe-plan mode"
