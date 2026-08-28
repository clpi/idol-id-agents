# Live Fleet Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a fail-closed 24/7 Idol fleet coordinator that projects its coordination facts into Idol Live, uses only proven included/local inference routes, prevents duplicate or stale work, and continuously advances current Idol and Live work.

**Architecture:** `clpi/idol-live` gains the native actor/task/claim/context-lease/budget/scheduler vocabulary and law tests. `clpi/idol-id-agents` gains a standard-library Python bootstrap daemon that observes Git/OpenClaw/Hermes/provider state, appends Live-shaped events, plans assignments, acquires Live and repository claims, materializes bounded work orders, invokes `openclaw agent exec` or Hermes one-shot runs, captures exact usage/evidence, and hands work to independent review. The daemon runs under `launchd` on the existing Mac mini; no new paid infrastructure is introduced.

**Tech Stack:** Idol `.id` law sources; Python 3.11+ standard library; Git and GitHub CLI; OpenClaw CLI; optional Hermes CLI; `launchd`; manually invoked no-inference GitHub Actions on the existing self-hosted runner.

**Spec:** `clpi/idol-live@1ce612eb70ffde75c34a20555d711999af0cc440:docs/superpowers/specs/2026-08-28-fleet-coordination-control-plane-design.md`

## Global Constraints

- Idol owns language semantics; Live owns collaboration truth; adapters are foreign projections/injections.
- Only `local` and proven `included` billing classes are eligible by default.
- `metered`, `purchased-credit`, `top-up`, `overage`, and `unknown` routes are always refused unless a separate expiring authorization names provider, model, and maximum spend.
- No credential value, prompt body, transcript, chain-of-thought, or unrelated file content may enter Git, events, logs, work orders, or CI output.
- Every editing/reviewing attempt is bound to exact repository and base SHA, semantic claims, path claims, context lease, stop conditions, witnesses, provider, model, runtime, and usage result.
- A model may not review or admit its own implementation.
- The bootstrap daemon may create worktrees, branches, commits, PRs, comments, and evidence; it may not merge, force-push protected branches, purchase, top up, rotate credentials, change billing, or deploy production infrastructure.
- Shell execution uses argument vectors; work-order paths are repository-relative and traversal-free.
- All modules use Python standard library only in v0.
- Initial installation is `observe-plan`; `apply` is enabled only after live calibration gates pass and a local non-secret enable file exists.
- No GitHub workflow runs automatically on push or pull request in v0; all workflows are `workflow_dispatch` only so creating or reviewing this branch cannot consume hosted minutes or trigger deployment.

---

## File Structure

### `clpi/idol-live`

- `graph/actor.id` — actor identity facts; model/provider/session remain provenance.
- `graph/task.id` — goal/task/dependency/completion-demand facts.
- `graph/claim.id` — semantic/source claim facts and modes.
- `graph/context.id` — context and context-lease facts.
- `agent/budget.id` — route, billing class, windows, reset and hard run budget.
- `coordination/claims.id` — acquire/renew/release/refuse relations.
- `coordination/leases.id` — exact observation validation and delta production.
- `coordination/scheduler.id` — eligibility and explained selection relation.
- `projection/fleet.id` — fleet/status/usage/work-order projection faces.
- `injection/agent.id` — foreign agent/run/provider observations become proposed events.
- `tests/laws/{claim,lease,budget,scheduler,authority}.id` — non-vacuous law examples.

### `clpi/idol-id-agents`

- `pyproject.toml` — package and `idol-fleet` entry point.
- `src/idol_fleet/model.py` — immutable dataclasses and validated IDs/paths.
- `src/idol_fleet/journal.py` — append-only Live-shaped JSONL event journal.
- `src/idol_fleet/policy.py` — billing/role/concurrency/admission policy loader.
- `src/idol_fleet/process.py` — bounded subprocess execution and redaction.
- `src/idol_fleet/observe.py` — Git, claim, OpenClaw, Hermes and provider telemetry observations.
- `src/idol_fleet/work_order.py` — task schema, validation and context materialization.
- `src/idol_fleet/scheduler.py` — deterministic eligibility/scoring and explanation.
- `src/idol_fleet/claims.py` — scheduler lease, semantic claims and repository path claims.
- `src/idol_fleet/worktree.py` — isolated exact-SHA worktree lifecycle.
- `src/idol_fleet/runtime.py` — OpenClaw/Hermes invocation and stable result parsing.
- `src/idol_fleet/coordinator.py` — observe/plan/dispatch/evidence/review/janitor state machine.
- `src/idol_fleet/cli.py` — `audit`, `plan`, `run-once`, `daemon`, `doctor`, `enable`, `disable`, `status`.
- `config/fleet-policy.example.json` — no-secret example policy.
- `config/work-orders.example.json` — exact current-SHA work-order examples.
- `launchd/com.idol.fleet.plist` — user service template.
- `scripts/install-fleet.sh` — idempotent user-local install.
- `.github/workflows/fleet-test.yml` — manual no-inference test run.
- `.github/workflows/fleet-deploy.yml` — manual self-hosted Mac-mini deploy/calibration, no model invocation.
- `tests/` — unit and integration tests with fake executables/repos.

---

### Task 1: Add native Live actor, task, claim, lease, budget and scheduler facts

**Files:**
- Create: `clpi/idol-live/graph/actor.id`
- Create: `clpi/idol-live/graph/task.id`
- Create: `clpi/idol-live/graph/claim.id`
- Create: `clpi/idol-live/graph/context.id`
- Create: `clpi/idol-live/agent/budget.id`
- Create: `clpi/idol-live/coordination/claims.id`
- Create: `clpi/idol-live/coordination/leases.id`
- Create: `clpi/idol-live/coordination/scheduler.id`
- Create: `clpi/idol-live/projection/fleet.id`
- Create: `clpi/idol-live/injection/agent.id`
- Create: `clpi/idol-live/tests/laws/claim.id`
- Create: `clpi/idol-live/tests/laws/lease.id`
- Create: `clpi/idol-live/tests/laws/budget.id`
- Create: `clpi/idol-live/tests/laws/scheduler.id`
- Create: `clpi/idol-live/tests/laws/authority.id`

**Interfaces:**
- Consumes: existing `identity`, `event`, `history`, `frontier`, `attempt`, `demand`, `witness`, `world`, projection and injection relations.
- Produces: native vocabulary used by the bootstrap journal schema: `actor`, `goal`, `task`, `claim`, `context`, `lease`, `budget`, `route`, `schedule`, and fleet projection/injection faces.

- [ ] **Step 1: Write law examples before the vocabulary exists**

Create `tests/laws/claim.id` with two actors attempting overlapping exclusive claims and an assertion in comments that the second attempt refuses while a disjoint observation claim is admitted. Create corresponding examples for a stale context lease, an unknown billing route, provider-family-separated review, and authority-shaped injected values.

- [ ] **Step 2: Run the current Idol checker and confirm each example fails for a missing relation**

```bash
for f in tests/laws/{claim,lease,budget,scheduler,authority}.id; do
  ../idol/zig-out/bin/idol check "$f"
done
```

Expected: nonzero for the newly referenced missing Live relations, not syntax errors.

- [ ] **Step 3: Add the minimum graph facts and relations**

Implement facts without subclass kingdoms. Keep provider/model/session/host as actor provenance and route facts, not identity. Keep claim modes and billing classes as facts accepted by laws, not an enum hierarchy.

- [ ] **Step 4: Re-run the checker and focused law tests**

```bash
for f in tests/laws/{claim,lease,budget,scheduler,authority}.id; do
  ../idol/zig-out/bin/idol check "$f"
done
```

Expected: exit 0 for all files on a current compiler capable of checking this source. Where the current compiler cannot execute the law examples, record the exact refusal and keep the examples as source law rather than weakening them.

- [ ] **Step 5: Commit the native Live vocabulary**

```bash
git add graph agent coordination projection injection tests/laws
git commit -m "live: add fleet coordination facts and laws"
```

---

### Task 2: Establish immutable bootstrap models and an append-only journal

**Files:**
- Create: `pyproject.toml`
- Create: `src/idol_fleet/__init__.py`
- Create: `src/idol_fleet/__main__.py`
- Create: `src/idol_fleet/model.py`
- Create: `src/idol_fleet/journal.py`
- Create: `tests/test_model.py`
- Create: `tests/test_journal.py`

**Interfaces:**
- Produces: `Event`, `Observation`, `Route`, `AllowanceWindow`, `Task`, `Claim`, `ContextLease`, `WorkOrder`, `RunResult`, `ReviewDemand`; `Journal.append(event)` and `Journal.read()`.

- [ ] **Step 1: Write failing model validation tests**

```python
class ModelTests(unittest.TestCase):
    def test_repository_path_refuses_parent_traversal(self):
        with self.assertRaises(ValueError):
            RepositoryPath("../idol/docs/spec/law.md")

    def test_route_unknown_billing_is_not_eligible(self):
        route = Route(id="r", provider="x", model="m", runtime="openclaw", billing="unknown")
        self.assertFalse(route.included)
```

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_model -v
```

Expected: import failure because `idol_fleet.model` does not exist.

- [ ] **Step 3: Implement frozen dataclasses and validators**

Use `dataclasses.dataclass(frozen=True, slots=True)`. Validate identifiers, repository-relative paths, SHA-1/SHA-256 hex forms, timestamps with timezone, billing classes, roles and run modes. Preserve unknown as a first-class value.

- [ ] **Step 4: Write failing journal durability tests**

Test append order, unique event IDs, truncated final-line recovery, `0600` files, and rejection of prompt/transcript/credential-shaped keys.

- [ ] **Step 5: Implement the append-only journal**

Use one JSON object per line, `os.open(..., 0o600)`, `fcntl.flock`, `flush`, and `os.fsync`. Never rewrite prior events. Snapshot files may be regenerated from the journal.

- [ ] **Step 6: Run tests**

```bash
python3 -m unittest tests.test_model tests.test_journal -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/idol_fleet tests/test_model.py tests/test_journal.py
git commit -m "fleet: add Live-shaped models and append-only journal"
```

---

### Task 3: Enforce no-pay-go route policy and route proof

**Files:**
- Create: `src/idol_fleet/policy.py`
- Create: `config/fleet-policy.example.json`
- Create: `tests/test_policy.py`

**Interfaces:**
- Produces: `Policy.load(path)`, `Policy.route_eligibility(route, now)`, `Policy.role_limits(role)`, `Eligibility(eligible, reasons)`.

- [ ] **Step 1: Write failing billing-class tests**

Cover `local` and `included` admission; refusal of `metered`, `purchased-credit`, `top-up`, `overage`, and `unknown`; expired spend authorization; provider/model mismatch; and an included route with explicit no-fallback policy.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_policy -v
```

Expected: import failure.

- [ ] **Step 3: Implement policy loading and hard eligibility**

Require every route entry to contain exact provider, model, runtime, billing class, proof source, maximum concurrency, supported roles, and fallback policy. The default policy contains no metered authorization object.

- [ ] **Step 4: Add a counterfactual policy fixture**

Create an in-test policy that labels an API-key route `included` without an accepted proof source. Assert it is refused with `billing-proof-untrusted`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_policy -v
git add src/idol_fleet/policy.py config/fleet-policy.example.json tests/test_policy.py
git commit -m "fleet: fail closed on unproven or metered inference routes"
```

---

### Task 4: Add bounded process execution and structured redaction

**Files:**
- Create: `src/idol_fleet/process.py`
- Create: `tests/test_process.py`
- Create: `tests/fixtures/fake-command`

**Interfaces:**
- Produces: `run_command(argv, cwd, timeout, env_allowlist) -> CommandResult`; `redact(value) -> JSONValue`.

- [ ] **Step 1: Write failing tests**

Test timeout, signal exit, JSON stdout, non-JSON stdout hashing, environment allowlist, path normalization, and rejection of shell-string invocation.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_process -v
```

- [ ] **Step 3: Implement argument-vector execution**

Use `subprocess.Popen` with `start_new_session=True`, a minimal environment, `communicate(timeout=...)`, `SIGTERM`, bounded grace, then `SIGKILL`. Return the inner exit status and separate timeout/signal facts.

- [ ] **Step 4: Implement recursive redaction**

Reject or redact credential, token, secret, password, cookie, authorization, prompt, transcript, content, reasoning and private-key fields. Replace home paths and emails. Store hashes for large output instead of bodies.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_process -v
git add src/idol_fleet/process.py tests/test_process.py tests/fixtures/fake-command
git commit -m "fleet: add bounded process and secret-safe evidence capture"
```

---

### Task 5: Observe repositories, claims, OpenClaw, Hermes and allowance windows

**Files:**
- Create: `src/idol_fleet/observe.py`
- Create: `tests/test_observe.py`
- Create: `tests/fixtures/fake-openclaw`
- Create: `tests/fixtures/fake-hermes`
- Create: `tests/fixtures/fake-claim`

**Interfaces:**
- Produces: `Observer.snapshot(config) -> Snapshot`; provider/runtime observations tagged `observed`, never inferred.

- [ ] **Step 1: Write failing repository observation tests**

Use a temporary Git repository. Assert exact HEAD, branch, dirty-path count without contents, remote host/repo identity with credentials stripped, and failure when an expected authority file is absent.

- [ ] **Step 2: Write failing claim/runtime observation tests**

Fake `tools/node/dev/claim list`, `openclaw models status --json`, `openclaw gateway status --json`, `openclaw sessions list --json`, `hermes sessions list`, `hermes dashboard --status`, and provider usage responses. Assert no message bodies are retained.

- [ ] **Step 3: Verify RED**

```bash
python3 -m unittest tests.test_observe -v
```

- [ ] **Step 4: Implement conservative observers**

Every absent command, parse failure, unknown field, and version skew becomes an explicit observation failure. Never infer unlimited allowance from absent telemetry. Never use model inference to audit the fleet.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_observe -v
git add src/idol_fleet/observe.py tests/test_observe.py tests/fixtures
git commit -m "fleet: observe repositories claims runtimes and allowances"
```

---

### Task 6: Validate and materialize bounded work orders

**Files:**
- Create: `src/idol_fleet/work_order.py`
- Create: `config/work-orders.example.json`
- Create: `tests/test_work_order.py`

**Interfaces:**
- Produces: `load_work_orders(path)`, `validate_work_order(order, snapshot)`, `materialize_prompt(order, context_delta)`.

- [ ] **Step 1: Write failing schema tests**

Refuse missing base SHA, no completion demand, no witnesses, no stop conditions, absolute/traversal paths, overlapping implement/review identity, unbounded run, stale current SHA, and forbidden credential/prompt fields.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_work_order -v
```

- [ ] **Step 3: Implement validation and minimal context projection**

The prompt authority order is: exact task constraints; current repository law/constitution/bootstrap/gap evidence; task observations and decisions; role-specific instructions. Include only demanded source excerpts and context delta, never whole conversation history.

- [ ] **Step 4: Add a stale-SHA damage control**

Create a valid order, advance a temporary repository, and assert validation returns `stale-base-sha` without materializing a prompt.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_work_order -v
git add src/idol_fleet/work_order.py config/work-orders.example.json tests/test_work_order.py
git commit -m "fleet: add exact-subject bounded work orders"
```

---

### Task 7: Replace the planner prototype with Live-aware deterministic scheduling

**Files:**
- Modify: `scripts/allowance_plan.py`
- Create: `src/idol_fleet/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Scheduler.plan(snapshot, tasks, active_attempts) -> Plan`; each assignment carries a score explanation and refusal reasons for every rejected candidate.

- [ ] **Step 1: Port prototype behavior into failing package tests**

Preserve no-pay-go, current SHA, readiness, estimates, evidence paths, stop conditions, reset fit, conflict detection, reviewer-family separation and premium mismatch penalty.

- [ ] **Step 2: Add coordination-cost tests**

Assert the scheduler prefers resident context, review-unblocking work, and critical-path tasks; refuses duplicate semantic attempts; limits simultaneous editing tasks; and expands to evidence work near reset.

- [ ] **Step 3: Verify RED**

```bash
python3 -m unittest tests.test_scheduler -v
```

- [ ] **Step 4: Implement scheduler with explicit explanation**

Use hard eligibility first, then a deterministic tuple score. Retain score inputs in the plan; never present the score as semantic truth.

- [ ] **Step 5: Keep the old script as a compatibility wrapper**

`scripts/allowance_plan.py` imports the package and translates the old JSON shape. Add a parity test proving the example result remains equivalent.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m unittest tests.test_scheduler -v
python3 scripts/allowance_plan.py config/allowance-policy.example.json >/tmp/plan.json
python3 -m json.tool /tmp/plan.json >/dev/null
git add scripts/allowance_plan.py src/idol_fleet/scheduler.py tests/test_scheduler.py
git commit -m "fleet: schedule included capacity against Live task facts"
```

---

### Task 8: Add scheduler, semantic and repository claim discipline

**Files:**
- Create: `src/idol_fleet/claims.py`
- Create: `tests/test_claims.py`

**Interfaces:**
- Produces: `SchedulerLease`, `SemanticClaimStore`, `RepositoryClaims.acquire/release/renew`, context managers that release on error.

- [ ] **Step 1: Write failing concurrency tests**

Use multiprocessing to race two scheduler leases, overlapping semantic targets, and overlapping repository paths. Assert exactly one owner succeeds.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_claims -v
```

- [ ] **Step 3: Implement local atomic leases and claim agreement**

Use `mkdir`/exclusive file creation plus owner metadata, TTL and heartbeat. Acquire semantic claim first and repository claims second. If any path claim fails, release every claim acquired in the transaction.

- [ ] **Step 4: Add dead-owner and stale-lease controls**

A stale lease may be recovered only after PID/liveness and TTL checks. Unique uncommitted work is never deleted by claim recovery.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_claims -v
git add src/idol_fleet/claims.py tests/test_claims.py
git commit -m "fleet: serialize scheduling and semantic source claims"
```

---

### Task 9: Create exact-SHA isolated worktrees

**Files:**
- Create: `src/idol_fleet/worktree.py`
- Create: `tests/test_worktree.py`

**Interfaces:**
- Produces: `WorktreeManager.create(order)`, `inspect(attempt)`, `retire(attempt)`.

- [ ] **Step 1: Write failing worktree tests**

Assert branch names are generated from attempt identity, base SHA matches exactly, dirty canonical checkout is untouched, foreign existing branches refuse, and retirement refuses dirty or unpushed unique work.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_worktree -v
```

- [ ] **Step 3: Implement worktrees without hidden cleanup**

Use `git worktree add --detach <path> <base_sha>`, then create a branch in the worktree. Never stash, hard-reset, clean, or delete an uncertain worktree. Record every path and commit in the attempt event.

- [ ] **Step 4: Run tests and commit**

```bash
python3 -m unittest tests.test_worktree -v
git add src/idol_fleet/worktree.py tests/test_worktree.py
git commit -m "fleet: isolate every attempt at its observed base"
```

---

### Task 10: Add OpenClaw and Hermes execution adapters

**Files:**
- Create: `src/idol_fleet/runtime.py`
- Create: `tests/test_runtime.py`

**Interfaces:**
- Produces: `OpenClawRuntime.execute(order, route, prompt_path) -> RunResult`; `HermesRuntime.execute(...) -> RunResult`.

- [ ] **Step 1: Write failing command-construction tests**

For OpenClaw, require:

```text
openclaw agent exec
--message-file <file>
--cwd <worktree>
--config <pinned route config>
--model <provider/model>
--timeout <seconds>
--json
```

For Hermes, require:

```text
hermes --oneshot <prompt>
--usage-file <file>
--provider <provider>
--model <model>
```

Assert fallback arguments are absent unless every fallback route is independently proven included/local.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_runtime -v
```

- [ ] **Step 3: Implement stable JSON/result parsing**

Capture provider, model, status, usage, estimated cost, tool summary, session ID hash, exit/timeout/signal and artifact hashes. A positive `costUsd` on a route declared included is a policy violation that holds the fleet and disables that route pending review.

- [ ] **Step 4: Add Codex subscription route controls**

Require pinned OpenClaw config selecting the official Codex harness and user/agent home policy explicitly. Reject an OpenAI Platform API-key route when the work order demands subscription capacity. Do not derive billing from `openai/` model spelling.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_runtime -v
git add src/idol_fleet/runtime.py tests/test_runtime.py
git commit -m "fleet: execute bounded OpenClaw and Hermes attempts"
```

---

### Task 11: Implement attempt state, evidence and independent review

**Files:**
- Create: `src/idol_fleet/coordinator.py`
- Create: `tests/test_coordinator.py`

**Interfaces:**
- Produces: `Coordinator.observe()`, `plan()`, `dispatch()`, `advance_attempt()`, `janitor()`.

- [ ] **Step 1: Write failing state-machine tests**

Cover:

```text
proposed → validated → claimed → running → evidence → review → ready
proposed → held
running → failed
running → stale
review → rejected
ready → superseded
```

Assert every transition appends an event and no prior event changes.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_coordinator -v
```

- [ ] **Step 3: Implement one-attempt orchestration**

Validate current SHA, acquire claims, create worktree, materialize prompt, invoke runtime, run exact deterministic witness commands, inspect changed paths, commit only allowed paths, release claims, and append outcome.

- [ ] **Step 4: Implement review demand**

A review attempt observes the implementer SHA, uses a different provider/runtime family, receives the diff plus exact authority and witnesses, and returns terminal findings. A comment from an unconfigured summary bot never satisfies review.

- [ ] **Step 5: Add damage controls**

Test runtime success with a failing witness; an agent result claiming success with no changes; changed paths outside claims; SHA movement during run; reviewer same family; and a wrapper completing while the inner command fails.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m unittest tests.test_coordinator -v
git add src/idol_fleet/coordinator.py tests/test_coordinator.py
git commit -m "fleet: drive attempts through evidence and independent review"
```

---

### Task 12: Add CLI, daemon loops, status and janitor

**Files:**
- Create: `src/idol_fleet/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces: `idol-fleet audit|plan|run-once|daemon|doctor|enable|disable|status`.

- [ ] **Step 1: Write failing CLI tests**

Assert `audit` and `plan` never invoke a model; `run-once` refuses in observe-plan mode; `enable` requires successful calibration record; `status` is secret-free; and signal shutdown releases the scheduler lease without deleting worktrees.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_cli -v
```

- [ ] **Step 3: Implement loops**

Observer interval 60 seconds, planner interval 300 seconds, janitor interval 600 seconds. Dispatcher is event-driven and respects role/provider/global slot limits. Use monotonic scheduling and signal-safe shutdown.

- [ ] **Step 4: Implement enable gate**

`enable` creates `~/.config/idol-fleet/apply-enabled` only when calibration records prove no-pay-go, route identity, claim, stale-SHA, overlap, zero-edit runtime and bounded-mechanic controls. The daemon checks this file and calibration hash before every dispatch.

- [ ] **Step 5: Run full test suite and commit**

```bash
python3 -m unittest discover -s tests -v
git add src/idol_fleet/cli.py tests/test_cli.py
git commit -m "fleet: add observe-plan daemon and guarded apply mode"
```

---

### Task 13: Install on the Mac mini without new infrastructure

**Files:**
- Create: `launchd/com.idol.fleet.plist`
- Create: `scripts/install-fleet.sh`
- Create: `.github/workflows/fleet-test.yml`
- Create: `.github/workflows/fleet-deploy.yml`
- Create: `tests/test_install.py`

**Interfaces:**
- Produces: idempotent user-local install at `~/.local/share/idol-fleet`, private state/config directories, and a `launchd` service in observe-plan mode.

- [ ] **Step 1: Write failing installer rendering tests**

Test paths with spaces, plist XML validity, `0600/0700` permissions, no embedded credentials, and no automatic apply enablement.

- [ ] **Step 2: Verify RED**

```bash
python3 -m unittest tests.test_install -v
```

- [ ] **Step 3: Implement idempotent install**

Create/update a venv with no external dependencies, copy config examples without overwriting live config, install the plist, bootstrap the service, and run `idol-fleet doctor`. Never delete preexisting state or credentials.

- [ ] **Step 4: Add manual-only workflows**

Both workflows use only `workflow_dispatch`. `fleet-test.yml` runs no-inference tests. `fleet-deploy.yml` targets the existing self-hosted Mac-mini runner, installs observe-plan mode, runs doctor/audit/plan, uploads only redacted artifacts, and never invokes a model or enables apply.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest discover -s tests -v
git add launchd scripts/install-fleet.sh .github/workflows tests/test_install.py
git commit -m "fleet: install observe-plan coordinator on existing Mac mini"
```

---

### Task 14: Calibrate live routes and admit the first productive attempt

**Files:**
- Create locally only: `~/.config/idol-fleet/policy.json`
- Create locally only: `~/.config/idol-fleet/routes/*.json5`
- Create via CLI: `~/.local/state/idol-fleet/calibration.json`
- Update: current task/work-order projection through Live/GitHub adapters.

**Interfaces:**
- Consumes: live OpenClaw/Hermes/provider state and current Idol authority.
- Produces: a signed/hash-bound calibration record and first accepted bounded attempt.

- [ ] **Step 1: Run read-only audit**

```bash
idol-fleet doctor
idol-fleet audit --json >~/.local/state/idol-fleet/snapshots/initial.json
idol-fleet plan --json >~/.local/state/idol-fleet/snapshots/initial-plan.json
```

Expected: no inference invocation, no credentials in output, exact route/fleet/claim/current-SHA facts or explicit unknowns.

- [ ] **Step 2: Classify every runtime route locally**

For each route, record proof source and billing class. Disable unknown and metered routes. Configure subscription-compatible Codex harness separately from OpenAI Platform. Configure no fallback to purchased credits.

- [ ] **Step 3: Run zero-edit calibrations**

Use a work order that asks the model to read exact files and return a structured authority summary without editing. Verify provider/model/runtime identity, zero changed paths, usage output and no positive pay-go cost.

- [ ] **Step 4: Verify stale-SHA and claim counterfactuals**

Run deliberate stale-base and overlapping-claim orders. Both must refuse before model invocation.

- [ ] **Step 5: Admit one bounded mechanic task**

Use the current PR/issue queue and select a current-SHA low-risk mechanical task with exact paths and gates. Require independent review where configured. Do not merge automatically.

- [ ] **Step 6: Enable apply only after reviewing the calibration record**

```bash
idol-fleet enable --calibration ~/.local/state/idol-fleet/calibration.json
launchctl kickstart -k "gui/$(id -u)/com.idol.fleet"
```

Expected: apply enable file created with the calibration hash; daemon begins only policy-eligible work.

---

### Task 15: Project bootstrap facts into Live and retire duplicate control logic

**Files:**
- Create: `clpi/idol-live/projection/bootstrap.id`
- Create: `clpi/idol-live/tests/laws/bootstrap-equivalence.id`
- Create: `docs/BOOTSTRAP_RETIREMENT.md`
- Add equivalence corpus under `tests/fixtures/live-events/`.

**Interfaces:**
- Produces: replay/equivalence gates between bootstrap JSONL outcomes and native Live relations.

- [ ] **Step 1: Record a sanitized event corpus from calibration and bounded attempts**

The corpus includes accepted, rejected, held, stale, exhausted, claim-conflict, review-rejected and superseded attempts, without prompt bodies or secrets.

- [ ] **Step 2: Write failing equivalence laws**

Assert native Live materialization and bootstrap projections agree on frontier membership, active claims, task readiness, route eligibility, required review and terminal outcomes.

- [ ] **Step 3: Implement projection relations and replay adapter**

Keep JSONL field names as foreign provenance. Map them to existing Live identities/facts; do not duplicate their vocabulary natively merely to mirror Python.

- [ ] **Step 4: Add retirement ledger**

For journal, task projection, semantic claims, lease validator, allowance planner and work-order materializer, name native replacement, equivalence gate, current consumers, and deletion condition.

- [ ] **Step 5: Run equivalence gates and commit**

```bash
python3 -m unittest tests.test_bootstrap_equivalence -v
../idol/zig-out/bin/idol check tests/laws/bootstrap-equivalence.id
git add projection tests/laws docs/BOOTSTRAP_RETIREMENT.md tests/fixtures/live-events
git commit -m "live: bind fleet bootstrap to native coordination facts"
```

---

## Plan self-review

- Every design requirement has an implementation task: native facts, foreign injections/projections, journal, route proof, no-pay-go, observers, work orders, deterministic scheduling, claims, worktrees, runtimes, evidence/review, daemon, installation, calibration and retirement.
- No task requires a third-party Python dependency.
- No workflow runs automatically or invokes a model.
- Production code steps follow a failing-test-first cycle.
- Interfaces use consistent names across tasks.
- Automatic merge, billing changes, top-ups, production deployment and credential handling remain outside the daemon's authority.
