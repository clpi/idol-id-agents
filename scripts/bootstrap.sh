#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[*] Updating base packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl build-essential python3 python3-pip nodejs npm 2>&1 | tail -5

echo "[*] Installing agents..."
npm install -g --silent @anthropic-ai/claude-code @openai/codex oh-my-pi poolside agy 2>&1 | tail -3

if ! command -v hermes >/dev/null 2>&1; then
  pip install --user --quiet hermes-agent 2>&1 | tail -2
fi

echo "[*] Setting up ~/.shared-env..."
if [ ! -f "$HOME/.shared-env" ]; then
  cp "$(dirname "$0")/../config/.shared-env.template" "$HOME/.shared-env"
  echo "EDIT $HOME/.shared-env and add your keys, then re-run this script."
fi

echo "[*] Done. Run: source ~/.shared-env && which claude codex hermes grok opencode oh-my-pi poolside agy kilo"
