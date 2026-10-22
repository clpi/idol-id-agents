# IDOL and LIVE fleet controller

The fleet controller is the temporary operational adapter for continuous IDOL and LIVE development. LIVE owns collaboration truth; this repository hosts the deletable bootstrap that observes current tools, proposes work, and—only after calibration—dispatches bounded attempts.

## Authority

The controller never defines IDOL semantics. Every work order binds an exact repository SHA and names the authority files it must read. The prompt carries hashes and byte sizes rather than duplicating full authority files; the agent reads the files directly from its exact-SHA worktree. For `clpi/idol`, the default chain is:

1. `docs/spec/law.md`
2. `docs/spec/constitution.md`
3. `docs/bootstrap.md`
4. the exact gap or issue named by the work order
5. current live claims from `tools/node/dev/claim`

LIVE owns actors, tasks, attempts, claims, routes, allowance observations, witnesses, review, admission, immutable history, and the accepted frontier. Git branches and pull requests are compatibility projections.

## Safety invariants

- **Zero pay-go by default.** Only `local` and trusted `included` routes are eligible. `paygo`, `purchased`, `topup`, and `unknown` routes are rejected before planning or process launch.
- **Billing is witnessed, not inferred.** The word `included` is insufficient. A route needs an unexpired local calibration proof tied to the exact route configuration and its configured regular-file proof subjects.
- **Observe-plan is the installation default.** It may inspect local state and write private controller state. It cannot invoke a model, acquire claims, create worktrees, edit repositories, push branches, or create pull requests.
- **Apply is hash-bound.** Apply requires a current calibration record covering the controller version, configuration hash, route identity, billing proof, stale-SHA refusal, claim overlap, outside-path refusal, bounded process termination, and no-pay-go controls.
- **One attempt, exact subject.** A work order names one task, exact base SHA, exact path claims, semantic claims, required outcome, stop conditions, witnesses, role, route set, and expected review family.
- **The configured remote branch is part of admission.** When `remote_head_required` is enabled, observation and dispatch both require the authority clone's exact SHA to equal that branch. A missing or moved remote head fails closed.
- **Fast-forward does not silently redefine work.** In apply mode, `auto_fast_forward` may advance only a clean authority branch along a proven fast-forward with no live controller attempt or claims. A work order is rebound only when it explicitly opts into `follow_remote_main` and neither its claimed paths nor its authority manifest changed. Every other order remains stale and held for re-adjudication.
- **Two claim layers.** Every repository path enters the controller’s private hierarchical path store; IDOL additionally uses its repository-owned positional `tools/node/dev/claim` authority. Semantic boundaries use a separate private hierarchical store. Any conflict blocks dispatch. A repository without an executable claim adapter, such as today’s early LIVE source tree, must opt into the private path store explicitly with `repository_claim_required: false`.
- **No shell-shaped work orders.** Runtime commands and witness commands are argument vectors. The controller never evaluates work-order text through a shell.
- **Containment is measured after execution.** Any edit outside the claimed paths holds the attempt and preserves the worktree. A successful process is not successful work.
- **No-change is reviewable evidence, never an implementation escape hatch.** Only an explicit `allow_no_change` order in an evidence-producing role may proceed without a repository delta, and only after exact-HEAD checks bracket every bound witness on the exact subject. Its bounded, nonempty runtime output is retained privately and the unchanged base enters the ordinary `attempt.ready` review lifecycle. Implementation and mechanical orders still refuse empty work.
- **Evidence precedes review; review precedes admission.** The controller can create a draft branch/PR handoff, but never merges or admits its own attempt.
- **Uncertainty preserves evidence.** Failed, stale, conflicted, timed-out, or ambiguous attempts retain their worktrees and journal entries. Cleanup is an explicit later operation.
- **No automatic reset redemption or top-up.** Banked resets, extra usage, credits, and purchases require a separate human-authorized operation outside the controller.

## Control loop

Each cycle:

1. acquire the single-controller lease;
2. fetch and fast-forward a configured remote authority only under the bounded rebind law above, then observe local and remote SHA, dirty state, claims, queued work orders, route proofs, and allowance snapshots without model inference;
3. reject stale or incomplete work orders;
4. rank eligible `(work order, route)` pairs by priority, role fit, reset urgency, estimated completion likelihood, reviewer separation, and premium-capacity conservation;
5. write the observation and proposal to the append-only private journal;
6. stop in `observe-plan`, or in calibrated `apply` acquire both claim layers and dispatch bounded attempts;
7. validate edited paths, run witnesses, commit explicit claimed paths, optionally publish a draft handoff, and release claims;
8. leave acceptance to an independent reviewer and IDOL’s normal admission gates.

Runtime refusals open a per-route circuit for 5 minutes, then 15 minutes, then 1 hour, capped at 6 hours. A successful bounded run resets that circuit. Exhausted allowance disables only that route, so the same immutable work order remains eligible for another calibrated provider on the next cycle. Reviewed outcomes feed a bounded productivity factor into routing; unreviewed token volume never counts as progress.

Allowed no-change evidence is reviewable only when its stdout is complete and nonempty. The controller stores the exact bounded bytes in a mode-0600 file beneath a mode-0700 `candidate-evidence` state directory, then journals only `{path, sha256, size_bytes}`. The ready fact sets `no_change: true`, uses the exact base SHA as its candidate commit, and keeps `paths` empty. Outcome recording reopens the artifact without following its final path, validates its private mode, owner, stable file identity, digest, and size, and binds every terminal verdict to that digest. This protects against ordinary drift; it is not an attestation against malicious root replacement.

Assignments with disjoint path and semantic claims execute concurrently up to `max_assignments`. Claiming, journal writes, and worktree creation remain serialized by their own locks.

Unused included allowance is not itself waste. The objective is accepted semantic progress per marginal token. When a reset approaches, the scheduler expands into counterexample search, fixture generation, reduction, differential tests, artifact inspection, and independent review before starting speculative implementation that cannot finish coherently.

## Runtime adapters

The controller uses explicit local commands. The primary supported adapter is `openclaw agent exec`, because it offers isolated execution, exact working-directory placement, stable JSON result/usage metadata, explicit model selection, timeout handling, and no silent gateway-to-local retry. Hermes one-shot routes are supported only when the host calibration proves their provider identity, billing class, result accounting, and containment.

Routes authenticate using either local model execution, a witnessed subscription/OAuth login, or a witnessed fixed token-plan account. API keys and provider balances are never assumed to be included allowance. Provider credentials are not written to work orders, journal records, Git, or LIVE.

## Local session inventory

Both local controllers serialize the complete fresh process scan and gateway snapshot through the same private `~/.local/state/idol-fleet-inventory/snapshot.lock`. This prevents their own overlapping snapshot RPCs from counting one another as active gateway requests. Each caller still obtains and validates its own fresh observation; no payload cache, saved-session fallback, or new collaboration authority is introduced. The lock waits at most eight seconds, releases on process death, and validates same-account ownership, directory mode 0700, lock mode 0600, regular/single-link identity, and path/descriptor binding before publishing a result. A timeout or observed replacement refuses inventory. Other active or unidentified work still fences dispatch.

On Linux, provision that directory before starting either protected service and add exactly this shared metadata directory to both existing `ReadWritePaths` lists. Keep `ProtectSystem=strict` and all other restrictions. The adapter supports an explicit `IDOL_FLEET_INVENTORY_STATE_DIR` for direct invocations; fleet services use the common HOME-based default. The lock detects in-flight replacement; it is not protection against a trusted same-UID or root actor deliberately bypassing the protocol.

`scripts/openclaw-inventory-adapter.py` fences standalone agent processes and actual active Codex turns. It recognizes the installed Codex 0.152.0 app-server listener and its proxies only after a complete process scan, same-user socket ownership, kernel listener/PID binding, executable identity, daemon/CLI version agreement, and a bracketed native protocol query. The query initializes the existing Unix WebSocket daemon, follows every loaded-thread page, and reads each loaded thread with `includeTurns: false`. Verified idle threads are omitted; active threads remain unidentified work until an authoritative order/task binding exists. A proxy alone, unknown version, ambiguous listener, malformed response, changed identity, or unavailable query refuses inventory. No daemon is started, stopped, or reconfigured.

Process exclusions bind both PID and process start time. Thread title, preview, messages, and raw protocol responses are never serialized. `includeTurns: false` can still return incidental thread preview fields; only the approved identity/activity fields reach the controller. This is a point-in-time observation, not an atomic reservation against a new interactive turn starting later. Dispatch still requires the controller's claim and admission checks.

OpenClaw inventory uses one authenticated `idol.fleet.activeWork.snapshot` call to the temporary [execution snapshot extension](OPENCLAW_SNAPSHOT.md). The explicit loopback port and bracketed local listener identity bind the response to the host whose processes were inspected. It reads the gateway's existing execution aggregate, including cron work that saved session and job listings omit. The consumer requires the exact schema and OpenClaw version, every counter as a nonnegative safe integer, a consistent aggregate/idle value, and a timestamp inside the bounded observation window. Any busy counter holds additional admission. Missing extension, unsupported version, malformed or stale metadata, and unavailable counters refuse inventory. The consumer never falls back to an apparently idle saved-session list.

The extension is an unsupported private-API bridge pinned to one installed OpenClaw package version and exact module bytes. It exposes no additional execution state or session content and does not pause the gateway. Its counts overlap and must not be reported as distinct jobs. Both the gateway snapshot and local process query remain point-in-time observations; new interactive work can start afterward. Complete observation does not replace the controller's claims, calibration, or independent admission requirements.

## Deployment

### Additional local control

The controller is disabled for new dispatch by default, including when the control directory is absent. An explicit local control is an additional necessary gate: configuration mode, service observation holds, current calibration, route proofs, allowance, canonical claims, and work orders still apply. Enabling this gate does not authorize a route, spending, resets, source changes, publication, or merges.

Use the existing controller command with its exact private configuration:

```sh
idol-fleet --config /absolute/fleet.json control status
idol-fleet --config /absolute/fleet.json control enable --ttl-seconds 3600
idol-fleet --config /absolute/fleet.json control refresh --ttl-seconds 3600
idol-fleet --config /absolute/fleet.json control drain
idol-fleet --config /absolute/fleet.json control disable
```

Enable leases last 30 seconds to 24 hours. Expiry stops new admission; refresh is explicit. Drain rejects new attempts while preserving already-owned bounded attempts in the same enable epoch. Disable invalidates those permits at later boundaries. After drain, explicitly disable before enabling a new epoch. Existing route, witness, and claim timeouts bound owned work; extending an admission lease does not extend those execution limits.

The control lock brackets local claim mutations, repository-claim acquire/renew launches, worktree creation, provider and witness launches, and finalization mutations and readiness. Disable linearizes against those operations: a launch that wins the lock has already started; a disable that wins prevents it. The lock is released immediately after a provider or claim-command process starts. Already-started child operations may complete; later boundaries recheck the current control. Witness launches are checked too, and claim cleanup remains available after disable. This is an admission kill switch, not retroactive termination of an already-launched process.

Attempt permits use `idol.fleet.attempt-permit.v2` and authenticate a `subject_hash` in addition to the attempt identity and control epoch. Before the first claim, the controller renders the prompt from the exact authority checkout and computes a versioned SHA-256 subject over every `WorkOrder` field, the existing route subject hash, the actual route timeout, the attempt ID, the intended worktree path, and the exact prompt-byte digest. The route subject remains the existing calibration identity; because it excludes `timeout_seconds`, the launch subject binds that execution limit separately. Auth environment secret values are not serialized. The existing `attempt.started`, `attempt.executed`, and terminal journal records carry this digest; there is no additional identity registry. A v1 or unbound permit cannot authorize a provider launch.

The runtime recomputes that subject inside the existing process guard immediately before `Popen`. A changed order, scope, role, witness, outcome, route subject, timeout, attempt, prompt, or working directory raises `ExecutionBindingRefusal`, preserves the diagnostic worktree, releases this attempt's claims, and holds the order without automatic provider fallback. The runtime captures prompt bytes once before taking the control lock, then verifies those immutable bytes inside the guard. `{prompt_text}` receives their UTF-8 decoding, and `{prompt}` receives a private 0600 temporary snapshot of the same bytes, removed when the bounded process finishes or refuses. Rewriting the original prompt file after capture cannot change those arguments. The snapshot and local permit remain within the private-account trust boundary; they do not defend against a same-UID or root actor changing trusted process memory, binaries, snapshot files, or control keys. This binding authenticates physical execution inputs and does not establish semantic admission, distributed claims, or compute isolation.

Automatic fast-forward, when separately configured, checks the control before remote refresh, then authenticates again immediately before changing the authority checkout. Remote fetch does not hold the control lock. The bounded fast-forward and order rebinding share a maintenance guard: disable that wins first prevents both; an already-admitted maintenance transaction finishes before disable returns. Drain and expired admission leases refuse new maintenance.

After the final witness, the controller authenticates the owned attempt again. Every commit, push, and pull-request creation launch has its own guard, and recording readiness is guarded too. Disable while a witness runs prevents subsequent finalization; drain can finish the already-owned attempt. A publication process launched before disable may still complete under its existing timeout.

State lives under the controller's existing private state directory in `control/`. The directory is 0700; state, key, and stable lock/anchor records are 0600. Signed state binds generation and actual file identity, while the anchor rejects replay or unauthorized replacement. Updates use atomic replacement and fsync under one exclusive lock; interruption between state and anchor commits fails closed. Missing, partial, malformed, insecure, stale-for-new-admission, or tampered state never silently becomes enabled. Status is read-only. Ordinary operations refuse corrupt records instead of repairing them. The local key detects operational corruption within the private-account boundary; it cannot protect against a same-UID or root actor rewriting all trusted artifacts.

The observer installers install only `observe-plan`. On Linux, `scripts/install-fleet-systemd.sh CONFIG INSTANCE` is the explicit production transition for configurations without live process inventory. With live inventory, use the system-manager installation described below. The installer requires an `apply` configuration with safe automatic proof refresh, runs the full no-inference test suite, executes one observe-only cycle, calibrates every enabled route, disables the user-manager observer service, and starts a supervised `idol-fleet-INSTANCE.service` loop. Separate IDOL and LIVE instances use disjoint route capacity and private state so one long bounded run cannot starve the other repository.

The installer initializes an absent local control as disabled and preserves an existing valid control record. It refuses invalid state. It never enables or refreshes the local gate. Keep both `50-calibration-hold.conf` overrides during a held deployment and verify real service cycles, private control state, unchanged queues, zero claims, and zero model attempts before declaring that deployment complete.

Both Linux installers resolve a systemd-version-specific recovery policy before changing unit files or service state and write it as a separate `40-restart-backoff.conf` drop-in. On systemd 254 and newer, a failed service retries after 30 seconds and increases its delay over six steps to a 15-minute ceiling. Older releases use a portable fixed five-minute delay. Start rate limiting is disabled so repeated failures do not permanently latch the service; invalid version output refuses installation before mutation. This policy applies only after the service process exits: an explicit `systemctl stop` remains stopped, and later mode drop-ins such as `50-calibration-hold.conf` remain authoritative.

Apply-mode calibration may refresh automatically only when `auto_calibrate` is true. Refresh runs billing/auth and containment probes with `IDOL_FLEET_NO_MODEL_INFERENCE=1`; it cannot invoke a model, buy credits, redeem resets, or enable a route absent from the configuration. A changed configuration or controller version invalidates the prior proof.

Every enabled route must list its configured regular-file proof subjects in `proof_subject_files`. Include the proof adapter and the provider config, auth store, environment file, or manifest that can affect its billing/auth conclusion. Entries are mandatory absolute paths after `~` expansion; missing, unreadable, non-regular, or final-component symlink entries fail closed. Bind a symlink's resolved regular target explicitly when it is part of the proof. Calibration stores only canonical path and fingerprint metadata: file type, device/inode, mode, uid/gid, timestamps, size, and SHA-256. It reads each file through one nonblocking, no-follow descriptor before and after the existing proof command, rejects observed mutation, rechecks the fingerprint while planning, and checks it again before claims or model launch.

If one proof fails, independently current routes remain eligible. Failed routes retry after 5 minutes, 15 minutes, 1 hour, then 6 hours; deferred cycles continue observing and planning without repeating refusal events. If every proof fails, apply completes with all routes disabled and performs no claim or model action. Recovery is journaled when a fresh proof succeeds. This is an operational filesystem-drift detector, not an attestation against malicious root-level ABA replacement or unlisted transitive dependencies.

Use `idol-fleet --config ... record-outcome --receipt ...` to append an independently reviewed terminal outcome. Ready attempts remain held for review, and all non-runtime refusals remain held for operator diagnosis. Only a bounded runtime refusal is eligible for automatic provider fallback.

The bootstrap adapter is temporary. Its deletion witness is a LIVE runtime capable of observing the same facts, producing the same proposals, enforcing the same claims and route proofs, and emitting the same append-only attempt history without Python owning collaboration semantics.

### Open coordination and native-admission integration

The controller does not yet implement explicit `execute`/`deliberate` reasoning modes, parent/child delegation admission, or a native dispatch gate. The permit binding above does not complete those capabilities. The existing coordination policy and work-order shape remain owned by [IDOL PR #470](https://github.com/clpi/idol/pull/470); [agents PR #34](https://github.com/clpi/idol-id-agents/pull/34) owns its separate Slack intake work. Reasoning modes are distinct from compiler generations S1/B and S2/C.

The native owner can consume raw work-order evidence at two existing seams: inside the first `transition_permit("claim")` before `attempt.started`, and inside the runtime's guarded provider launch. A future contract must bind the immutable work order and route to the attempt while allowing phase-specific raw claim and journal evidence to change after acquisition. It must carry explicit mode, acknowledged owner and exact scope, parent work-record provenance, recipe/control/verifier evidence or a bounded deliberation question and exit artifact, dependency identity, attempt history, and the existing recovery owner. Native code must evaluate that evidence; a Python-produced exclusivity or admission boolean is not a substitute. Missing or refused native evidence must prevent launch without turning into automatic provider fallback. LIVE's post-execution `admit` gate is not a dispatch permit.

Delegated children must enter through the existing work-order, claim, and dispatch path. Parent provenance does not exempt overlapping claims: a parent holding an umbrella scope must hand it off or release it before a child owns a descendant scope. Current claims are local to their configured authority, and raw claim snapshots do not replace locked acquisition. These remaining native, delegation, and mode requirements need their owner's executable positive, negative, mutation, and exact-subject witnesses before they can be reported as implemented.

## Linux service identity

A controller that observes existing Codex processes must run in the same user namespace as those processes. In systemd’s user manager, filesystem protection settings implicitly create a private user namespace; on r16 this made `/proc/PID/exe` binding fail with EACCES even though the controller and Codex both appeared as UID 1000. An environment-only calibration replay did not reproduce that restriction.

Install these controllers as root through the system manager with `FLEET_SERVICE_PATH='<existing service PATH>' scripts/install-fleet-systemd.sh --system-user USER /absolute/fleet.json INSTANCE`. The explicit preserved PATH is required; the installer does not inherit root’s command path. The controller still runs as that existing non-root account and keeps the filesystem protections, private temporary directory, no-new-privileges setting, explicit writable paths, and bounded restart policy. Preflight commands use a clean account environment. System mode refuses nonempty route, usage, or inventory `auth_env` requirements because the emitted unit has no credential environment. Use the existing account’s HOME-based credential/configuration sources. Do not copy credentials or remove process identity checks to make inventory pass.

For a migration, preserve the exact configuration, holds, recovery policy, queues, journals, claims, and worktrees. Stop and disable the previous user-manager instance before starting its system-manager replacement; preserve a rollback copy and prevent the retired instance from running concurrently. Validate inventory from the actual replacement service before removing an observation hold. A successful external command with the same environment is insufficient.

Set `IDOL_HEALTH_FLEET_MANAGER=system` in the health observer’s environment after both controller instances use the system manager. The observer reports the selected manager and checks each controller only there; tunnels retain their existing user-manager checks. An invalid manager setting refuses a healthy result.

The version-pinned [systemd 257 namespace documentation](https://github.com/systemd/systemd/blob/v257/man/system-or-user-ns.xml) explains the implicit user namespace. This deployment preserves the filesystem restrictions while retaining same-account process visibility.
