# Idol fleet control plane

## Purpose

This repository is the operational control plane for continuous Idol and Idol
Live development. It does not own language meaning. It consumes exact authority
subjects from the repository and coordinates actors against them.

The optimization target is **accepted semantic progress per marginal included
allowance**, not raw token burn.

## Authority split

- `clpi/idol/docs/spec/law.md` owns compact language law.
- `docs/spec/constitution.md` owns structured `law.*` expansion.
- `docs/bootstrap.md` owns the executed compiler frontier.
- exact `gaps/GAP-*.md` records own open obligations.
- `tools/node/dev/claim` owns live path and semantic-boundary leases.
- this controller owns operational history, work-order lifecycle, allowance
  allocation, agent lifecycle, and the accepted collaboration frontier.
- Git branches and PRs are outward projections; they are not the history owner.

## Closed pipeline

```text
authority snapshot
  -> exact work order
  -> live claim
  -> architect/counterexample when required
  -> implementer
  -> independent reviewer family
  -> evidence
  -> integration candidate
  -> explicit admission
```

There is no automatic merge. A transport success, green wrapper, completed
agent, or consumed quota is not admission.

## Cost boundary

The controller admits only `local`, `free`, and `included` capacity. The
following fail closed in code, regardless of configuration:

- pay-go API use;
- purchased credits;
- top-ups and auto-reload;
- unknown cost class;
- extra usage;
- automatic reset-credit redemption;
- automatic upgrades or resource creation.

Both `IDOL_FLEET_APPLY=1` and
`IDOL_FLEET_COST_BOUNDARY=INCLUDED_ONLY` are required before an action adapter
may run. Planning remains read-only.

## Work-order contract

Every productive task must name:

- repository and exact base SHA;
- issue/gap identity;
- role and minimum quality;
- exact paths and semantic boundaries;
- authority references and hashes;
- estimate that fits before the relevant reset plus checkpoint buffer;
- stop conditions;
- positive, negative, deliberate-damage, restoration, and integrated evidence;
- independent reviewer requirement;
- terminal handoff path.

The controller never reconstructs a task from a branch name, PR title, source
spelling, chat transcript, or model summary.

## Live history

`history.ndjson` is an append-only hash chain. It retains observations, plans,
actions, refusals, results, reviews, and accepted operations. `live.json` is a
materialized projection:

```text
H = immutable causal operational history
F = accepted operational frontier
S = materialize(H, F)
```

This is the collaboration authority described by Idol Live. Compiler semantic
identity remains in the Idol semantic graph.

Prompts, transcripts, credentials, cookies, and secrets are omitted from the
journal. Live records exact actor, model/provider family, task, SHA, claims,
evidence, outcome, usage window, and provenance.

## Agent roles

- coordinator: deterministic scheduler and lifecycle reconciler;
- observer: claims, gaps, SHA, gates, and quota census;
- architect: semantic boundary and invariants;
- counterexample: falsifies the proposed boundary before implementation;
- implementer: one claimed work order;
- reviewer: independent provider family;
- evidence: controls, differential runs, artifacts, and performance;
- integrator: mechanical convergence after terminal review;
- janitor: releases stale leases and preserves unique work before suspension.

## Reset behavior

As an included reset approaches, the scheduler first adds independent review,
then counterexample work, fixtures, reductions, differential testing, artifact
inspection, and bounded research. It never starts work that cannot checkpoint
coherently before reset. Unused allowance is preferable to architectural debt.

## Deployment

The intended primary coordinator runs on the Mac mini against local OpenClaw
and local claim state. The Mac launchd template reconciles every five minutes.
Linux workers can run the systemd timer. Action commands are local adapters and
must return one JSON object with `ok: true|false`.

`apply_enabled` remains false until:

1. the local collector sees exact agents, sessions, providers, windows, and
   claims without exposing content;
2. provider cost classes are locally classified;
3. the old exposed Hermes/OpenClaw credentials are rotated;
4. action adapters pass fixture and live no-op controls;
5. one complete work order traverses claim -> agent -> review -> evidence ->
   integration-ready without an automatic merge.
