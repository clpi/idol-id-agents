#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /absolute/path/to/fleet.json" >&2
  exit 2
fi
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=$1
case "$CONFIG" in /*) ;; *) echo "configuration path must be absolute" >&2; exit 2 ;; esac
[ -f "$CONFIG" ] || { echo "configuration not found: $CONFIG" >&2; exit 2; }
[ "$(uname -s)" = Linux ] || { echo "this installer is for Linux systemd --user" >&2; exit 2; }
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
mkdir -p "$STATE/logs" "$HOME/.config/systemd/user"
chmod 700 "$STATE" "$STATE/logs"
UNIT="$HOME/.config/systemd/user/idol-fleet-observe.service"
cat > "$UNIT" <<EOF
[Unit]
Description=Idol fleet observe-plan controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$ROOT
Environment=IDOL_FLEET_NO_PAYGO=1
Environment=IDOL_FLEET_NO_MODEL_INFERENCE=1
ExecStart=$PYTHON -m fleet_control.cli --config $CONFIG serve --mode observe-plan
Restart=always
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$STATE
StandardOutput=append:$STATE/logs/observe.stdout.log
StandardError=append:$STATE/logs/observe.stderr.log

[Install]
WantedBy=default.target
EOF
chmod 600 "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now idol-fleet-observe.service
systemctl --user --no-pager --full status idol-fleet-observe.service | sed -n '1,80p'
echo "installed idol-fleet-observe.service in observe-plan mode"
echo "apply mode was not enabled; no model route can be invoked by this service"
