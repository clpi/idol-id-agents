#!/bin/sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 /absolute/path/to/fleet.json [instance]" >&2
  exit 2
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=$1
INSTANCE=${2:-main}
case "$INSTANCE" in ''|*[!A-Za-z0-9_-]*) echo "invalid fleet instance" >&2; exit 2 ;; esac
case "$CONFIG" in /*) ;; *) echo "configuration path must be absolute" >&2; exit 2 ;; esac
[ -f "$CONFIG" ] || { echo "configuration not found: $CONFIG" >&2; exit 2; }
[ "$(uname -s)" = Linux ] || { echo "this installer is for Linux systemd --user" >&2; exit 2; }
PYTHON=${PYTHON:-$(command -v python3)}
[ -n "$PYTHON" ] || { echo "python3 not found" >&2; exit 2; }
SERVICE_PATH=${FLEET_SERVICE_PATH:-$PATH}
case "$SERVICE_PATH" in ''|*[!A-Za-z0-9_./:+-]*) echo "service PATH contains unsupported characters" >&2; exit 2 ;; esac
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required" >&2
  exit 2
}

VALUES=$(
  "$PYTHON" - "$CONFIG" <<'PY'
import json, pathlib, sys
raw=json.loads(pathlib.Path(sys.argv[1]).read_text())
if raw.get('mode') != 'apply': raise SystemExit('configuration mode must be apply')
if raw.get('auto_calibrate') is not True: raise SystemExit('auto_calibrate must be true for continuous apply')
for key in ('state_dir', 'repository'):
    path=pathlib.Path(raw[key]).expanduser()
    if not path.is_absolute(): raise SystemExit(f'{key} must be absolute after expansion')
    print(path)
PY
)
STATE=$(printf '%s\n' "$VALUES" | sed -n '1p')
REPOSITORY=$(printf '%s\n' "$VALUES" | sed -n '2p')
[ -d "$REPOSITORY/.git" ] || [ -f "$REPOSITORY/.git" ] || {
  echo "repository is not a Git worktree: $REPOSITORY" >&2
  exit 2
}

cd "$ROOT"
"$PYTHON" -m compileall -q fleet_control tests
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m fleet_control.cli --config "$CONFIG" run-once --mode observe-plan >/dev/null
"$PYTHON" -m fleet_control.cli --config "$CONFIG" calibrate >/dev/null

mkdir -p "$STATE/logs" "$HOME/.config/systemd/user"
chmod 700 "$STATE" "$STATE/logs"
SERVICE="idol-fleet-$INSTANCE.service"
UNIT="$HOME/.config/systemd/user/$SERVICE"
cat > "$UNIT" <<EOF
[Unit]
Description=IDOL and LIVE continuous fleet controller ($INSTANCE)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$ROOT
Environment=IDOL_FLEET_NO_PAYGO=1
Environment=PATH=$SERVICE_PATH
ExecStart=$PYTHON -m fleet_control.cli --config $CONFIG serve --mode apply
Restart=always
RestartSec=15
KillMode=control-group
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=$STATE $REPOSITORY
StandardOutput=append:$STATE/logs/controller.stdout.log
StandardError=append:$STATE/logs/controller.stderr.log

[Install]
WantedBy=default.target
EOF
chmod 600 "$UNIT"
systemctl --user disable --now idol-fleet-observe.service >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable "$SERVICE"
systemctl --user restart "$SERVICE"
systemctl --user --no-pager --full status "$SERVICE" | sed -n '1,100p'
echo "installed $SERVICE in calibrated apply mode"
