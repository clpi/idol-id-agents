#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
  echo "usage: $0" >&2
  exit 2
fi

SYSTEMCTL=$(command -v systemctl || true)
[ -n "$SYSTEMCTL" ] || {
  echo "systemctl not found" >&2
  exit 2
}

VERSION_OUTPUT=$(
  "$SYSTEMCTL" --version 2>/dev/null
) || {
  echo "unable to determine systemd version" >&2
  exit 2
}
VERSION=$(
  printf '%s\n' "$VERSION_OUTPUT" |
    awk 'NR == 1 && $1 == "systemd" && $2 ~ /^[0-9]+$/ { print $2 }'
)
case "$VERSION" in
  ''|*[!0-9]*)
    echo "unable to parse systemd version" >&2
    exit 2
    ;;
esac
[ "${#VERSION}" -le 9 ] || {
  echo "systemd version is outside the supported range" >&2
  exit 2
}

if [ "$VERSION" -ge 254 ]; then
  cat <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=30s
RestartSteps=6
RestartMaxDelaySec=15min
EOF
else
  cat <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=5min
EOF
fi
