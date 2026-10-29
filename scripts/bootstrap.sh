#!/usr/bin/env bash
set -euo pipefail

# Keep the pipe-install template immutable; checkout installs use their own template.
template_revision=4cb500f1ac17eb358723bf495c3748ea03d4593c
template_sha256=e043e3bc81171ff908d7670cd5e278f1481af5627986d258aca153410bfb6d24
temporary_template=

cleanup() {
  if [ -n "$temporary_template" ]; then
    rm -f -- "$temporary_template"
  fi
}
trap cleanup EXIT

fail() {
  printf 'bootstrap: %s\n' "$*" >&2
  exit 1
}

privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "Missing system dependencies require root or sudo."
  fi
}

install_dependencies() {
  local platform=$1
  local packages=()
  local tool
  for tool in git curl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      if [ "$platform" = Darwin ] && [ "$tool" = python3 ]; then
        packages+=(python)
      else
        packages+=("$tool")
      fi
    fi
  done
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    if [ "$platform" = Darwin ]; then
      packages+=(node)
    else
      packages+=(nodejs npm)
    fi
  fi
  if [ "${#packages[@]}" -gt 0 ]; then
    printf '[*] Installing missing dependencies for %s...\n' "$platform"
    case "$platform" in
      Darwin)
        command -v brew >/dev/null 2>&1 || fail "Install Homebrew (https://brew.sh) or provide git, curl, python3, node and npm on PATH, then rerun."
        brew install "${packages[@]}" < /dev/null
        ;;
      Linux)
        command -v apt-get >/dev/null 2>&1 || fail "Automatic Linux dependency installation requires apt-get. Provide git, curl, python3, node and npm on PATH, then rerun."
        privileged env DEBIAN_FRONTEND=noninteractive apt-get update
        privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates "${packages[@]}" < /dev/null
        ;;
    esac
  fi
  for tool in git curl python3 node npm; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is still missing after dependency installation."
  done
  local node_major
  node_major=$(node -p "process.versions.node.split('.')[0]")
  case "$node_major" in
    ''|*[!0-9]*) fail "Could not determine the installed Node.js version." ;;
  esac
  [ "$node_major" -ge 18 ] || fail "Node.js 18 or newer is required; install a supported Node.js release and rerun."
}

prepare_environment() {
  local destination="$HOME/.shared-env"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    [ ! -d "$destination" ] || fail "$destination is a directory; choose a secrets file before rerunning."
    printf '[*] Preserving existing %s\n' "$destination"
    return
  fi

  local template= expected_sha= script_directory=
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_directory=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    if [ -f "$script_directory/../config/.shared-env.template" ]; then
      template="$script_directory/../config/.shared-env.template"
    fi
  fi
  temporary_template=$(mktemp "$HOME/.shared-env.XXXXXX")
  if [ -n "$template" ]; then
    cp -- "$template" "$temporary_template"
  else
    printf '[*] Fetching the pinned environment template...\n'
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      "https://raw.githubusercontent.com/clpi/idol-id-agents/$template_revision/config/.shared-env.template" \
      --output "$temporary_template" < /dev/null
    expected_sha=$template_sha256
  fi
  # Publish without replacing an existing file, symlink, or concurrent writer.
  python3 - "$temporary_template" "$destination" "$expected_sha" <<'PY'
import hashlib
import os
from pathlib import Path
import sys

source, destination, expected = sys.argv[1:]
if expected and hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected:
    raise SystemExit("bootstrap: downloaded environment template checksum mismatch")
os.chmod(source, 0o600)
try:
    os.link(source, destination)
except FileExistsError:
    raise SystemExit("bootstrap: environment path appeared during setup; left it unchanged, rerun")
print(f"[*] Created {destination} with mode 600; configure credentials or your secrets manager separately.")
PY
  rm -f -- "$temporary_template"
  temporary_template=
}

install_agent() {
  local executable=$1 package=$2 prefix=$3
  if command -v "$executable" >/dev/null 2>&1; then
    printf '[*] Preserving existing %s installation.\n' "$executable"
    return
  fi
  printf '[*] Installing %s...\n' "$executable"
  npm install --global --prefix "$prefix" --no-audit --no-fund --engine-strict "$package" < /dev/null
  hash -r
  command -v "$executable" >/dev/null 2>&1 || fail "$package did not provide $executable on PATH."
}

main() {
  if [ "${1:-}" = --help ]; then
    printf 'Usage: bash bootstrap.sh\nPrepares dependencies, Claude Code, Codex, and a private empty credential template.\nPreserves existing agents; does not authenticate or start them.\n'
    return
  fi
  [ "$#" -eq 0 ] || fail "Unexpected arguments; use --help."
  local platform
  platform=$(uname -s)
  case "$platform" in
    Darwin|Linux) ;;
    *) fail "Unsupported operating system: $platform. Use macOS or Linux." ;;
  esac
  [ -n "${HOME:-}" ] && [ -d "$HOME" ] || fail "HOME must name an existing directory."
  local prefix="$HOME/.local"
  export PATH="$prefix/bin:$PATH"
  umask 077
  install_dependencies "$platform"
  prepare_environment
  install_agent claude @anthropic-ai/claude-code "$prefix"
  install_agent codex @openai/codex "$prefix"

  printf '[*] Executables available in this shell (authentication and provider billing are not tested):\n'
  local executable location
  for executable in claude codex hermes grok opencode omp oh-my-pi poolside agy kilo; do
    if location=$(command -v "$executable" 2>/dev/null); then
      printf '  %s: %s\n' "$executable" "$location"
    else
      printf '  %s: not installed/on PATH\n' "$executable"
    fi
  done
  if ! command -v hermes >/dev/null 2>&1; then
    printf '[*] Hermes is missing; use its official setup: https://hermes-agent.nousresearch.com/docs/getting-started/installation\n'
  fi
  printf '[*] For subsequent shells, add %s/bin to PATH.\n' "$prefix"
  printf '[*] Bootstrap complete. Provider activation and fleet admission remain separate steps.\n'
}

main "$@"
