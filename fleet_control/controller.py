from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .util import parse_time, stable_id


class PlanError(RuntimeError):
    pass


_ALLOWED_COST_CLASSES = frozenset({"local", "free", "included"})
_FORBIDDEN_COST_CLASSES = frozenset(
    {"paygo", "pay_go", "purchased_credit", "purchased_credits", "topup", "top_up", "unknown"}
)
_ACTIVE_AGENT_STATES = frozenset({"starting", "running", "waiting", "reviewing", "integrating"})
_ACTIVE_TASK_STATES = frozenset(
    {"productive_ready", "architecture_ready", "implementation_ready", "running", "implemented", "review_ready", "reviewing", "evidence_ready", "integration_ready"}
)
_PRIORITY = {"P0": 100.0, "P1": 60.0, "P2": 30.0, "P3": 10.0}


@dataclass(frozen=True)
class FleetPolicy:
    max_agents_total: int = 12
    max_starts_per_tick: int = 4
    stale_agent_seconds: int = 1_800
    checkpoint_buffer_minutes: int = 20
    minimum_remaining_fraction: float = 0.02
    reset_urgency_minutes: int = 180
    require_independent_review_family: bool = True
    automatic_merge: bool = False
    allow_paygo: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FleetPolicy":
        policy = cls(
            max_agents_total=int(value.get("max_agents_total", 12)),
            max_starts_per_tick=int(value.get("max_starts_per_tick", 4)),
            stale_agent_seconds=int(value.get("stale_agent_seconds", 1_800)),
            checkpoint_buffer_minutes=int(value.get("checkpoint_buffer_minutes", 20)),
            minimum_remaining_fraction=float(value.get("minimum_remaining_fraction", 0.02)),
            reset_urgency_minutes=int(value.get("reset_urgency_minutes", 180)),
            require_independent_review_family=bool(value.get("require_independent_review_family", True)),
            automatic_merge=bool(value.get("automatic_merge", False)),
            allow_paygo=bool(value.get("allow_paygo", False)),
        )
        if policy.allow_paygo:
            raise PlanError("pay-go and purchased-credit capacity are not admissible")
        if policy.automatic_merge:
            raise PlanError("automatic merge is not admissible; admission remains explicit")
        if policy.max_agents_total < 1 or policy.max_starts_per_tick < 0:
            raise PlanError("invalid concurrency policy")
        return policy


@dataclass(frozen=True)
class _ProviderCapacity:
    provider: dict[str, Any]
    minutes_to_reset: float | None
    remaining_fraction: float
    reset_urgency: float


class FleetController:
    def __init__(self, policy: FleetPolicy, now: datetime | None = None):
        self.policy = policy
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def plan(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._validate_snapshot(snapshot)
        repos = {str(row["id"]): row for row in snapshot.get("repositories", [])}
        tasks = {str(row["id"]): row for row in snapshot.get("tasks", [])}
        providers = {str(row["id"]): row for row in snapshot.get("providers", [])}
        agents = list(snapshot.get("agents", []))

        actions: list[dict[str, Any]] = []
        retiring: set[str] = set()
        for agent in agents:
            if str(agent.get("status")) not in _ACTIVE_AGENT_STATES:
                continue
            reason = self._retirement_reason(agent, tasks, repos)
            if reason:
                agent_id = str(agent["id"])
                retiring.add(agent_id)
                actions.extend(self._retire_actions(agent, reason))

        actions.extend(self._deduplicate_agents(agents, tasks, retiring))
        retiring.update(
            str(action["agent_id"])
            for action in actions
            if action["kind"] in {"agent.stop", "agent.suspend"}
        )
        active_after = [
            agent for agent in agents
            if str(agent.get("status")) in _ACTIVE_AGENT_STATES
            and str(agent.get("id")) not in retiring
        ]
        concurrency = self._provider_concurrency(active_after)
        occupied_paths, occupied_boundaries = self._occupied_claims(active_after, tasks)
        starts_left = min(
            max(0, self.policy.max_agents_total - len(active_after)),
            self.policy.max_starts_per_tick,
        )

        for stage in sorted(self._candidate_stages(tasks.values(), repos), key=self._stage_sort_key):
            if starts_left <= 0:
                break
            task = stage["task"]
            if self._overlaps_task(task, occupied_paths, occupied_boundaries):
                continue
            selection = self._select_provider(stage=stage, providers=providers.values(), concurrency=concurrency)
            if selection is None:
                continue
            provider_id = str(selection.provider["id"])
            agent_id = stable_id(
                "agent",
                {"task": task["id"], "stage": stage["role"], "provider": provider_id, "base_sha": task["base_sha"]},
            )
            claim_payload = {
                "task_id": task["id"],
                "repo_id": task["repo_id"],
                "base_sha": task["base_sha"],
                "paths": sorted(set(task.get("paths", []))),
                "semantic_boundaries": sorted(set(task.get("semantic_boundaries", []))),
                "lease_seconds": int(task.get("lease_seconds", self.policy.stale_agent_seconds)),
            }
            start_payload = {
                **claim_payload,
                "agent_id": agent_id,
                "provider_id": provider_id,
                "provider_family": selection.provider["family"],
                "model": selection.provider.get("model"),
                "role": stage["role"],
                "issue": task.get("issue"),
                "work_order": task.get("work_order"),
                "authority": task.get("authority", {}),
                "stop_conditions": task.get("stop_conditions", []),
                "evidence": task.get("evidence", []),
            }
            actions.append(self._action("claim.acquire", agent_id, claim_payload))
            actions.append(self._action("agent.start", agent_id, start_payload))
            occupied_paths.update(claim_payload["paths"])
            occupied_boundaries.update(claim_payload["semantic_boundaries"])
            concurrency[provider_id] = concurrency.get(provider_id, 0) + 1
            starts_left -= 1

        return {
            "schema": "idol.fleet.plan.v1",
            "observed_at": self.now.isoformat(),
            "automatic_dispatch": False,
            "automatic_merge": False,
            "paygo_allowed": False,
            "actions": actions,
            "summary": self._summary(actions),
        }

    def _validate_snapshot(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("schema") not in {"idol.fleet.snapshot.v1", None}:
            raise PlanError(f"unsupported snapshot schema: {snapshot.get('schema')!r}")
        repo_ids: set[str] = set()
        for repo in snapshot.get("repositories", []):
            for key in ("id", "head_sha"):
                if not str(repo.get(key, "")).strip():
                    raise PlanError(f"repository missing {key}")
            repo_id = str(repo["id"])
            if repo_id in repo_ids:
                raise PlanError(f"duplicate repository id: {repo_id}")
            repo_ids.add(repo_id)
        for provider in snapshot.get("providers", []):
            cost = str(provider.get("cost_class", "unknown")).lower()
            if cost in _FORBIDDEN_COST_CLASSES:
                continue
            if cost not in _ALLOWED_COST_CLASSES:
                raise PlanError(f"unrecognized provider cost class: {cost!r}")
            if not str(provider.get("family", "")).strip():
                raise PlanError(f"provider {provider.get('id')!r} missing family")
        for task in snapshot.get("tasks", []):
            for key in ("id", "repo_id", "base_sha", "state"):
                if not str(task.get(key, "")).strip():
                    raise PlanError(f"task missing {key}")
            if task["repo_id"] not in repo_ids:
                raise PlanError(f"task {task['id']} references unknown repository")
            if task.get("state") in _ACTIVE_TASK_STATES and not task.get("work_order"):
                raise PlanError(f"task {task['id']} has no exact work_order")

    def _retirement_reason(self, agent: dict[str, Any], tasks: dict[str, dict[str, Any]], repos: dict[str, dict[str, Any]]) -> str | None:
        task = tasks.get(str(agent.get("task_id", "")))
        if task is None:
            return "missing-work-order"
        if str(task.get("state")) not in _ACTIVE_TASK_STATES:
            return "task-not-active"
        repo = repos.get(str(task.get("repo_id")))
        if repo is None:
            return "repository-missing"
        if str(agent.get("base_sha")) != str(task.get("base_sha")):
            return "agent-work-order-sha-mismatch"
        if str(task.get("base_sha")) != str(repo.get("head_sha")):
            return "stale-repository-sha"
        last_activity = parse_time(str(agent.get("last_activity_at") or ""))
        if last_activity is None:
            return "missing-heartbeat"
        if (self.now - last_activity).total_seconds() > self.policy.stale_agent_seconds:
            return "stale-heartbeat"
        return None

    def _retire_actions(self, agent: dict[str, Any], reason: str) -> list[dict[str, Any]]:
        agent_id = str(agent["id"])
        checkpoint = {
            "agent_id": agent_id,
            "task_id": agent.get("task_id"),
            "reason": reason,
            "required_fields": ["exact_sha", "owned_paths", "semantic_boundaries", "last_command", "evidence", "blocker", "next_action"],
        }
        release = {
            "agent_id": agent_id,
            "task_id": agent.get("task_id"),
            "paths": agent.get("claims", {}).get("paths", []),
            "semantic_boundaries": agent.get("claims", {}).get("semantic_boundaries", []),
            "reason": reason,
        }
        return [
            self._action("agent.checkpoint", agent_id, checkpoint),
            self._action("agent.suspend", agent_id, {"reason": reason}),
            self._action("claim.release", agent_id, release),
        ]

    def _deduplicate_agents(self, agents: list[dict[str, Any]], tasks: dict[str, dict[str, Any]], retiring: set[str]) -> list[dict[str, Any]]:
        active = [row for row in agents if str(row.get("status")) in _ACTIVE_AGENT_STATES and str(row.get("id")) not in retiring]
        actions: list[dict[str, Any]] = []
        retired_here: set[str] = set()
        for index, left in enumerate(active):
            if str(left["id"]) in retired_here:
                continue
            for right in active[index + 1:]:
                if str(right["id"]) in retired_here or self._parallel_pair_allowed(left, right):
                    continue
                if not self._agents_overlap(left, right, tasks):
                    continue
                keep, retire = sorted(
                    (left, right),
                    key=lambda row: (
                        float(row.get("progress", 0.0)),
                        parse_time(str(row.get("last_activity_at") or "")) or datetime.min.replace(tzinfo=timezone.utc),
                        str(row.get("id")),
                    ),
                    reverse=True,
                )
                retired_here.add(str(retire["id"]))
                actions.extend(self._retire_actions(retire, f"duplicate-overlap-kept:{keep['id']}"))
        return actions

    @staticmethod
    def _parallel_pair_allowed(left: dict[str, Any], right: dict[str, Any]) -> bool:
        roles = {str(left.get("role")), str(right.get("role"))}
        same_task = left.get("task_id") == right.get("task_id")
        return same_task and roles in (
            {"implementer", "reviewer"},
            {"architect", "counterexample"},
            {"implementer", "evidence"},
        )

    @staticmethod
    def _agents_overlap(left: dict[str, Any], right: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> bool:
        left_task = tasks.get(str(left.get("task_id")), {})
        right_task = tasks.get(str(right.get("task_id")), {})
        left_paths = set(left.get("claims", {}).get("paths", left_task.get("paths", [])))
        right_paths = set(right.get("claims", {}).get("paths", right_task.get("paths", [])))
        left_bounds = set(left.get("claims", {}).get("semantic_boundaries", left_task.get("semantic_boundaries", [])))
        right_bounds = set(right.get("claims", {}).get("semantic_boundaries", right_task.get("semantic_boundaries", [])))
        return bool(left_paths & right_paths or left_bounds & right_bounds)

    @staticmethod
    def _provider_concurrency(agents: Iterable[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for agent in agents:
            provider_id = str(agent.get("provider_id", ""))
            if provider_id:
                out[provider_id] = out.get(provider_id, 0) + 1
        return out

    @staticmethod
    def _occupied_claims(agents: Iterable[dict[str, Any]], tasks: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
        paths: set[str] = set()
        boundaries: set[str] = set()
        for agent in agents:
            task = tasks.get(str(agent.get("task_id")), {})
            claims = agent.get("claims", {}) or {}
            paths.update(claims.get("paths", task.get("paths", [])))
            boundaries.update(claims.get("semantic_boundaries", task.get("semantic_boundaries", [])))
        return paths, boundaries

    def _candidate_stages(self, tasks: Iterable[dict[str, Any]], repos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        stages: list[dict[str, Any]] = []
        for task in tasks:
            if str(task.get("state")) not in _ACTIVE_TASK_STATES:
                continue
            repo = repos[str(task["repo_id"])]
            if str(task.get("base_sha")) != str(repo.get("head_sha")):
                continue
            role = self._required_role(task)
            if role is not None:
                stages.append({"task": task, "role": role})
        return stages

    @staticmethod
    def _required_role(task: dict[str, Any]) -> str | None:
        state = str(task.get("state"))
        if state in {"productive_ready", "architecture_ready"} and task.get("architecture_required", False) and not task.get("architecture_accepted", False):
            return "architect"
        if state in {"productive_ready", "implementation_ready"}:
            return str(task.get("required_role") or "implementer")
        if state == "implemented" and task.get("review_required", True):
            if not [row for row in task.get("reviews", []) or [] if row.get("verdict") == "accepted"]:
                return "reviewer"
        if state in {"review_ready", "reviewing"}:
            return "reviewer"
        if state == "evidence_ready":
            return "evidence"
        if state == "integration_ready":
            reviews = task.get("reviews", []) or []
            if not reviews or any(row.get("verdict") != "accepted" for row in reviews):
                return None
            if task.get("evidence_status") != "pass":
                return None
            return "integrator"
        return None

    @staticmethod
    def _overlaps_task(task: dict[str, Any], occupied_paths: set[str], occupied_boundaries: set[str]) -> bool:
        return bool(set(task.get("paths", [])) & occupied_paths or set(task.get("semantic_boundaries", [])) & occupied_boundaries)

    def _select_provider(self, *, stage: dict[str, Any], providers: Iterable[dict[str, Any]], concurrency: dict[str, int]) -> _ProviderCapacity | None:
        task = stage["task"]
        role = stage["role"]
        candidates: list[tuple[float, _ProviderCapacity]] = []
        for provider in providers:
            if provider.get("enabled", True) is not True:
                continue
            cost = str(provider.get("cost_class", "unknown")).lower()
            if cost not in _ALLOWED_COST_CLASSES or role not in set(provider.get("roles", [])):
                continue
            provider_id = str(provider["id"])
            if concurrency.get(provider_id, 0) >= int(provider.get("max_concurrency", 1)):
                continue
            family = str(provider["family"])
            if role == "reviewer" and self.policy.require_independent_review_family:
                implementer_family = str(task.get("implementer_family") or "")
                if implementer_family and family == implementer_family:
                    continue
            if family in set(task.get("excluded_families", [])):
                continue
            capacity = self._capacity(provider)
            if capacity.remaining_fraction < self.policy.minimum_remaining_fraction:
                continue
            estimate = float(task.get("estimate_minutes", 30))
            if capacity.minutes_to_reset is not None and estimate + self.policy.checkpoint_buffer_minutes > capacity.minutes_to_reset:
                continue
            quality = float(provider.get("quality", 0.5))
            minimum = float(task.get("minimum_quality", 0.0))
            if quality < minimum:
                continue
            priority = _PRIORITY.get(str(task.get("priority", "P2")), 20.0)
            local_bonus = 20.0 if cost in {"local", "free"} and role in {"observer", "evidence", "mechanic", "counterexample"} else 0.0
            premium_penalty = 15.0 if provider.get("premium", False) and minimum < 0.8 else 0.0
            score = priority + quality * 40.0 + capacity.reset_urgency + local_bonus - premium_penalty
            candidates.append((score, capacity))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], str(row[1].provider["id"])), reverse=True)
        return candidates[0][1]

    def _capacity(self, provider: dict[str, Any]) -> _ProviderCapacity:
        remaining = 1.0
        minutes_to_reset: float | None = None
        for window in provider.get("windows", []) or []:
            if "remaining_fraction" in window:
                remaining = min(remaining, max(0.0, min(1.0, float(window["remaining_fraction"]))))
            elif "used_percent" in window:
                remaining = min(remaining, max(0.0, min(1.0, (100.0 - float(window["used_percent"])) / 100.0)))
            reset_at = parse_time(str(window.get("resets_at") or window.get("reset_at") or ""))
            if reset_at is not None:
                candidate = max(0.0, (reset_at - self.now).total_seconds() / 60.0)
                minutes_to_reset = candidate if minutes_to_reset is None else min(minutes_to_reset, candidate)
        urgency = 0.0
        if minutes_to_reset is not None and minutes_to_reset <= self.policy.reset_urgency_minutes:
            urgency = remaining * 50.0 * (1.0 - minutes_to_reset / max(1.0, float(self.policy.reset_urgency_minutes)))
        return _ProviderCapacity(provider, minutes_to_reset, remaining, urgency)

    @staticmethod
    def _stage_sort_key(stage: dict[str, Any]) -> tuple[float, str]:
        task = stage["task"]
        return (-_PRIORITY.get(str(task.get("priority", "P2")), 20.0), str(task["id"]))

    @staticmethod
    def _action(kind: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        core = {"kind": kind, "agent_id": agent_id, "payload": payload}
        return {"id": stable_id("action", core), **core}

    @staticmethod
    def _summary(actions: list[dict[str, Any]]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for action in actions:
            summary[str(action["kind"])] = summary.get(str(action["kind"]), 0) + 1
        return summary
