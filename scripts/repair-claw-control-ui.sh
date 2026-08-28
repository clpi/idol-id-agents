#!/bin/sh
set -eu

version=2026.8.1-beta.3
tarball=https://registry.npmjs.org/openclaw/-/openclaw-2026.8.1-beta.3.tgz
tarsha=d312e5bb9a3798b5c0ea222a09691e4186f7937711ffb4184170d3394fcaa265
entry=index-CL0D4SUF.js
entrysha=60b00f3d37d674abb5f947037483a02fb1d0ca2081a1778808ef083db828b5de
required='mcp-servers-CtWfZH8M.js mcp-app-security-BhWBPx_4.js chat-page-DGyROxwr.js'

fail() {
    printf 'IDOL_CLAW_REPAIR_FAIL=%s\n' "$1" >&2
    exit 40
}

exe=$(command -v openclaw 2>/dev/null || true)
[ -n "$exe" ] || fail openclaw-not-found

root=$(python3 - "$exe" "$version" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(os.path.realpath(sys.argv[1]))
expected = sys.argv[2]
for parent in (path.parent, *path.parents):
    manifest = parent / "package.json"
    if not manifest.is_file():
        continue
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        continue
    if data.get("name") == "openclaw" and data.get("version") == expected:
        print(parent)
        raise SystemExit(0)
raise SystemExit(1)
PY
) || fail exact-package-root-not-found

ui=$root/dist/control-ui
assets=$ui/assets
[ -d "$assets" ] || fail control-ui-assets-not-found
[ -w "$root/dist" ] || fail control-ui-parent-not-writable
[ -f "$assets/$entry" ] || fail live-entry-absent-locally
actual_entry=$(sha256sum "$assets/$entry" | awk '{print $1}')
[ "$actual_entry" = "$entrysha" ] || fail live-entry-package-mismatch

missing=0
for name in $required; do
    if [ ! -f "$assets/$name" ]; then
        missing=$((missing + 1))
    fi
done
if [ "$missing" -eq 0 ]; then
    printf 'IDOL_CLAW_REPAIR_ALREADY_COMPLETE=1\n'
    exit 0
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/idol-claw-assets.XXXXXX")
new=$root/dist/.control-ui.idol-new.$$
backup=$root/dist/.control-ui.idol-backup.$(date -u +%Y%m%dT%H%M%SZ)
cleanup() {
    rm -rf "$work" "$new"
}
trap cleanup EXIT INT TERM

curl -fsSL --retry 3 --retry-all-errors "$tarball" -o "$work/openclaw.tgz"
actual_tar=$(sha256sum "$work/openclaw.tgz" | awk '{print $1}')
[ "$actual_tar" = "$tarsha" ] || fail tarball-digest-mismatch

tar -xzf "$work/openclaw.tgz" -C "$work" package/dist/control-ui
source_ui=$work/package/dist/control-ui
[ -d "$source_ui/assets" ] || fail package-control-ui-absent
for name in $required; do
    [ -f "$source_ui/assets/$name" ] || fail "package-chunk-absent-$name"
done
source_entry=$(sha256sum "$source_ui/assets/$entry" | awk '{print $1}')
[ "$source_entry" = "$entrysha" ] || fail package-entry-mismatch

cp -a "$source_ui" "$new"
python3 - "$source_ui" "$new" <<'PY'
import hashlib
import pathlib
import sys

left = pathlib.Path(sys.argv[1])
right = pathlib.Path(sys.argv[2])

def inventory(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[relative] = (path.stat().st_size, digest)
    return result

if inventory(left) != inventory(right):
    raise SystemExit("copied control UI differs from verified package")
PY

mv "$ui" "$backup"
if ! mv "$new" "$ui"; then
    mv "$backup" "$ui" || true
    fail atomic-swap-failed
fi

python3 - "$source_ui" "$ui" <<'PY'
import hashlib
import pathlib
import sys

left = pathlib.Path(sys.argv[1])
right = pathlib.Path(sys.argv[2])

def inventory(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            result[relative] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return result

if inventory(left) != inventory(right):
    raise SystemExit("installed control UI differs from verified package")
PY

for name in $required; do
    [ -f "$ui/assets/$name" ] || fail "installed-chunk-absent-$name"
    code=$(curl -sS -o /dev/null -w '%{http_code}' "https://claw.idol.id/assets/$name?idol-repair=$(date +%s)")
    [ "$code" = 200 ] || fail "origin-still-$code-$name"
done

printf 'IDOL_CLAW_REPAIR_DONE=1\n'
printf 'IDOL_CLAW_REPAIR_BACKUP=%s\n' "$(basename "$backup")"
