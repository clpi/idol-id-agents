#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /absolute/path/to/fleet.json" >&2
  exit 2
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=$1
case "$CONFIG" in
  /*) ;;
  *) echo "configuration path must be absolute" >&2; exit 2 ;;
esac
[ -f "$CONFIG" ] || { echo "configuration not found: $CONFIG" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] || { echo "this installer is for macOS launchd" >&2; exit 2; }

PYTHON=${PYTHON:-$(command -v python3)}
[ -n "$PYTHON" ] || { echo "python3 not found" >&2; exit 2; }

cd "$ROOT"
"$PYTHON" -m compileall -q fleet_control
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m fleet_control.cli --config "$CONFIG" run-once --mode observe-plan >/dev/null

STATE=$(
  "$PYTHON" - "$CONFIG" <<'PY'
import json, pathlib, sys
raw=json.loads(pathlib.Path(sys.argv[1]).read_text())
path=pathlib.Path(raw['state_dir']).expanduser()
if not path.is_absolute(): raise SystemExit('state_dir must be absolute after expansion')
print(path)
PY
)
mkdir -p "$STATE/logs" "$HOME/Library/LaunchAgents"
chmod 700 "$STATE" "$STATE/logs"

LABEL=com.idol.fleet.observe
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>fleet_control.cli</string>
    <string>--config</string>
    <string>$CONFIG</string>
    <string>serve</string>
    <string>--mode</string>
    <string>observe-plan</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$ROOT</string>
    <key>IDOL_FLEET_NO_PAYGO</key><string>1</string>
    <key>IDOL_FLEET_NO_MODEL_INFERENCE</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$STATE/logs/observe.stdout.log</string>
  <key>StandardErrorPath</key><string>$STATE/logs/observe.stderr.log</string>
</dict>
</plist>
EOF
chmod 600 "$PLIST"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
launchctl print "$DOMAIN/$LABEL" | sed -n '1,80p'

echo "installed $LABEL in observe-plan mode"
echo "apply mode was not enabled; no model route can be invoked by this service"
