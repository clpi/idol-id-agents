from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class BillingClass(str, Enum):
    LOCAL = "local"
    INCLUDED = "included"
    METERED = "metered"
    PURCHASED_CREDIT = "purchased-credit"
    TOP_UP = "top-up"
    OVERAGE = "overage"
    UNKNOWN = "unknown"


class RepositoryPath(str):
    def __new__(cls, value: str) -> "RepositoryPath":
        if not isinstance(value, str):
            raise TypeError("repository path must be a string")
        if (
            not value
            or value.startswith("/")
            or "//" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise ValueError(f"invalid repository-relative path: {value!r}")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"invalid repository-relative path: {value!r}")
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
            raise ValueError(f"invalid repository-relative path: {value!r}")
        return str.__new__(cls, value)


def _validate_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")


def _validate_sha(value: str) -> None:
    if not _SHA1_RE.fullmatch(value):
        raise ValueError(f"invalid git SHA-1: {value!r}")


@dataclass(frozen=True, slots=True)
class AllowanceWindow:
    label: str
    remaining_fraction: float | None
    reset_at: float | None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("allowance window label is required")
        if self.remaining_fraction is not None and not 0.0 <= self.remaining_fraction <= 1.0:
            raise ValueError("remaining_fraction must be in [0, 1]")

    def can_finish(self, *, now: float, estimated_seconds: int) -> bool:
        if estimated_seconds <= 0:
            return False
        if self.remaining_fraction is not None and self.remaining_fraction <= 0:
            return False
        if self.reset_at is None:
            return True
        return now + estimated_seconds <= self.reset_at


@dataclass(frozen=True, slots=True)
class Route:
    id: str
    provider: str
    model: str
    runtime: str
    billing: BillingClass
    proof: str
    roles: tuple[str, ...]
    max_concurrency: int
    windows: tuple[AllowanceWindow, ...] = ()
    fallbacks: tuple[str, ...] = ()
    provider_family: str | None = None
    config_path: str | None = None
    billing_proven: bool = False
    executable: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.id, "route id")
        for value, label in (
            (self.provider, "provider"),
            (self.runtime, "runtime"),
        ):
            if not value:
                raise ValueError(f"{label} is required")
        if not self.model:
            raise ValueError("model is required")
        if isinstance(self.billing, str) and not isinstance(self.billing, BillingClass):
            object.__setattr__(self, "billing", BillingClass(self.billing))
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if not self.roles:
            raise ValueError("at least one route role is required")

    @property
    def included(self) -> bool:
        return self.billing in {BillingClass.LOCAL, BillingClass.INCLUDED}

    @property
    def family(self) -> str:
        return self.provider_family or self.provider


@dataclass(frozen=True, slots=True)
class Snapshot:
    repository_heads: Mapping[str, str]
    active_semantic_claims: Mapping[str, str]
    active_path_claims: Mapping[str, str]
    route_status: Mapping[str, str] = field(default_factory=dict)
    observed_at: float | None = None

    def __post_init__(self) -> None:
        for sha in self.repository_heads.values():
            _validate_sha(sha)


@dataclass(frozen=True, slots=True)
class WorkOrder:
    id: str
    task_id: str
    repository: str
    base_sha: str
    branch: str
    role: str
    route_id: str
    semantic_claims: tuple[str, ...]
    path_claims: tuple[RepositoryPath, ...]
    goal: str
    required_outcome: str
    constraints: tuple[str, ...]
    forbidden_repairs: tuple[str, ...]
    witnesses: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    estimated_seconds: int
    max_tokens: int
    risk: str
    reviewer_family: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.id, "attempt id")
        _validate_id(self.task_id, "task id")
        _validate_id(self.route_id, "route id")
        _validate_sha(self.base_sha)
        if "/" not in self.repository or self.repository.startswith("/"):
            raise ValueError("repository must be owner/name")
        if not self.branch or self.branch.startswith("/") or ".." in self.branch:
            raise ValueError("invalid branch")
        for value, label in (
            (self.role, "role"),
            (self.goal, "goal"),
            (self.required_outcome, "required outcome"),
            (self.risk, "risk"),
        ):
            if not value:
                raise ValueError(f"{label} is required")
        if self.estimated_seconds <= 0 or self.max_tokens <= 0:
            raise ValueError("work order budgets must be positive")
        if not self.witnesses:
            raise ValueError("at least one witness is required")
        if not self.stop_conditions:
            raise ValueError("at least one stop condition is required")


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    role: str
    priority: int
    criticality: int
    estimated_seconds: int
    ready: bool
    semantic_targets: tuple[str, ...]
    path_targets: tuple[RepositoryPath, ...]
    resident_routes: tuple[str, ...]
    risk: str
    review_required: bool
    repository: str | None = None
    base_sha: str | None = None
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.id, "task id")
        if not self.role:
            raise ValueError("task role is required")
        if self.estimated_seconds <= 0:
            raise ValueError("estimated_seconds must be positive")
        if self.base_sha is not None:
            _validate_sha(self.base_sha)


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    signal: int | None = None


JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


def jsonable(value: Any) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: jsonable(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
