from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .journal import Journal


CALIBRATION_FIELDS = (
    "no_paygo",
    "route_identity",
    "claim_control",
    "stale_sha_control",
    "overlap_control",
    "zero_edit_runtime",
    "bounded_mechanic",
)


def is_enabled(calibration: Path | None, config_dir: Path | None) -> bool:
    if calibration is None or config_dir is None:
        return False
    enabled = config_dir / "apply-enabled"
    if not calibration.is_file() or not enabled.is_file():
        return False
    digest = hashlib.sha256(calibration.read_bytes()).hexdigest()
    return enabled.read_text(encoding="utf-8").strip() == digest


def enable(calibration: Path, state: Path, config_dir: Path) -> str:
    data = json.loads(calibration.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "idol.fleet.calibration.v1":
        raise ValueError("calibration schema is absent or unsupported")
    missing = [field for field in CALIBRATION_FIELDS if data.get(field) is not True]
    if data.get("positive_cost_detected") is not False:
        missing.append("positive_cost_detected=false")
    if missing:
        raise ValueError("calibration is incomplete: " + ", ".join(missing))
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(calibration.read_bytes()).hexdigest()
    enabled = config_dir / "apply-enabled"
    fd = os.open(enabled, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (digest + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    Journal(state / "events.jsonl").append({
        "id": str(uuid.uuid4()),
        "kind": "apply-enabled",
        "at": time.time(),
        "fact": {"calibration_sha256": digest},
    })
    return digest


def disable(config_dir: Path) -> None:
    try:
        (config_dir / "apply-enabled").unlink()
    except FileNotFoundError:
        pass
