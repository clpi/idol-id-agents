#!/usr/bin/env python3
"""Untagged Slack intake for the non-Devin fleet apps.

Polls one channel, selects top-level human messages that no fleet app has
answered, hands each one to the next ``fanout`` configured Slack apps whose bot
token is present and whose fleet route passes the zero-pay-go policy, runs each
route without a shell, and replies in thread with the same app's bot token.
Observe-plan is the default mode; ``--status`` reports every app's readiness.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataclasses import replace  # noqa: E402

from fleet_control.calibration import CalibrationError, apply_calibration, load_calibration  # noqa: E402
from fleet_control.model import Route, load_routes  # noqa: E402
from fleet_control.policy import route_verdict  # noqa: E402
from fleet_control.processes import kill_group_and_reap  # noqa: E402

SCHEMA = "idol.fleet.slack-intake.v1"
MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
REPLY_KEYS = ("reply", "text", "output", "result")
MAX_REPLY_CHARS = 3_500
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER", "LOGNAME")

Transport = Callable[[str, str, Mapping[str, str]], Mapping[str, Any]]


class IntakeRefusal(RuntimeError):
    pass


def slack_call(method: str, token: str, params: Mapping[str, str]) -> Mapping[str, Any]:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, Mapping) else "malformed"
        raise IntakeRefusal(f"slack {method} failed: {error}")
    return payload


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != SCHEMA:
        raise IntakeRefusal(f"config schema must be {SCHEMA}")
    if raw.get("mode", "observe-plan") not in {"observe-plan", "apply"}:
        raise IntakeRefusal("mode must be observe-plan or apply")
    if not re.fullmatch(r"C[A-Z0-9]+", str(raw.get("channel", ""))):
        raise IntakeRefusal("channel must be a Slack channel id")
    trusted = raw.get("trusted_users")
    if not isinstance(trusted, list) or not trusted or not all(
            isinstance(user, str) and re.fullmatch(r"[UW][A-Z0-9]+", user) for user in trusted):
        raise IntakeRefusal("trusted_users must be a non-empty list of Slack user ids")
    agents = raw.get("agents")
    if not isinstance(agents, list) or not agents:
        raise IntakeRefusal("agents must be a non-empty list")
    for agent in agents:
        for key in ("name", "user_id", "token_env", "route"):
            if not isinstance(agent.get(key), str) or not agent[key]:
                raise IntakeRefusal(f"agent field {key} is required")
        if not re.fullmatch(r"[UW][A-Z0-9]+", agent["user_id"]):
            raise IntakeRefusal(f"agent {agent['name']} user_id must be a Slack user id")
    users = [agent["user_id"] for agent in agents]
    if len(set(users)) != len(users):
        raise IntakeRefusal("agent user_id values must be unique")
    fanout = raw.get("fanout", 1)
    if not isinstance(fanout, int) or isinstance(fanout, bool) or fanout < 1:
        raise IntakeRefusal("fanout must be a positive integer")
    return raw


def load_fleet_routes(path: Path, *, now: float | None = None) -> dict[str, Route]:
    raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    configured = load_routes(raw.get("routes", ()))
    try:
        record = load_calibration(Path(str(raw.get("calibration_file", ""))).expanduser())
        routes = apply_calibration(routes=configured, record=record, raw_config=raw, now=now)
    except CalibrationError:
        routes = tuple(replace(route, enabled=False) for route in configured)
    return {route.id: route for route in routes}


class Ledger:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir.expanduser()
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "slack-intake.json"
        self.lock = open(self.dir / "slack-intake.lock", "a+")
        os.chmod(self.lock.name, 0o600)

    def __enter__(self) -> "Ledger":
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntakeRefusal("another intake cycle holds the ledger lock") from exc
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"seen": {}, "cursor": 0}
        return self

    def __exit__(self, *_: Any) -> None:
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def record(self, ts: str, entry: Mapping[str, Any]) -> None:
        self.data["seen"][ts] = dict(entry)
        handle, name = tempfile.mkstemp(dir=self.dir, prefix=".slack-intake-")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream, sort_keys=True)
        os.replace(name, self.path)


def candidates(messages: Sequence[Mapping[str, Any]], trusted_users: set[str],
               ledger: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    selected = []
    for message in messages:
        ts = str(message.get("ts", ""))
        if not ts or ts in ledger["seen"]:
            continue
        if message.get("subtype") or message.get("bot_id") or message.get("app_id"):
            continue
        if message.get("thread_ts") not in (None, ts):
            continue
        if message.get("user") not in trusted_users:
            continue
        if not str(message.get("text", "")).strip():
            continue
        selected.append(message)
    return sorted(selected, key=lambda item: float(item["ts"]))


def answered(replies: Sequence[Mapping[str, Any]], agent_users: set[str], ts: str) -> bool:
    return any(item.get("ts") != ts and item.get("user") in agent_users for item in replies)


def readiness(agent: Mapping[str, Any], routes: Mapping[str, Route], now: float) -> tuple[str, ...]:
    reasons = []
    if not os.environ.get(agent["token_env"]):
        reasons.append("token-missing")
    route = routes.get(agent["route"])
    if route is None:
        reasons.append("route-missing")
    else:
        reasons.extend(route_verdict(route, now=now).reasons)
    return tuple(reasons)


def choose(message: Mapping[str, Any], agents: Sequence[Mapping[str, Any]],
           routes: Mapping[str, Route], cursor: int, now: float,
           fanout: int = 1) -> tuple[list[Mapping[str, Any]], str]:
    mentioned = set(MENTION_RE.findall(str(message.get("text", ""))))
    pool = list(agents)
    if mentioned:
        pool = [agent for agent in agents if agent["user_id"] in mentioned]
        if not pool:
            return [], "addressed-elsewhere"
        fanout = len(pool)
    selected: list[Mapping[str, Any]] = []
    refusals = []
    for offset in range(len(pool)):
        agent = pool[(cursor + offset) % len(pool)]
        reasons = readiness(agent, routes, now)
        if reasons:
            refusals.append(f"{agent['name']}:{','.join(reasons)}")
            continue
        selected.append(agent)
        if len(selected) == fanout:
            break
    if selected:
        return selected, "selected"
    return [], "no-admitted-route:" + ";".join(refusals)


def status(config: Mapping[str, Any], routes: Mapping[str, Route], now: float) -> list[dict[str, Any]]:
    report = []
    for agent in config["agents"]:
        reasons = readiness(agent, routes, now)
        report.append({"agent": agent["name"], "user_id": agent["user_id"], "route": agent["route"],
                       "token_env": agent["token_env"], "ready": not reasons, "reasons": list(reasons)})
    return report


def prompt_for(config: Mapping[str, Any], message: Mapping[str, Any], agent: Mapping[str, Any]) -> str:
    return "\n".join((
        str(config.get("instructions", "")).strip(),
        f"You are {agent['name']} in Slack channel {config['channel']}.",
        "Reply with the verdict, evidence links, and anything concretely outstanding.",
        "Human message:",
        str(message.get("text", "")),
    )).strip() + "\n"


def run_route(route: Route, prompt: str, workdir: Path) -> str:
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt_path = workdir / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    values = {"prompt": str(prompt_path), "prompt_text": prompt, "cwd": str(workdir),
              "model": route.model, "provider": route.provider, "order": "slack-intake",
              "task": "slack-intake", "usage": str(workdir / "usage.json")}
    try:
        command = [part.format_map(values) for part in route.command]
    except KeyError as exc:
        raise IntakeRefusal(f"route argument references unknown placeholder: {exc.args[0]}") from exc
    env = {name: os.environ[name] for name in BASE_ENV if name in os.environ}
    env.update({"IDOL_FLEET_ROUTE": route.id, "IDOL_FLEET_NO_PAYGO": "1",
                "IDOL_FLEET_BILLING_CLASS": route.billing.value})
    for name in route.auth_env:
        if name not in os.environ:
            raise IntakeRefusal(f"required route auth environment is absent: {name}")
        env[name] = os.environ[name]
    process = subprocess.Popen(command, cwd=workdir, env=env, text=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=route.timeout_seconds)
    except BaseException:
        kill_group_and_reap(process)
        raise
    if process.returncode != 0:
        raise IntakeRefusal(f"route {route.id} exited {process.returncode}")
    lines = [line for line in stdout.strip().splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise IntakeRefusal(f"route {route.id} returned no JSON envelope") from exc
    if not isinstance(payload, Mapping):
        raise IntakeRefusal(f"route {route.id} returned no JSON envelope")
    cost = payload.get("costUsd", (payload.get("usage") or {}).get("costUsd") if isinstance(payload.get("usage"), Mapping) else None)
    if isinstance(cost, (int, float)) and cost > 0 and route.billing.value == "local":
        raise IntakeRefusal(f"local route {route.id} reported positive cost")
    for key in REPLY_KEYS:
        if isinstance(payload.get(key), str) and payload[key].strip():
            return payload[key].strip()[:MAX_REPLY_CHARS]
    raise IntakeRefusal(f"route {route.id} envelope has no reply text")


def cycle(config: Mapping[str, Any], routes: Mapping[str, Route], ledger: Ledger, *,
          transport: Transport = slack_call, runner: Callable[[Route, str, Path], str] = run_route,
          now: float | None = None) -> list[dict[str, Any]]:
    current = time.time() if now is None else now
    apply = config.get("mode") == "apply"
    agents = config["agents"]
    agent_users = {agent["user_id"] for agent in agents}
    trusted_users = set(config.get("trusted_users", ())) - agent_users - set(config.get("ignore_users", ()))
    history_token = os.environ.get(config.get("history_token_env", agents[0]["token_env"]), "")
    if not history_token:
        raise IntakeRefusal("history token environment is absent")
    oldest = current - int(config.get("max_age_seconds", 3600))
    history = transport("conversations.history", history_token,
                        {"channel": config["channel"], "oldest": f"{oldest:.6f}", "limit": "100"})
    decisions = []
    for message in candidates(history.get("messages", ()), trusted_users, ledger.data):
        ts = str(message["ts"])
        if message.get("reply_count"):
            replies = transport("conversations.replies", history_token,
                                {"channel": config["channel"], "ts": ts, "limit": "100"})
            if answered(replies.get("messages", ()), agent_users, ts):
                if apply:
                    ledger.record(ts, {"status": "already-answered", "at": current})
                decisions.append({"ts": ts, "status": "already-answered"})
                continue
        cursor = int(ledger.data.get("cursor", 0))
        selected, reason = choose(message, agents, routes, cursor, current, int(config.get("fanout", 1)))
        if not selected:
            decisions.append({"ts": ts, "agent": None, "status": reason})
            if apply:
                ledger.record(ts, {"status": reason, "at": current})
            continue
        batch = [{"ts": ts, "agent": agent["name"], "status": reason} for agent in selected]
        decisions.extend(batch)
        if not apply:
            continue
        ledger.data["cursor"] = cursor + len(selected)
        outcomes = {agent["name"]: "claimed" for agent in selected}
        ledger.record(ts, {"status": "claimed", "agents": dict(outcomes), "at": current})
        for agent, decision in zip(selected, batch):
            try:
                reply = runner(routes[agent["route"]], prompt_for(config, message, agent),
                               ledger.dir / "work" / ts.replace(".", "-") / agent["user_id"])
                transport("chat.postMessage", os.environ[agent["token_env"]],
                          {"channel": config["channel"], "thread_ts": ts, "text": reply})
            except (IntakeRefusal, OSError, KeyError, subprocess.SubprocessError) as exc:
                decision["status"] = f"refused:{exc}"
            else:
                decision["status"] = "replied"
            outcomes[agent["name"]] = decision["status"]
            ledger.record(ts, {"status": "attempted", "agents": dict(outcomes), "at": current})
    return decisions


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--status", action="store_true", help="print each app's readiness and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        routes = load_fleet_routes(Path(config["fleet_config"]))
    except (IntakeRefusal, OSError, ValueError) as exc:
        print(json.dumps({"status": f"config-refused:{exc}"}), flush=True)
        return 2
    if args.status:
        report = status(config, routes, time.time())
        for line in report:
            print(json.dumps(line, sort_keys=True), flush=True)
        return 0 if any(line["ready"] for line in report) else 1
    while True:
        try:
            with Ledger(Path(config.get("state_dir", "~/.local/state/idol-slack-intake"))) as ledger:
                for decision in cycle(config, routes, ledger):
                    print(json.dumps(decision, sort_keys=True), flush=True)
        except IntakeRefusal as exc:
            print(json.dumps({"status": f"cycle-refused:{exc}"}), flush=True)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(int(config.get("interval_seconds", 30)))


if __name__ == "__main__":
    raise SystemExit(main())
