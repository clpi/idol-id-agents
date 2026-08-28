from __future__ import annotations

import json
import re
from typing import Any

from .util import sanitize, stable_id, utc_now

_HANDOFF = re.compile(r"^IDOL_HANDOFF_V1=(\{.*\})\s*$", re.MULTILINE)
_ALLOWED_VERDICTS = frozenset(
    {
        "accepted",
        "no-counterexample",
        "counterexample-found",
        "ready-for-review",
        "pass",
        "ready-for-admission",
        "blocked",
        "refused",
    }
)


def build_prompt(payload: dict[str, Any], *, checkpoint: bool = False) -> str:
    role = str(payload.get("role") or "implementer")
    work = {
        "task_id": payload.get("task_id"),
        "issue": payload.get("issue"),
        "repo_id": payload.get("repo_id"),
        "base_sha": payload.get("base_sha"),
        "paths": payload.get("paths", []),
        "semantic_boundaries": payload.get("semantic_boundaries", []),
        "work_order": payload.get("work_order"),
        "work_order_id": payload.get("work_order_id"),
        "authority": payload.get("authority", {}),
        "stop_conditions": payload.get("stop_conditions", []),
        "evidence": payload.get("evidence", []),
        "role": role,
    }
    if checkpoint:
        purpose = (
            "Checkpoint the existing run immediately. Do not begin new work. Preserve unique "
            "uncommitted work, state the exact SHA and claims, and stop at a coherent boundary."
        )
    else:
        purpose = (
            "Execute only this bounded role. Verify the exact repository SHA before work. Read "
            "docs/spec/law.md, docs/spec/constitution.md, docs/bootstrap.md, AGENTS.md, and the "
            "named issue/gap as authority. Stop rather than invent semantics."
        )
    return f"""You are an Idol fleet {role}.

{purpose}

Non-negotiable invariants:
- one meaning, one exact semantic identity;
- source spelling, names, paths, hashes, AST kinds, opcodes, and backend tags are not meaning;
- worlds, authority, applications, demand, witnesses, provenance, transformations, and realization remain distinct graph facts;
- DNIR and backends are physical projections, never a second semantic language;
- Idol Live owns operational history/frontier, not compiler semantics;
- no pay-go, purchased credits, top-up, auto-reload, infrastructure creation, or automatic merge;
- no edits outside the claimed paths or semantic boundaries;
- positive, negative, deliberate-damage, restoration, and integrated evidence are required where named;
- a wrapper completing is not evidence that the requested inner outcome executed;
- preserve unique work before stopping; never use destructive cleanup.

Exact work order:
{json.dumps(work, indent=2, sort_keys=True)}

At the end, output exactly one machine-readable line and no text after it:
IDOL_HANDOFF_V1={{"schema":"idol.agent.handoff.v1","task_id":{json.dumps(payload.get('task_id'))},"role":{json.dumps(role)},"provider_family":{json.dumps(payload.get('provider_family'))},"base_sha":{json.dumps(payload.get('base_sha'))},"final_sha":"<exact SHA>","verdict":"<allowed verdict>","summary":"<bounded factual summary>","branch":"<branch or empty>","pull_request":null,"evidence":["<exact commands/outcomes/artifacts>"],"owned_paths":{json.dumps(payload.get('paths', []))},"semantic_boundaries":{json.dumps(payload.get('semantic_boundaries', []))},"last_command":"<exact last command or empty>","blocker":null,"next_action":"<exact next action>"}}
"""


def parse_handoff(text: str, *, expected: dict[str, Any]) -> dict[str, Any]:
    matches = list(_HANDOFF.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one IDOL_HANDOFF_V1 line, found {len(matches)}")
    value = json.loads(matches[0].group(1))
    if not isinstance(value, dict) or value.get("schema") != "idol.agent.handoff.v1":
        raise ValueError("handoff schema mismatch")
    for key in ("task_id", "role", "provider_family", "base_sha"):
        if value.get(key) != expected.get(key):
            raise ValueError(f"handoff {key} does not match the admitted work order")
    verdict = str(value.get("verdict") or "")
    if verdict not in _ALLOWED_VERDICTS:
        raise ValueError(f"closed verdict required, got {verdict!r}")
    if not isinstance(value.get("evidence"), list):
        raise ValueError("handoff evidence must be a list")
    cleaned = sanitize(value)
    cleaned["id"] = stable_id("handoff", cleaned)
    cleaned["completed_at"] = utc_now().isoformat()
    return cleaned
