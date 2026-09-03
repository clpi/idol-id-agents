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
- **Billing is witnessed, not inferred.** The word `included` is insufficient. A route needs an unexpired local calibration proof tied to the exact route configuration.
- **Observe-plan is the installation default.** It may inspect local state and write private controller state. It cannot invoke a model, acquire claims, create worktrees, edit repositories, push branches, or create pull requests.
- **Apply is hash-bound.** Apply requires a current calibration record covering the controller version, configuration hash, route identity, billing proof, stale-SHA refusal, claim overlap, outside-path refusal, bounded process termination, and no-pay-go controls.
- **One attempt, exact subject.** A work order names one task, exact base SHA, exact path claims, semantic claims, required outcome, stop conditions, witnesses, role, route set, and expected review family.
- **Two claim layers.** Every repository path enters the controller’s private hierarchical path store; IDOL additionally uses its repository-owned positional `tools/node/dev/claim` authority. Semantic boundaries use a separate private hierarchical store. Any conflict blocks dispatch. A repository without an executable claim adapter, such as today’s early LIVE source tree, must opt into the private path store explicitly with `repository_claim_required: false`.
- **No shell-shaped work orders.** Runtime commands and witness commands are argument vectors. The controller never evaluates work-order text through a shell.
- **Containment is measured after execution.** Any edit outside the claimed paths holds the attempt and preserves the worktree. A successful process is not successful work.
- **Evidence precedes review; review precedes admission.** The controller can create a draft branch/PR handoff, but never merges or admits its own attempt.
- **Uncertainty preserves evidence.** Failed, stale, conflicted, timed-out, or ambiguous attempts retain their worktrees and journal entries. Cleanup is an explicit later operation.
- **No automatic reset redemption or top-up.** Banked resets, extra usage, credits, and purchases require a separate human-authorized operation outside the controller.

## Control loop

Each cycle:

1. acquire the single-controller lease;
2. observe repository SHA, dirty state, claims, queued work orders, route proofs, and allowance snapshots without model inference;
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

## Deployment

The observer installers install only `observe-plan`. On Linux, `scripts/install-fleet-systemd.sh CONFIG INSTANCE` is the explicit production transition: it requires an `apply` configuration with safe automatic proof refresh, runs the full no-inference test suite, executes one observe-only cycle, calibrates every enabled route, disables the observer service, and starts a supervised `idol-fleet-INSTANCE.service` loop. Separate IDOL and LIVE instances use disjoint route capacity and private state so one long bounded run cannot starve the other repository.

Apply-mode calibration may refresh automatically only when `auto_calibrate` is true. Refresh runs billing/auth and containment probes with `IDOL_FLEET_NO_MODEL_INFERENCE=1`; it cannot invoke a model, buy credits, redeem resets, or enable a route absent from the configuration. A changed configuration or controller version invalidates the prior proof.

Use `idol-fleet --config ... record-outcome --receipt ...` to append an independently reviewed terminal outcome. Ready attempts remain held for review, and all non-runtime refusals remain held for operator diagnosis. Only a bounded runtime refusal is eligible for automatic provider fallback.

The bootstrap adapter is temporary. Its deletion witness is a LIVE runtime capable of observing the same facts, producing the same proposals, enforcing the same claims and route proofs, and emitting the same append-only attempt history without Python owning collaboration semantics.
