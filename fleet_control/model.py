from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import pathlib
import re
import time
from typing import Any, Iterable, Mapping, Sequence


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SAFE_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class BillingClass(str, Enum):
    LOCAL = "local"
    INCLUDED = "included"
    PAYGO = "paygo"
    PURCHASED = "purchased"
    TOPUP = "topup"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BillingProof:
    kind: str
    subject_hash: str
    observed_at: float
    expires_at: float
    evidence_hash: str
    trusted: bool

    def valid(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return (
            self.trusted
            and bool(self.kind)
            and bool(self.subject_hash)
            and bool(self.evidence_hash)
            and self.observed_at <= current < self.expires_at
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BillingProof":
        return cls(
            kind=str(raw.get("kind", "")).strip(),
            subject_hash=str(raw.get("subject_hash", "")).strip(),
            observed_at=float(raw.get("observed_at", 0)),
            expires_at=float(raw.get("expires_at", 0)),
            evidence_hash=str(raw.get("evidence_hash", "")).strip(),
            trusted=raw.get("trusted") is True,
        )


@dataclass(frozen=True, slots=True)
class AllowanceWindow:
    label: str
    remaining_fraction: float
    resets_at: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.remaining_fraction <= 1.0:
            raise ValueError("remaining_fraction must be between 0 and 1")
        if self.resets_at <= 0:
            raise ValueError("resets_at must be a positive timestamp")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AllowanceWindow":
        return cls(
            label=str(raw.get("label", "window")).strip() or "window",
            remaining_fraction=float(raw.get("remaining_fraction", 0.0)),
            resets_at=float(raw.get("resets_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class Route:
    id: str
    provider: str
    model: str
    provider_family: str
    runtime: str
    command: tuple[str, ...]
    parser: str
    billing: BillingClass
    proof: BillingProof
    roles: frozenset[str]
    auth_env: tuple[str, ...] = ()
    timeout_seconds: int = 900
    max_parallel: int = 1
    premium: bool = False
    enabled: bool = True
    allowance: tuple[AllowanceWindow, ...] = ()
    proof_command: tuple[str, ...] = ()
    proof_expect: str = ""
    usage_command: tuple[str, ...] = ()
    usage_auth_env: tuple[str, ...] = ()
    usage_timeout_seconds: int = 30
    usage_max_age_seconds: int = 300
    usage_required: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("route id", self.id),
            ("provider", self.provider),
            ("provider family", self.provider_family),
            ("runtime", self.runtime),
        ):
            if not _ATOM_RE.fullmatch(value):
                raise ValueError(f"invalid {label}: {value!r}")
        if not self.model or len(self.model) > 240:
            raise ValueError("route model must be non-empty and bounded")
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("route command must be a non-empty argument vector")
        if self.parser not in {"openclaw-json", "hermes-usage", "plain-json"}:
            raise ValueError("unsupported route parser")
        if self.timeout_seconds < 10 or self.timeout_seconds > 172800:
            raise ValueError("route timeout outside supported bounds")
        if self.max_parallel < 1 or self.max_parallel > 64:
            raise ValueError("route max_parallel outside supported bounds")
        if self.usage_timeout_seconds < 1 or self.usage_timeout_seconds > 300:
            raise ValueError("usage timeout outside supported bounds")
        if self.usage_max_age_seconds < 15 or self.usage_max_age_seconds > 86400:
            raise ValueError("usage maximum age outside supported bounds")
        for name in (*self.auth_env, *self.usage_auth_env):
            if not _SAFE_ENV_RE.fullmatch(name):
                raise ValueError(f"invalid auth environment name: {name!r}")

    @property
    def subject_hash(self) -> str:
        payload = {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "provider_family": self.provider_family,
            "runtime": self.runtime,
            "command": self.command,
            "parser": self.parser,
            "billing": self.billing.value,
            "roles": sorted(self.roles),
            "auth_env": self.auth_env,
            "usage_command": self.usage_command,
            "usage_auth_env": self.usage_auth_env,
            "usage_timeout_seconds": self.usage_timeout_seconds,
            "usage_max_age_seconds": self.usage_max_age_seconds,
            "usage_required": self.usage_required,
        }
        return stable_hash(payload)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Route":
        return cls(
            id=str(raw.get("id", "")).strip(),
            provider=str(raw.get("provider", "")).strip(),
            model=str(raw.get("model", "")).strip(),
            provider_family=str(raw.get("provider_family") or raw.get("provider") or "").strip(),
            runtime=str(raw.get("runtime", "")).strip(),
            command=string_tuple(raw.get("command"), "command"),
            parser=str(raw.get("parser", "openclaw-json")).strip(),
            billing=BillingClass(str(raw.get("billing", "unknown")).strip().lower()),
            proof=BillingProof.from_mapping(mapping(raw.get("proof"), "proof")),
            roles=frozenset(string_tuple(raw.get("roles"), "roles")),
            auth_env=string_tuple(raw.get("auth_env", ()), "auth_env"),
            timeout_seconds=int(raw.get("timeout_seconds", 900)),
            max_parallel=int(raw.get("max_parallel", 1)),
            premium=raw.get("premium") is True,
            enabled=raw.get("enabled", True) is True,
            allowance=tuple(
                AllowanceWindow.from_mapping(mapping(item, "allowance item"))
                for item in sequence(raw.get("allowance", ()), "allowance")
            ),
            proof_command=string_tuple(raw.get("proof_command", ()), "proof_command"),
            proof_expect=str(raw.get("proof_expect", "")),
            usage_command=string_tuple(raw.get("usage_command", ()), "usage_command"),
            usage_auth_env=string_tuple(raw.get("usage_auth_env", ()), "usage_auth_env"),
            usage_timeout_seconds=int(raw.get("usage_timeout_seconds", 30)),
            usage_max_age_seconds=int(raw.get("usage_max_age_seconds", 300)),
            usage_required=raw.get("usage_required", False) is True,
        )


@dataclass(frozen=True, slots=True)
class WorkOrder:
    id: str
    task_id: str
    repository: pathlib.Path
    base_sha: str
    branch: str
    role: str
    required_outcome: str
    path_claims: tuple[str, ...]
    semantic_claims: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    witnesses: tuple[tuple[str, ...], ...]
    route_ids: tuple[str, ...]
    authority_files: tuple[str, ...]
    risk: str
    priority: int
    estimated_seconds: int
    estimated_tokens: int
    reviewer_family: str | None = None
    issue: str | None = None
    publish_branch: bool = True
    create_draft_pr: bool = True

    def __post_init__(self) -> None:
        for label, value in (("work-order id", self.id), ("task id", self.task_id), ("role", self.role)):
            if not _ATOM_RE.fullmatch(value):
                raise ValueError(f"invalid {label}: {value!r}")
        if not _SHA_RE.fullmatch(self.base_sha):
            raise ValueError("base_sha must be an exact lowercase 40-hex SHA")
        validate_branch(self.branch)
        if not self.required_outcome.strip():
            raise ValueError("required_outcome is required")
        if not self.path_claims or not self.semantic_claims:
            raise ValueError("work order requires path and semantic claims")
        for path in (*self.path_claims, *self.authority_files):
            validate_relative_path(path)
        for target in self.semantic_claims:
            validate_semantic_target(target)
        if not self.stop_conditions:
            raise ValueError("work order requires stop conditions")
        if not self.witnesses or any(not command for command in self.witnesses):
            raise ValueError("work order requires non-empty witness argument vectors")
        if not self.route_ids:
            raise ValueError("work order requires eligible route ids")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValueError("unsupported risk")
        if self.priority < 1 or self.priority > 100:
            raise ValueError("priority outside supported bounds")
        if self.estimated_seconds < 1 or self.estimated_seconds > 172800:
            raise ValueError("estimated_seconds outside supported bounds")
        if self.estimated_tokens < 1:
            raise ValueError("estimated_tokens must be positive")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkOrder":
        repository = pathlib.Path(str(raw.get("repository", ""))).expanduser()
        if not repository.is_absolute():
            raise ValueError("work-order repository must be absolute")
        witnesses = tuple(
            string_tuple(item, "witness command")
            for item in sequence(raw.get("witnesses"), "witnesses")
        )
        reviewer = raw.get("reviewer_family")
        issue = raw.get("issue")
        return cls(
            id=str(raw.get("id", "")).strip(),
            task_id=str(raw.get("task_id", "")).strip(),
            repository=repository,
            base_sha=str(raw.get("base_sha", "")).strip(),
            branch=str(raw.get("branch", "")).strip(),
            role=str(raw.get("role", "")).strip(),
            required_outcome=str(raw.get("required_outcome", "")).strip(),
            path_claims=string_tuple(raw.get("path_claims"), "path_claims"),
            semantic_claims=string_tuple(raw.get("semantic_claims"), "semantic_claims"),
            stop_conditions=string_tuple(raw.get("stop_conditions"), "stop_conditions"),
            witnesses=witnesses,
            route_ids=string_tuple(raw.get("route_ids"), "route_ids"),
            authority_files=string_tuple(raw.get("authority_files"), "authority_files"),
            risk=str(raw.get("risk", "high")).lower(),
            priority=int(raw.get("priority", 50)),
            estimated_seconds=int(raw.get("estimated_seconds", 900)),
            estimated_tokens=int(raw.get("estimated_tokens", 10000)),
            reviewer_family=str(reviewer).strip() if reviewer else None,
            issue=str(issue).strip() if issue else None,
            publish_branch=raw.get("publish_branch", True) is True,
            create_draft_pr=raw.get("create_draft_pr", True) is True,
        )


@dataclass(frozen=True, slots=True)
class Assignment:
    order: WorkOrder
    route: Route
    score: float
    reason: tuple[str, ...]


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return value


def string_tuple(value: Any, label: str) -> tuple[str, ...]:
    items = sequence(value, label)
    result = tuple(str(item).strip() for item in items)
    if any(not item for item in result):
        raise ValueError(f"{label} contains an empty item")
    return result


def validate_relative_path(value: str) -> None:
    path = pathlib.PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(char in value for char in ("\x00", "~"))
    ):
        raise ValueError(f"invalid repository-relative path: {value!r}")


def validate_semantic_target(value: str) -> None:
    if not value or value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError(f"invalid semantic target: {value!r}")
    if any(not _ATOM_RE.fullmatch(part) for part in value.split("/")):
        raise ValueError(f"invalid semantic target: {value!r}")


def validate_branch(value: str) -> None:
    if (
        not value
        or value.startswith(('/', '.'))
        or value.endswith(('/', '.', '.lock'))
        or '..' in value
        or '@{' in value
        or '\\' in value
        or any(part in {'', '.', '..'} for part in value.split('/'))
        or any(char.isspace() or ord(char) < 32 or char in '~^:?*[' for char in value)
    ):
        raise ValueError(f"invalid branch: {value!r}")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_routes(raw: Iterable[Mapping[str, Any]]) -> tuple[Route, ...]:
    routes = tuple(Route.from_mapping(item) for item in raw)
    ids = [route.id for route in routes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate route id")
    return routes
