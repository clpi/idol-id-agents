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
- **No-change is evidence, never an implementation escape hatch.** Only an explicit `allow_no_change` order in an evidence-producing role may terminate without a repository delta, and only after exact-HEAD checks bracket every bound witness on the exact subject. Implementation and mechanical orders still refuse empty work.
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

Assignments with disjoint path and semantic claims execute concurrently up to `max_assignments`. Claiming, journal writes, and worktree creation remain serialized by their own locks.

Unused included allowance is not itself waste. The objective is accepted semantic progress per marginal token. When a reset approaches, the scheduler expands into counterexample search, fixture generation, reduction, differential tests, artifact inspection, and independent review before starting speculative implementation that cannot finish coherently.

## Runtime adapters

The controller uses explicit local commands. The primary supported adapter is `openclaw agent exec`, because it offers isolated execution, exact working-directory placement, stable JSON result/usage metadata, explicit model selection, timeout handling, and no silent gateway-to-local retry. Hermes one-shot routes are supported only when the host calibration proves their provider identity, billing class, result accounting, and containment.

Routes authenticate using either local model execution, a witnessed subscription/OAuth login, or a witnessed fixed token-plan account. API keys and provider balances are never assumed to be included allowance. Provider credentials are not written to work orders, journal records, Git, or LIVE.

## Local session inventory

`scripts/openclaw-inventory-adapter.py` fences standalone agent processes and actual active Codex turns. It recognizes the installed Codex 0.152.0 app-server listener and its proxies only after a complete process scan, same-user socket ownership, kernel listener/PID binding, executable identity, daemon/CLI version agreement, and a bracketed native protocol query. The query initializes the existing Unix WebSocket daemon, follows every loaded-thread page, and reads each loaded thread with `includeTurns: false`. Verified idle threads are omitted; active threads remain unidentified work until an authoritative order/task binding exists. A proxy alone, unknown version, ambiguous listener, malformed response, changed identity, or unavailable query refuses inventory. No daemon is started, stopped, or reconfigured.

Process exclusions bind both PID and process start time. Thread title, preview, messages, and raw protocol responses are never serialized. `includeTurns: false` can still return incidental thread preview fields; only the approved identity/activity fields reach the controller. This is a point-in-time observation, not an atomic reservation against a new interactive turn starting later. Dispatch still requires the controller's claim and admission checks.

OpenClaw inventory uses one authenticated `idol.fleet.activeWork.snapshot` call to the temporary [execution snapshot extension](OPENCLAW_SNAPSHOT.md). The explicit loopback port and bracketed local listener identity bind the response to the host whose processes were inspected. It reads the gateway's existing execution aggregate, including cron work that saved session and job listings omit. The consumer requires the exact schema and OpenClaw version, every counter as a nonnegative safe integer, a consistent aggregate/idle value, and a timestamp inside the bounded observation window. Any busy counter holds additional admission. Missing extension, unsupported version, malformed or stale metadata, and unavailable counters refuse inventory. The consumer never falls back to an apparently idle saved-session list.

The extension is an unsupported private-API bridge pinned to one installed OpenClaw package version and exact module bytes. It exposes no additional execution state or session content and does not pause the gateway. Its counts overlap and must not be reported as distinct jobs. Both the gateway snapshot and local process query remain point-in-time observations; new interactive work can start afterward. Complete observation does not replace the controller's claims, calibration, or independent admission requirements.

## Deployment

The observer installers install only `observe-plan`. On Linux, `scripts/install-fleet-systemd.sh CONFIG INSTANCE` is the explicit production transition: it requires an `apply` configuration with safe automatic proof refresh, runs the full no-inference test suite, executes one observe-only cycle, calibrates every enabled route, disables the observer service, and starts a supervised `idol-fleet-INSTANCE.service` loop. Separate IDOL and LIVE instances use disjoint route capacity and private state so one long bounded run cannot starve the other repository.

Both Linux installers resolve a systemd-version-specific recovery policy before changing unit files or service state and write it as a separate `40-restart-backoff.conf` drop-in. On systemd 254 and newer, a failed service retries after 30 seconds and increases its delay over six steps to a 15-minute ceiling. Older releases use a portable fixed five-minute delay. Start rate limiting is disabled so repeated failures do not permanently latch the service; invalid version output refuses installation before mutation. This policy applies only after the service process exits: an explicit `systemctl stop` remains stopped, and later mode drop-ins such as `50-calibration-hold.conf` remain authoritative.

Apply-mode calibration may refresh automatically only when `auto_calibrate` is true. Refresh runs billing/auth and containment probes with `IDOL_FLEET_NO_MODEL_INFERENCE=1`; it cannot invoke a model, buy credits, redeem resets, or enable a route absent from the configuration. A changed configuration or controller version invalidates the prior proof.

Every enabled route must list its configured regular-file proof subjects in `proof_subject_files`. Include the proof adapter and the provider config, auth store, environment file, or manifest that can affect its billing/auth conclusion. Entries are mandatory absolute paths after `~` expansion; missing, unreadable, non-regular, or final-component symlink entries fail closed. Bind a symlink's resolved regular target explicitly when it is part of the proof. Calibration stores only canonical path and fingerprint metadata: file type, device/inode, mode, uid/gid, timestamps, size, and SHA-256. It reads each file through one nonblocking, no-follow descriptor before and after the existing proof command, rejects observed mutation, rechecks the fingerprint while planning, and checks it again before claims or model launch.

If one proof fails, independently current routes remain eligible. Failed routes retry after 5 minutes, 15 minutes, 1 hour, then 6 hours; deferred cycles continue observing and planning without repeating refusal events. If every proof fails, apply completes with all routes disabled and performs no claim or model action. Recovery is journaled when a fresh proof succeeds. This is an operational filesystem-drift detector, not an attestation against malicious root-level ABA replacement or unlisted transitive dependencies.

Use `idol-fleet --config ... record-outcome --receipt ...` to append an independently reviewed terminal outcome. Ready attempts remain held for review, and all non-runtime refusals remain held for operator diagnosis. Only a bounded runtime refusal is eligible for automatic provider fallback.

The bootstrap adapter is temporary. Its deletion witness is a LIVE runtime capable of observing the same facts, producing the same proposals, enforcing the same claims and route proofs, and emitting the same append-only attempt history without Python owning collaboration semantics.
