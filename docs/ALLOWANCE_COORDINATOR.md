# Allowance coordinator

This is an operational scheduling tool, not Idol language law and not a model dispatcher.

It converts exact provider allowance telemetry plus current-SHA work orders into a reviewable assignment proposal. It never logs in to a provider, invokes a model, redeems a reset, spends purchased credits, acquires claims, or edits a repository.

## Non-negotiable policy

- Optimize accepted semantic progress per marginal token, not raw utilization.
- Only `productive_ready` tasks with the current base SHA, evidence path, token/time estimate, and stop conditions are eligible.
- Purchased/pay-go/top-up capacity is rejected unless the input explicitly sets `paygo_approved: true`.
- Local/free capacity handles census, reduction, fixtures, health checks, and deterministic evidence whenever its measured role fit is sufficient.
- Premium allowance is penalized for work that does not require it.
- Reset urgency increases only near an expiring window and never overrides quality, completion, evidence, independence, or conflict constraints.
- Work that cannot finish coherently before reset is rejected.
- Paths and semantic boundaries may not overlap across parallel assignments.
- Independent review may not use the implementer’s provider family when `requires_different_family` is true.
- Automatic dispatch is deliberately absent. Live claims and `.agents/WORK_ORDER.md` remain mandatory.

## Input telemetry

Allowance values must identify their source as `live_provider`, `local_telemetry`, `cached`, `estimated`, or `unsupported`. Estimated/example data may be used to test the planner but must not trigger dispatch.

Use exact model names. Never infer model identity from a branch, session label, workstream name, or provider alias.

## Usage

```sh
python3 scripts/allowance_plan.py config/allowance-policy.example.json --pretty
```

The JSON output reports selected assignments, every rejected task/provider reason, projected window balances, and safety warnings.

## Deployment boundary

A future dispatcher may consume the plan only after all of these exist:

1. exact provider/model/window telemetry without inference calls;
2. current-SHA productive work orders;
3. live path and semantic claim acquisition;
4. implementer/reviewer family separation;
5. a hard exclusion for pay-go providers;
6. a terminal integrated evidence path;
7. a kill switch that stops on HEAD drift or unexpected graph movement.
