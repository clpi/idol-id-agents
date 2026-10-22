from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import tarfile
import time
from typing import Any, Mapping, Sequence

from .control import ControlRefusal, LocalControl
from .gitops import current_sha, require_exact_subject
from .journal import Journal, JournalTransaction
from .model import Coordination, Route, WorkOrder, mapping, validate_relative_path
from .processes import kill_group_and_reap


class CoordinationError(ControlRefusal):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def order_mapping(order: WorkOrder) -> dict[str, Any]:
    value = asdict(order)
    value["repository"] = str(order.repository)
    return value


def assignment_subject(order: WorkOrder) -> str:
    return digest({"schema": "idol.fleet.assignment.v1", "order": order_mapping(order)})


def premise_subject(order: WorkOrder) -> str:
    # The recipe receipt acknowledges this complete premise before its own hash
    # can be selected. No other field is omitted from that binding.
    provisional = replace(order, coordination=replace(order.coordination, recipe="")) if order.coordination else order
    return assignment_subject(provisional)


def scope_subject(order: WorkOrder) -> str:
    return digest({"paths": order.path_claims, "semantics": order.semantic_claims, "role": order.role})


def route_subject(route: Route) -> str:
    return digest({"route_subject": route.subject_hash, "timeout_seconds": route.timeout_seconds})


def read_regular(path: Path, *, maximum: int = 16 * 1024 * 1024) -> bytes:
    """Capture one bounded regular file, without a final-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise CoordinationError(f"evidence is not a bounded regular file: {path}")
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(data) > maximum or identity(before) != identity(after):
            raise CoordinationError(f"evidence changed while reading: {path}")
        return data
    except (OSError, ValueError) as exc:
        raise CoordinationError(f"evidence is unreadable: {path}") from exc


def local_path(root: Path, relative: str) -> Path:
    validate_relative_path(relative)
    root = root.resolve(strict=True)
    path = root / relative
    try:
        path.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise CoordinationError("evidence path leaves its declared repository") from exc
    return path


def load_order(path: Path) -> WorkOrder:
    try:
        raw = json.loads(read_regular(Path(path), maximum=1024 * 1024))
        return WorkOrder.from_mapping(mapping(raw, "work order"))
    except (ValueError, TypeError, KeyError) as exc:
        raise CoordinationError("work order is malformed") from exc


def declaration(order: WorkOrder) -> Coordination:
    if order.coordination is None:
        raise CoordinationError("work order has no explicit coordination declaration")
    return order.coordination


def dependency_subject(order: WorkOrder, rows: Sequence[Mapping[str, Any]], *, observed: bool) -> str:
    """Hash actual authority bytes and admitted prerequisite event identities.

    The expected bytes come from the immutable Git base; the observed bytes and
    head come from the authority checkout. Neither accepts a caller reset token.
    ACK, delegation, and recipe receipts do not enter their own premise.
    """
    coordination = declaration(order)
    by_hash = {row.get("hash"): row for row in rows}
    prerequisites = []
    for reference in sorted(coordination.prerequisites):
        row = by_hash.get(reference)
        if row is None or row.get("kind") != "attempt.admitted":
            raise CoordinationError("prerequisite does not name an existing admitted outcome")
        prerequisites.append({"hash": reference, "fact": row.get("fact")})
    files = []
    # These are dependency sets. Permuting or repeating the same authority
    # cannot grant this stable task a fresh retry budget.
    for relative in sorted(set(order.authority_files)):
        if observed:
            data = read_regular(local_path(order.repository, relative))
        else:
            result = subprocess.run(
                ["git", "show", f"{order.base_sha}:{relative}"], cwd=order.repository,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False,
            )
            if result.returncode != 0:
                raise CoordinationError(f"authority file is absent from exact base: {relative}")
            data = result.stdout
        files.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    return digest({
        "schema": "idol.fleet.dependency.v1",
        "base": current_sha(order.repository) if observed else order.base_sha,
        "authority": files, "prerequisites": prerequisites,
    })


def _last(rows: Sequence[Mapping[str, Any]], kind: str, **facts: Any) -> Mapping[str, Any] | None:
    for row in reversed(rows):
        fact = row.get("fact")
        if row.get("kind") == kind and isinstance(fact, Mapping) and all(fact.get(k) == v for k, v in facts.items()):
            return row
    return None


def latest_assignment_ack(rows: Sequence[Mapping[str, Any]], task_id: str) -> Mapping[str, Any] | None:
    """Select raw acceptance evidence; only the native law decides its validity."""
    return _last(rows, "assignment.ack", task_id=task_id)


def acknowledge(
    *, journal: Journal, order: WorkOrder, route: Route, recipient: str,
    ttl_seconds: int = 3600, now: int | None = None,
) -> Mapping[str, Any]:
    """Record an explicit trusted-local recipient action, never delivery inference."""
    coordination = declaration(order)
    if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 86400:
        raise CoordinationError("ACK lease must be between 30 and 86400 seconds")
    if not isinstance(recipient, str) or not recipient or len(recipient) > 240:
        raise CoordinationError("explicit recipient is required")
    require_exact_subject(order.repository, order.base_sha)
    observed_at = int(time.time()) if now is None else now
    with journal.transaction() as transaction:
        dependency = dependency_subject(order, transaction.events(), observed=False)
        fact = {
            "schema": "idol.fleet.assignment-ack.v1", "task_id": order.task_id,
            "order_id": order.id, "assignment_subject": assignment_subject(order),
            "scope_subject": scope_subject(order), "base_sha": order.base_sha,
            "mode": coordination.mode, "recipient": recipient,
            "parent_task": coordination.parent_task, "parent_subject": coordination.parent_subject,
            "dependency_subject": dependency, "route_subject": route_subject(route),
            "route_id": route.id, "provider": route.provider, "model": route.model,
            "accepted_at": observed_at, "expires_at": observed_at + ttl_seconds,
            "source": "local-cli", "uid": os.getuid(), "accepted_order": order_mapping(order),
        }
        return transaction.append("assignment.ack", fact, at=observed_at)


def _publish_queue(order_dir: Path, fact: Mapping[str, Any]) -> Path:
    """Publish complete queue bytes once; a different existing file is a refusal."""
    order = WorkOrder.from_mapping(mapping(fact.get("work_order"), "delegated work order"))
    target = order_dir / f"{order.id}.json"
    content = canonical(order_mapping(order)) + b"\n"
    if hashlib.sha256(content).hexdigest() != fact.get("queue_sha256"):
        raise CoordinationError("delegation queue bytes differ from journaled intent")
    order_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        if read_regular(target, maximum=1024 * 1024) != content:
            raise CoordinationError("a different work order already occupies the child queue path")
        return target
    fd, temporary_name = tempfile.mkstemp(prefix="delegation-", suffix=".tmp", dir=order_dir)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if read_regular(target, maximum=1024 * 1024) != content:
                raise CoordinationError("a concurrent different child was queued")
        directory_fd = os.open(order_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def delegate(
    *, journal: Journal, work_orders_dir: Path, parent: WorkOrder, child: WorkOrder,
    owner: str, now: int | None = None,
) -> Mapping[str, Any]:
    """Journal accepted-parent provenance before publishing child queue bytes."""
    parent_coordination = declaration(parent)
    child_coordination = declaration(child)
    require_exact_subject(parent.repository, parent.base_sha)
    require_exact_subject(child.repository, child.base_sha)
    observed_at = int(time.time()) if now is None else now
    parent_subject = assignment_subject(parent)
    child_subject = assignment_subject(child)
    # These are exact provenance bindings, not native mode/admission verdicts.
    if child_coordination.parent_task != parent.task_id or child_coordination.parent_subject != parent_subject:
        raise CoordinationError("child does not reference this exact parent declaration")
    if owner != parent_coordination.recipient:
        raise CoordinationError("delegation owner differs from the declared parent recipient")
    if child.repository != parent.repository:
        raise CoordinationError("child must use the same configured repository")
    content = canonical(order_mapping(child)) + b"\n"
    with journal.transaction() as transaction:
        rows = transaction.events()
        accepted = _last(rows, "assignment.ack", assignment_subject=parent_subject, recipient=owner)
        if accepted is None:
            raise CoordinationError("parent has no explicit accepted assignment in this journal")
        prior = _last(rows, "assignment.delegated", assignment_subject=child_subject)
        if prior is None or prior["fact"].get("parent_ack") != accepted["hash"]:
            prior = transaction.append("assignment.delegated", {
                "schema": "idol.fleet.delegation.v1", "parent_task": parent.task_id,
                "parent_subject": parent_subject, "parent_ack": accepted["hash"], "owner": owner,
                "task_id": child.task_id, "order_id": child.id, "assignment_subject": child_subject,
                "recipient": child_coordination.recipient, "source": "local-cli", "uid": os.getuid(),
                "work_order": order_mapping(child), "queue_sha256": hashlib.sha256(content).hexdigest(),
            }, at=observed_at)
        target = _publish_queue(work_orders_dir, prior["fact"])
        if _last(transaction.events(), "assignment.queued", delegation_event=prior["hash"]) is None:
            transaction.append("assignment.queued", {
                "delegation_event": prior["hash"], "task_id": child.task_id,
                "assignment_subject": child_subject, "queue_path": str(target),
            }, at=observed_at)
        return prior


def recover_delegation(*, journal: Journal, work_orders_dir: Path, event: str) -> Mapping[str, Any]:
    """Retry the exact durable queue publication after an interrupted delegate."""
    with journal.transaction() as transaction:
        row = next((row for row in transaction.events({"assignment.delegated"}) if row["hash"] == event), None)
        if row is None:
            raise CoordinationError("unknown delegation event")
        target = _publish_queue(work_orders_dir, row["fact"])
        if _last(transaction.events(), "assignment.queued", delegation_event=event) is None:
            transaction.append("assignment.queued", {
                "delegation_event": event, "task_id": row["fact"]["task_id"],
                "assignment_subject": row["fact"]["assignment_subject"], "queue_path": str(target),
            })
        return row


def _recipe_command(raw: Any, label: str) -> tuple[tuple[str, ...], int]:
    value = mapping(raw, label)
    command = value.get("command")
    expected = value.get("expected_exit")
    if not isinstance(command, (list, tuple)) or not command or len(command) > 128 or any(
        not isinstance(part, str) or not part or "\0" in part or len(part) > 16384 for part in command
    ):
        raise CoordinationError(f"recipe {label} requires a bounded argument vector")
    if type(expected) is not int or not 0 <= expected <= 255:
        raise CoordinationError(f"recipe {label} expected exit must be an integer from 0 through 255")
    return tuple(command), expected


def _controlled_recipe_run(
    *, control: LocalControl, command: Sequence[str], cwd: Path, directory: Path,
    label: str, timeout: int,
) -> Mapping[str, Any]:
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    environment.update(IDOL_FLEET_NO_MODEL_INFERENCE="1", IDOL_FLEET_NO_PAYGO="1")
    stdout_path, stderr_path = directory / f"{label}.stdout", directory / f"{label}.stderr"
    started = time.time()
    process = None
    timed_out = False
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        try:
            # Recipe preparation is explicit local maintenance. It has no route
            # or attempt permit and cannot bypass disable/drain at code launch.
            with control.transition_permit("maintenance"):
                process = subprocess.Popen(
                    list(command), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            # A successful leader may leave ordinary children running in the
            # owned group. End that group before observing outputs or allowing
            # the next control to touch the checkout. Reaping preserves an
            # already observed leader exit; a timeout records its killed exit.
            kill_group_and_reap(process)
        except BaseException:
            if process is not None:
                kill_group_and_reap(process)
            raise
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    return {
        "command": tuple(command), "actual_exit": process.returncode,
        "started_at": started, "ended_at": time.time(), "timed_out": timed_out,
        "stdout_path": str(stdout_path), "stdout_sha256": hashlib.sha256(read_regular(stdout_path)).hexdigest(),
        "stderr_path": str(stderr_path), "stderr_sha256": hashlib.sha256(read_regular(stderr_path)).hexdigest(),
    }


def verify_recipe(
    *, journal: Journal, control: LocalControl, order: WorkOrder, specification: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Run explicit local verifier controls and journal their raw observations.

    The native law, not this producer or its event name, decides whether these
    observed exits qualify a recipe for the selected assignment.
    """
    coordination = declaration(order)
    require_exact_subject(order.repository, order.base_sha)
    positive, positive_expected = _recipe_command(specification.get("positive"), "positive")
    negative, negative_expected = _recipe_command(specification.get("negative"), "negative")
    recipe_id = specification.get("id")
    verifier = specification.get("verifier")
    artifact_name = specification.get("artifact")
    timeout = specification.get("timeout_seconds", min(coordination.allowance, 900))
    if not isinstance(recipe_id, str) or not recipe_id or not isinstance(verifier, str) or not verifier:
        raise CoordinationError("recipe id and explicit verifier are required")
    if not isinstance(artifact_name, str):
        raise CoordinationError("recipe artifact path is required")
    validate_relative_path(artifact_name)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise CoordinationError("recipe control timeout must be bounded from 1 through 3600 seconds")
    dependency = dependency_subject(order, journal.events(), observed=False)
    parent = journal.path.parent / "recipe-evidence"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="recipe-", dir=parent))
    checkout = directory / "checkout"
    checkout.mkdir(mode=0o700)
    archive = directory / "base.tar"
    observations: dict[str, Any] = {
        "schema": "idol.fleet.recipe-observation.v1", "recipe_id": recipe_id,
        "task_id": order.task_id, "scope_subject": scope_subject(order), "base_sha": order.base_sha,
        "dependency_subject": dependency, "premise_subject": premise_subject(order),
        "verifier": verifier, "source": "local-cli", "uid": os.getuid(),
        "specification": dict(specification), "evidence_directory": str(directory),
    }
    try:
        observations["checkout"] = _controlled_recipe_run(
            control=control, command=("git", "archive", "--format=tar", f"--output={archive}", order.base_sha),
            cwd=order.repository, directory=directory, label="checkout", timeout=30,
        )
        if observations["checkout"]["actual_exit"] != 0:
            raise CoordinationError("exact-base recipe checkout failed")
        with tarfile.open(archive, mode="r") as bundle:
            bundle.extractall(checkout, filter="data")
        archive.unlink()
        observations["positive"] = _controlled_recipe_run(
            control=control, command=positive, cwd=checkout, directory=directory,
            label="positive", timeout=timeout,
        )
        observations["negative"] = _controlled_recipe_run(
            control=control, command=negative, cwd=checkout, directory=directory,
            label="negative", timeout=timeout,
        )
        artifact_content = read_regular(local_path(checkout, artifact_name))
        artifact = directory / "artifact.bin"
        with artifact.open("xb") as handle:
            os.chmod(artifact, 0o600)
            handle.write(artifact_content)
            handle.flush()
            os.fsync(handle.fileno())
        observations.update({
            "observed_at": int(time.time()),
            "positive_control": digest({"command": positive, "expected_exit": positive_expected}),
            "positive_expected": positive_expected, "positive_actual": observations["positive"]["actual_exit"],
            "negative_control": digest({"command": negative, "expected_exit": negative_expected}),
            "negative_expected": negative_expected, "negative_actual": observations["negative"]["actual_exit"],
            "artifact_path": str(artifact), "artifact_sha256": hashlib.sha256(artifact_content).hexdigest(),
        })
        with journal.transaction() as transaction:
            # Ensure the prerequisite event identities still project completely;
            # freshness and equality themselves remain native dispatch inputs.
            observations["observed_dependency_subject"] = dependency_subject(order, transaction.events(), observed=True)
            return transaction.append("recipe.observed", observations, at=observations["observed_at"])
    except BaseException as exc:
        journal.append("recipe.interrupted", {
            **observations, "error_type": type(exc).__name__, "error": str(exc),
        })
        raise
