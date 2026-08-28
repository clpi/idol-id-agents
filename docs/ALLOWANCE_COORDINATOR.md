# Allowance coordinator

This is an operational scheduling tool, not Idol language law and not a model dispatcher.

It converts provider allowance evidence plus current-SHA work orders into a reviewable assignment proposal. It never logs in to a provider, invokes a model, redeems a reset, spends purchased credits, acquires claims, or edits a repository.

## Non-negotiable policy

- Optimize accepted semantic progress per marginal token, not raw utilization.
- Only `productive_ready` tasks with the current base SHA, evidence path, token/time estimate, path boundary, semantic boundary, and stop conditions are eligible.
- Purchased/pay-go/top-up capacity is rejected unless the input contains the JSON boolean `paygo_approved: true`; strings such as `"true"` or `"false"` are rejected.
- Local/free capacity handles census, reduction, fixtures, health checks, and deterministic evidence whenever its measured role fit is sufficient.
- Premium allowance is penalized for work that does not require it.
- Reset urgency increases only near an expiring window and never overrides quality, completion, evidence, independence, or conflict constraints.
- Work that cannot finish coherently before reset is rejected.
- Equal paths and ancestor/descendant paths conflict. Semantic boundaries may not overlap across parallel assignments.
- Independent review may not use the implementer’s provider family when `requires_different_family` is true.
- Duplicate task, provider, or allowance-window identities fail closed.
- Automatic dispatch is deliberately absent.

## Evidence boundary

The planner distinguishes a useful proposal from an execution-ready assignment.

An assignment is `execution_ready` only when all of the following are explicit:

1. allowance evidence comes from `live_provider` or `local_telemetry`;
2. exact model identity is verified;
3. a live claim is verified;
4. the work-order SHA equals the current repository SHA.

Cached, estimated, unsupported, and example telemetry may exercise the planner, but they cannot produce an execution-ready assignment. Missing evidence remains a visible blocker; it is never inferred from a branch, session label, provider alias, file presence, or successful transport.

`automatic_dispatch` is always `false`. Even an execution-ready proposal requires the repository's claim, review, admission, and completion rules.

## Usage

```sh
python3 scripts/allowance_plan.py config/allowance-policy.example.json --pretty
python3 -m unittest discover -s tests -v
```

The JSON output reports selected assignments, every rejected task/provider reason, projected window balances, telemetry provenance, and per-assignment execution blockers.

## Deployment boundary

A future dispatcher may consume the plan only after all of these exist:

1. exact provider/model/window telemetry without inference calls;
2. current-SHA productive work orders;
3. live path and semantic claim acquisition;
4. implementer/reviewer family separation;
5. a hard exclusion for pay-go providers unless the user explicitly approves them;
6. a terminal integrated evidence path;
7. a kill switch that stops on HEAD drift or unexpected graph movement.

The planner deliberately implements none of those side effects.
