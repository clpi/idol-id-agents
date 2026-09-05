#!/bin/sh
set -eu

INSTALL_MODE=user
SERVICE_USER=
case "${1:-}" in
  --system-user)
    if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
      echo "usage: $0 --system-user USER /absolute/path/to/fleet.json [instance]" >&2
      exit 2
    fi
    INSTALL_MODE=system
    SERVICE_USER=$2
    CONFIG=$3
    INSTANCE=${4:-main}
    ;;
  *)
    if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
      echo "usage: $0 /absolute/path/to/fleet.json [instance]" >&2
      exit 2
    fi
    CONFIG=$1
    INSTANCE=${2:-main}
    ;;
esac

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$INSTANCE" in ''|*[!A-Za-z0-9_-]*) echo "invalid fleet instance" >&2; exit 2 ;; esac
case "$CONFIG" in /*) ;; *) echo "configuration path must be absolute" >&2; exit 2 ;; esac
[ -f "$CONFIG" ] || { echo "configuration not found: $CONFIG" >&2; exit 2; }
[ "$(uname -s)" = Linux ] || { echo "this installer is for Linux systemd" >&2; exit 2; }
if [ "$INSTALL_MODE" = system ]; then
  case "$SERVICE_USER" in
    ''|[!A-Za-z_]*|*[!A-Za-z0-9_-]*)
      echo "invalid system service user" >&2
      exit 2
      ;;
  esac
  [ "$(id -u)" -eq 0 ] || {
    echo "--system-user installation requires root" >&2
    exit 2
  }
fi

RECOVERY_POLICY=$("$ROOT/scripts/fleet-systemd-recovery.sh")
PYTHON=${PYTHON:-$(command -v python3)}
[ -n "$PYTHON" ] || { echo "python3 not found" >&2; exit 2; }
if [ "$INSTALL_MODE" = system ]; then
  SERVICE_PATH=${FLEET_SERVICE_PATH:-}
  [ -n "$SERVICE_PATH" ] || {
    echo "--system-user requires FLEET_SERVICE_PATH from the existing service" >&2
    exit 2
  }
else
  SERVICE_PATH=${FLEET_SERVICE_PATH:-$PATH}
fi

require_unit_value() {
  case "$1" in
    ''|*%*|*[!A-Za-z0-9_./:+@=-]*)
      echo "$2 contains unsupported unit characters" >&2
      exit 2
      ;;
  esac
}

require_unit_value "$ROOT" "installer path"
require_unit_value "$CONFIG" "configuration path"
require_unit_value "$PYTHON" "Python path"
require_unit_value "$SERVICE_PATH" "service PATH"

SERVICE_HOME=$HOME
SERVICE_GROUP=
SERVICE_UID=$(id -u)
RUNUSER=
if [ "$INSTALL_MODE" = system ]; then
  ACCOUNT_VALUES=$(
    "$PYTHON" - "$SERVICE_USER" <<'PY'
import grp, pathlib, pwd, re, sys

try:
    account = pwd.getpwnam(sys.argv[1])
    group = grp.getgrgid(account.pw_gid)
except KeyError as exc:
    raise SystemExit("system service account does not exist") from exc
if account.pw_uid == 0:
    raise SystemExit("system service account must be non-root")
home = pathlib.Path(account.pw_dir)
if not home.is_absolute():
    raise SystemExit("system service account HOME must be absolute")
safe_name = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
safe_value = re.compile(r"[A-Za-z0-9_./:+@=-]+")
if safe_name.fullmatch(account.pw_name) is None or safe_name.fullmatch(group.gr_name) is None:
    raise SystemExit("system service account or group is not unit-safe")
if safe_value.fullmatch(str(home)) is None or "%" in str(home):
    raise SystemExit("system service account HOME is not unit-safe")
print(account.pw_name)
print(group.gr_name)
print(home)
print(account.pw_uid)
PY
  ) || exit 2
  SERVICE_USER=$(printf '%s\n' "$ACCOUNT_VALUES" | sed -n '1p')
  SERVICE_GROUP=$(printf '%s\n' "$ACCOUNT_VALUES" | sed -n '2p')
  SERVICE_HOME=$(printf '%s\n' "$ACCOUNT_VALUES" | sed -n '3p')
  SERVICE_UID=$(printf '%s\n' "$ACCOUNT_VALUES" | sed -n '4p')
  [ -z "$(printf '%s\n' "$ACCOUNT_VALUES" | sed -n '5p')" ] || {
    echo "system service account lookup returned unexpected data" >&2
    exit 2
  }
  case "$SERVICE_UID" in ''|*[!0-9]*) echo "system service account UID is invalid" >&2; exit 2 ;; esac
  [ "$SERVICE_UID" -ne 0 ] || { echo "system service account must be non-root" >&2; exit 2; }
  [ -d "$SERVICE_HOME" ] || { echo "system service account HOME is unavailable" >&2; exit 2; }
  require_unit_value "$SERVICE_USER" "system service user"
  require_unit_value "$SERVICE_GROUP" "system service group"
  require_unit_value "$SERVICE_HOME" "system service HOME"
  RUNUSER=$(command -v runuser)
  [ -n "$RUNUSER" ] || { echo "runuser is required for --system-user" >&2; exit 2; }
  require_unit_value "$RUNUSER" "runuser path"
fi

run_as_service_user() {
  if [ "$INSTALL_MODE" = system ]; then
    "$RUNUSER" -u "$SERVICE_USER" -- env -i \
      HOME="$SERVICE_HOME" \
      USER="$SERVICE_USER" \
      LOGNAME="$SERVICE_USER" \
      PATH="$SERVICE_PATH" \
      PYTHONPATH="$ROOT" \
      IDOL_FLEET_NO_PAYGO=1 \
      "$@"
  else
    "$@"
  fi
}

run_user_manager_as_service_user() {
  "$RUNUSER" -u "$SERVICE_USER" -- env -i \
    HOME="$SERVICE_HOME" \
    USER="$SERVICE_USER" \
    LOGNAME="$SERVICE_USER" \
    PATH="$SERVICE_PATH" \
    XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$SERVICE_UID/bus" \
    systemctl --user "$@"
}

run_as_service_user "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required" >&2
  exit 2
}

VALUES=$(
  run_as_service_user "$PYTHON" - "$CONFIG" "$INSTALL_MODE" <<'PY'
import json, pathlib, re, sys

raw = json.loads(pathlib.Path(sys.argv[1]).read_text())
if raw.get("mode") != "apply":
    raise SystemExit("configuration mode must be apply")
if raw.get("auto_calibrate") is not True:
    raise SystemExit("auto_calibrate must be true for continuous apply")
inventory = raw.get("inventory")
if sys.argv[2] == "user" and isinstance(inventory, dict) and inventory.get("enabled") is True:
    raise SystemExit("enabled live process inventory requires --system-user USER")
if sys.argv[2] == "system":
    for index, route in enumerate(raw.get("routes", ())):
        if not isinstance(route, dict):
            continue
        for key in ("auth_env", "usage_auth_env"):
            if route.get(key):
                raise SystemExit(
                    f"system service route {index} {key} requires unsupported credential environment"
                )
    if isinstance(inventory, dict) and inventory.get("auth_env"):
        raise SystemExit(
            "system service inventory auth_env requires unsupported credential environment"
        )
safe_value = re.compile(r"[A-Za-z0-9_./:+@=-]+")
for key in ("state_dir", "repository"):
    path = pathlib.Path(raw[key]).expanduser()
    value = str(path)
    if not path.is_absolute():
        raise SystemExit(f"{key} must be absolute after expansion")
    if safe_value.fullmatch(value) is None or "%" in value:
        raise SystemExit(f"{key} contains unsupported unit characters")
    print(value)
PY
) || exit 2
STATE=$(printf '%s\n' "$VALUES" | sed -n '1p')
REPOSITORY=$(printf '%s\n' "$VALUES" | sed -n '2p')
[ -z "$(printf '%s\n' "$VALUES" | sed -n '3p')" ] || {
  echo "configuration validation returned unexpected data" >&2
  exit 2
}
require_unit_value "$STATE" "state directory"
require_unit_value "$REPOSITORY" "repository path"
[ -d "$REPOSITORY/.git" ] || [ -f "$REPOSITORY/.git" ] || {
  echo "repository is not a Git worktree: $REPOSITORY" >&2
  exit 2
}

SERVICE="idol-fleet-$INSTANCE.service"
if [ "$INSTALL_MODE" = system ]; then
  OTHER_STATE=$(
    run_user_manager_as_service_user show "$SERVICE" --property=ActiveState --value 2>/dev/null
  ) || {
    echo "cannot verify the target user's competing service manager" >&2
    exit 2
  }
else
  OTHER_STATE=$(systemctl show "$SERVICE" --property=ActiveState --value 2>/dev/null) || {
    echo "cannot verify the competing system service manager" >&2
    exit 2
  }
fi
case "$OTHER_STATE" in
  inactive|failed) ;;
  *)
    echo "refusing competing $SERVICE in the other service manager: $OTHER_STATE" >&2
    exit 2
    ;;
esac

cd "$ROOT"
run_as_service_user "$PYTHON" -m compileall -q fleet_control tests
run_as_service_user "$PYTHON" -m unittest discover -s tests -v
run_as_service_user "$PYTHON" -m fleet_control.cli --config "$CONFIG" run-once --mode observe-plan >/dev/null
run_as_service_user "$PYTHON" -m fleet_control.cli --config "$CONFIG" calibrate >/dev/null

if [ "$INSTALL_MODE" = system ]; then
  SYSTEM_UNIT_DIR=/etc/systemd/system
  UNIT="$SYSTEM_UNIT_DIR/$SERVICE"
  RECOVERY_DIR="$SYSTEM_UNIT_DIR/$SERVICE.d"
  INSTALL_TARGET=multi-user.target
  SERVICE_IDENTITY="User=$SERVICE_USER
Group=$SERVICE_GROUP
Environment=HOME=$SERVICE_HOME"
  run_as_service_user mkdir -p "$STATE/logs"
  run_as_service_user chmod 700 "$STATE" "$STATE/logs"
  mkdir -p "$SYSTEM_UNIT_DIR" "$RECOVERY_DIR"
else
  UNIT="$HOME/.config/systemd/user/$SERVICE"
  RECOVERY_DIR="$HOME/.config/systemd/user/$SERVICE.d"
  INSTALL_TARGET=default.target
  SERVICE_IDENTITY=
  mkdir -p "$STATE/logs" "$HOME/.config/systemd/user" "$RECOVERY_DIR"
  chmod 700 "$STATE" "$STATE/logs"
fi
RECOVERY_UNIT="$RECOVERY_DIR/40-restart-backoff.conf"
cat > "$UNIT" <<EOF
[Unit]
Description=IDOL and LIVE continuous fleet controller ($INSTANCE)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
$SERVICE_IDENTITY
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
WantedBy=$INSTALL_TARGET
EOF
printf '%s\n' "$RECOVERY_POLICY" > "$RECOVERY_UNIT"
chmod 600 "$UNIT" "$RECOVERY_UNIT"
if [ "$INSTALL_MODE" = system ]; then
  run_user_manager_as_service_user disable --now idol-fleet-observe.service >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  systemctl --no-pager --full status "$SERVICE" | sed -n '1,100p'
else
  systemctl --user disable --now idol-fleet-observe.service >/dev/null 2>&1 || true
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE"
  systemctl --user restart "$SERVICE"
  systemctl --user --no-pager --full status "$SERVICE" | sed -n '1,100p'
fi
echo "installed $SERVICE in calibrated apply mode ($INSTALL_MODE manager)"
