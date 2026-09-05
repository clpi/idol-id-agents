#!/usr/bin/env python3
"""Read-only r16 service, route, tailnet, heartbeat, and HTTPS observer."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

STATE = Path(os.getenv("IDOL_HEALTH_STATE_DIR", Path.home() / ".local/state/idol-health-probe"))
HEARTBEAT = Path(os.getenv("IDOL_HEALTH_HEARTBEAT", Path.home() / ".config/idol-hermes/primary-heartbeat"))
ROLE = Path(os.getenv("IDOL_HEALTH_ROLE", Path.home() / ".config/idol-hermes/role"))
MAX_AGE = int(os.getenv("IDOL_HEALTH_HEARTBEAT_MAX_AGE", "55"))
MM_NAMES = {x.strip().lower() for x in os.getenv("IDOL_HEALTH_MM_NAMES", "mm,mac-mini,macmini").split(",") if x.strip()}
CONFIGS = {
    "idol": os.getenv("IDOL_HEALTH_IDOL_CONFIG"),
    "live": os.getenv("IDOL_HEALTH_LIVE_CONFIG"),
}
JOURNALS = {
    "idol": Path(os.getenv("IDOL_HEALTH_IDOL_JOURNAL", Path.home() / ".local/state/idol-fleet-idol/fleet-history.jsonl")),
    "live": Path(os.getenv("IDOL_HEALTH_LIVE_JOURNAL", Path.home() / ".local/state/idol-fleet-live/fleet-history.jsonl")),
}
SERVICES = (
    ("tailscaled", "tailscaled.service", False, "active"),
    ("obsolete_cloudflared", "cloudflared.service", False, "obsolete"),
    ("idol_fleet_idol", "idol-fleet-idol.service", True, "enabled"),
    ("idol_fleet_live", "idol-fleet-live.service", True, "enabled"),
    ("r16_tunnel", "r16-tunnel.service", True, "enabled"),
    ("r16_legacy_secure", "r16-legacy-secure.service", True, "enabled"),
)
URLS = (
    ("idol", "root", "https://idol.id/", 200),
    ("live", "root", "https://live.idol.id/", 200),
    ("hermes", "health", "https://hermes.idol.id/health", 200),
    ("hermes", "root", "https://hermes.idol.id/", 302),
    ("claw", "root", "https://claw.idol.id/", 200),
    ("hermes_mm", "health", "https://hermes-mm.idol.id/health", 200),
    ("hermes_mm", "root", "https://hermes-mm.idol.id/", 302),
    ("claw_mm", "root", "https://claw-mm.idol.id/", 200),
)


def run(command, timeout=8, runner=subprocess.run):
    try:
        result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout, False
    except subprocess.TimeoutExpired:
        return None, "", True
    except OSError:
        return None, "", False


def service_batch(items, user, runner=subprocess.run):
    names = [item[1] for item in items]
    command = ["systemctl"] + (["--user"] if user else []) + [
        "show", "--no-pager", "--property=Id,LoadState,UnitFileState,ActiveState,ExecStart", *names]
    _, output, timed_out = run(command, 8, runner)
    records = {}
    for block in output.split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id"):
            records[fields["Id"]] = fields
    result = {}
    for key, name, _, expectation in items:
        fields = records.get(name, {})
        enabled, active, loaded = (fields.get("UnitFileState", "unknown"),
                                   fields.get("ActiveState", "unknown"), fields.get("LoadState", "unknown"))
        if expectation == "obsolete":
            healthy = active == "inactive" and (enabled in {"disabled", "masked"} or loaded == "not-found")
        else:
            healthy = active == "active" and (expectation == "active" or enabled == "enabled")
        result[key] = {"healthy": healthy, "enabled": enabled, "active": active,
                       "load_state": loaded, "status": "timeout" if timed_out else "ok" if healthy else "mismatch"}
        if key.startswith("idol_fleet_"):
            modes = re.findall(r"(?:^|\s)--mode(?:=|\s+)(apply|observe-plan)(?=\s|;|$)",
                               fields.get("ExecStart", ""))
            result[key]["mode"] = modes[-1] if len(modes) == 1 else "unknown"
    return result


def services(runner=subprocess.run):
    system = [item for item in SERVICES if not item[2]]
    user = [item for item in SERVICES if item[2]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        system_future = pool.submit(service_batch, system, False, runner)
        user_future = pool.submit(service_batch, user, True, runner)
        return {**system_future.result(), **user_future.result()}


def curl(spec, runner=subprocess.run):
    endpoint, variant, url, expected = spec
    command = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
               "--connect-timeout", "5", "--max-time", "12", "--proto", "=https", url]
    code, output, timed_out = run(command, 12, runner)
    status = int(output.strip()) if output.strip().isdigit() else None
    return endpoint, {"variant": variant, "healthy": code == 0 and status == expected,
                      "http_status": status, "tls_verified": code == 0,
                      "timeout": timed_out or code == 28, "tls_failed": code == 60}


def endpoints(runner=subprocess.run):
    with ThreadPoolExecutor(max_workers=len(URLS)) as pool:
        checks = list(pool.map(lambda item: curl(item, runner), URLS))
    result = {}
    for name in ("idol", "live", "hermes", "claw", "hermes_mm", "claw_mm"):
        choices = [value for endpoint, value in checks if endpoint == name]
        passed = next((value for value in choices if value["healthy"]), None)
        sample = passed or next((value for value in choices if value["timeout"]), choices[0])
        if passed:
            status = "ok"
        elif name == "live" and sample["timeout"]:
            status = "private_live_timeout"
        elif any(value["tls_failed"] for value in choices):
            status = "tls_verification_failed"
        elif sample["timeout"]:
            status = "timeout"
        else:
            status = "unexpected_response"
        result[name] = {"healthy": bool(passed), "status": status,
                        "accepted_variant": passed["variant"] if passed else None,
                        "http_status": sample["http_status"],
                        "tls_verified": bool(passed and passed["tls_verified"])}
    return result


def tailnet(runner=subprocess.run):
    if not MM_NAMES:
        return {"healthy": False, "found": False, "online": False, "status": "invalid_config"}
    code, output, timed_out = run(["tailscale", "status", "--json"], runner=runner)
    if timed_out or code != 0 or len(output) > 1_048_576:
        return {"healthy": False, "found": False, "online": False, "status": "timeout" if timed_out else "unavailable"}
    try:
        peers = json.loads(output).get("Peer", {}).values()
    except (AttributeError, json.JSONDecodeError):
        return {"healthy": False, "found": False, "online": False, "status": "invalid_json"}
    match = None
    for peer in peers:
        names = {str(peer.get(key, "")).lower().rstrip(".") for key in ("HostName", "DNSName")}
        names |= {name.split(".", 1)[0] for name in names}
        names.discard("")
        if names & MM_NAMES:
            match = peer
            break
    found, online = match is not None, bool(match and match.get("Online") is True)
    return {"healthy": found and online, "found": found, "online": online,
            "status": "online" if online else ("offline" if found else "not_found")}


def load_json(path, limit=1_048_576):
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too_large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("not_object")
    return value


def calibration(config_path, now, effective_mode="unknown"):
    if not config_path:
        return {"healthy": False, "calibration_ready": False, "available": False,
                "status": "unbound", "mode": "unknown", "no_route": True}
    try:
        from fleet_control.calibration import apply_calibration, config_hash, load_calibration
        from fleet_control.controller import load_config
        from fleet_control.policy import route_verdict
        raw, config = load_config(Path(config_path))
    except ImportError:
        return {"healthy": False, "calibration_ready": False, "available": False,
                "status": "validator_unavailable", "mode": "unknown", "no_route": True}
    except Exception:
        return {"healthy": False, "calibration_ready": False, "available": False,
                "status": "config_unreadable", "mode": "unknown", "no_route": True}
    try:
        record = load_calibration(config.calibration_file)
        record_valid = record.valid(config_hash=config_hash(raw), now=now)
        routes = apply_calibration(routes=config.routes, record=record, raw_config=raw, now=now)
    except Exception:
        return {"healthy": False, "calibration_ready": False, "available": False,
                "status": "invalid_or_stale", "mode": effective_mode, "no_route": True}
    configured = [route for route in config.routes if route.enabled]
    ready = [route for route in routes if route.enabled and route.proof.subject_files
             and route_verdict(route, now=now).allowed]
    bound_files = sum(len(route.proof.subject_files) for route in ready)
    calibration_ready = record_valid and bool(ready)
    all_routes_healthy = calibration_ready and len(ready) == len(configured)
    status = ("observation_only" if effective_mode == "observe-plan" else
              "mode_unknown" if effective_mode != "apply" else
              "ok" if all_routes_healthy else
              "partial_route_availability" if calibration_ready else "route_proof_invalid")
    return {"healthy": status == "ok", "calibration_ready": calibration_ready,
            "all_routes_healthy": all_routes_healthy, "available": True,
            "status": status, "mode": effective_mode, "no_route": not ready,
            "configured_route_count": len(configured), "ready_route_count": len(ready),
            "bound_proof_file_count": bound_files, "record_valid": record_valid,
            "expires_at": record.expires_at}


def journal(path):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - 65_536)
            handle.seek(start); data = handle.read(65_536)
        if start:
            data = data.split(b"\n", 1)[1]
        line = [row for row in data.splitlines() if row][-1]
        value = json.loads(line)
        base = {key: item for key, item in value.items() if key != "hash"}
        digest = hashlib.sha256(json.dumps(base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        valid = value.get("hash") == digest and isinstance(value.get("kind"), str)
        return {"healthy": valid, "available": True, "status": "ok" if valid else "invalid_last_event",
                "last_event": {"kind": value.get("kind"), "at": value.get("at"),
                               "sequence": value.get("sequence"), "self_hash_valid": valid}}
    except FileNotFoundError:
        return {"healthy": False, "available": False, "status": "missing"}
    except (OSError, IndexError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"healthy": False, "available": False, "status": "unreadable_tail"}


def heartbeat(now):
    try:
        with ROLE.open() as handle:
            role = handle.read(33).strip().lower()
        if role not in {"active", "standby"}:
            role = ""
    except OSError:
        role = ""
    try:
        file_age = max(0, now - HEARTBEAT.stat().st_mtime)
        with HEARTBEAT.open() as handle:
            raw = handle.read(65).strip()
        if len(raw) > 64:
            raise ValueError("heartbeat_too_large")
        epoch_age = max(0, now - float(raw))
    except (OSError, ValueError):
        file_age, epoch_age = None, None
    fresh = file_age is not None and file_age < MAX_AGE
    healthy = role == "active" or (role == "standby" and fresh)
    status = "local_primary" if role == "active" else "ok" if healthy else "stale" if role == "standby" else "role_missing"
    return {"healthy": healthy, "available": bool(role), "status": status, "role": role or None,
            "heartbeat_mtime_age_seconds": file_age, "reported_epoch_age_seconds": epoch_age,
            "fresh": fresh, "threshold_seconds": MAX_AGE}


def observe(runner=subprocess.run, now=None):
    now = time.time() if now is None else now
    service_state = services(runner)
    value = {"schema": "idol.health-probe.v1", "observed_at": now,
             "services": service_state,
             "endpoints": endpoints(runner), "tailnet_mac_mini": tailnet(runner),
             "primary_heartbeat": heartbeat(now),
             "calibrations": {key: calibration(path, now, service_state.get(f"idol_fleet_{key}", {}).get("mode", "unknown"))
                              for key, path in CONFIGS.items()},
             "journals": {key: journal(path) for key, path in JOURNALS.items()}}
    reasons = []
    for section in ("services", "endpoints", "calibrations", "journals"):
        for key, check in value[section].items():
            if not check["healthy"]:
                reasons.append(f"{section}:{key}:{check.get('status', check.get('active', 'unhealthy'))}")
    for section in ("tailnet_mac_mini", "primary_heartbeat"):
        if not value[section]["healthy"]:
            reasons.append(f"{section}:{value[section]['status']}")
    value["overall"] = {"healthy": not reasons, "status": "healthy" if not reasons else "unhealthy",
                        "reasons": sorted(reasons)}
    return value


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in sorted(value.items())
                if key not in {"observed_at", "expires_at", "at", "sequence", "fingerprint"}
                and not key.endswith("_age_seconds")}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def atomic(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".current.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path); os.chmod(path, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def persist(value):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700); os.chmod(STATE, 0o700)
    lock = os.open(STATE / "health-probe.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        identity = stable(value)
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        current = STATE / "current.json"
        try:
            previous = load_json(current).get("fingerprint")
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None
        value["fingerprint"] = fingerprint; atomic(current, value)
        if previous == fingerprint:
            return False
        event = (json.dumps({"at": value["observed_at"], "fingerprint": fingerprint,
                            "previous_fingerprint": previous, "identity": identity},
                           sort_keys=True, separators=(",", ":")) + "\n").encode()
        log = STATE / "transitions.jsonl"
        if log.exists() and log.stat().st_size + len(event) > 1_048_576:
            os.replace(log, STATE / "transitions.jsonl.1")
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600); os.write(descriptor, event); os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); os.close(lock)


if __name__ == "__main__":
    report = observe(); changed = persist(report)
    print(json.dumps({"healthy": report["overall"]["healthy"], "status": report["overall"]["status"],
                      "reasons": report["overall"]["reasons"], "transition": changed,
                      "state": str(STATE / "current.json")}, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0 if report["overall"]["healthy"] else 1)
