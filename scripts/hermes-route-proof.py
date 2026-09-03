#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Prove one fixed-cost Hermes route without inference")
    root.add_argument("--provider", required=True)
    root.add_argument("--model", required=True)
    root.add_argument(
        "--contract",
        required=True,
        choices=("codex-oauth", "xai-oauth", "zai-max", "kilo-free"),
    )
    return root


def dotenv(name: str) -> str:
    path = Path.home() / ".hermes/.env"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"Hermes environment has no {name}")


def auth_state() -> dict:
    path = Path.home() / ".hermes/auth.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("Hermes auth store is invalid")
    return raw


def no_fallbacks() -> None:
    path = Path.home() / ".hermes/config.yaml"
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("fallback_providers:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value not in {"", "[]", "null", "~"}:
            raise RuntimeError("Hermes fallback_providers is not empty")
        if not value:
            for child in lines[index + 1 :]:
                if child and not child[0].isspace():
                    break
                if child.strip().startswith("-"):
                    raise RuntimeError("Hermes fallback_providers is not empty")
        return


def oauth_logged_in(provider: str) -> None:
    hermes = shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
    result = subprocess.run(
        [hermes, "auth", "status", provider],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or "logged in" not in result.stdout.lower():
        raise RuntimeError(f"Hermes {provider} OAuth is not logged in")


def json_get(url: str, bearer: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + bearer, "User-Agent": "idol-fleet-proof/1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = json.load(response)
    if not isinstance(raw, dict):
        raise RuntimeError("provider proof endpoint returned a non-object")
    return raw


def prove(args: argparse.Namespace) -> None:
    no_fallbacks()
    if args.contract == "codex-oauth":
        if args.provider != "openai-codex" or not args.model.startswith("gpt-"):
            raise RuntimeError("Codex proof is bound to openai-codex and a gpt model")
        oauth_logged_in(args.provider)
    elif args.contract == "xai-oauth":
        if args.provider != "xai-oauth" or not args.model.startswith("grok-"):
            raise RuntimeError("xAI proof is bound to xai-oauth and a grok model")
        oauth_logged_in(args.provider)
        state = auth_state().get("providers", {}).get("xai-oauth", {})
        token = (state.get("tokens") or {}).get("access_token") if isinstance(state, dict) else None
        if not token:
            raise RuntimeError("xAI OAuth access token is absent")
        billing = json_get("https://cli-chat-proxy.grok.com/v1/billing?format=credits", token)
        config = billing.get("config") if isinstance(billing.get("config"), dict) else {}
        cap = config.get("onDemandCap") if isinstance(config.get("onDemandCap"), dict) else {}
        if cap.get("val") != 0:
            raise RuntimeError("xAI on-demand cap is not zero")
    elif args.contract == "zai-max":
        if args.provider != "zai" or args.model != "glm-5":
            raise RuntimeError("Z.AI proof is bound to zai/glm-5")
        usage = json_get(
            "https://api.z.ai/api/monitor/usage/quota/limit",
            dotenv("GLM_API_KEY"),
        )
        data = usage.get("data") if isinstance(usage.get("data"), dict) else {}
        if usage.get("success") is not True or str(data.get("level", "")).lower() != "max":
            raise RuntimeError("Z.AI account is not a witnessed Max plan")
    else:
        if args.provider != "kilo" or args.model != "kilo-auto/free":
            raise RuntimeError("Kilo free proof is bound to kilo/kilo-auto/free")
        status = subprocess.run(
            [shutil.which("hermes") or str(Path.home() / ".local/bin/hermes"), "auth", "status", "kilocode"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        if status.returncode != 0:
            raise RuntimeError("Kilo credential is unavailable")


def main() -> int:
    args = parser().parse_args()
    try:
        prove(args)
    except Exception as exc:
        print(f"route proof refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"IDOL ROUTE READY provider={args.provider} model={args.model} "
        f"contract={args.contract} no-fallback no-paygo"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
