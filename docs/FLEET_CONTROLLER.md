# Idol fleet controller

The fleet controller is the temporary operational adapter for continuous Idol development. Idol Live owns collaboration truth; this repository hosts the deletable bootstrap that observes current tools, proposes work, and—only after calibration—dispatches bounded attempts.

## Authority

The controller never defines Idol semantics. Every work order binds an exact repository SHA and names the authority files it must read. For `clpi/idol`, the default chain is:

1. `docs/spec/law.md`
2. `docs/spec/constitution.md`
3. `docs/bootstrap.md`
4. the exact gap or issue named by the work order
5. current live claims from `tools/node/dev/claim`

Idol Live owns actors, tasks, attempts, claims, routes, allowance observations, witnesses, review, admission, immutable history, and the accepted frontier. Git branches and pull requests are compatibility projections.

## Safety invariants

- **Zero pay-go by default.** Only `local` and trusted `included` routes are eligible. `paygo`, `purchased`, `topup`, and `unknown` routes are rejected before planning or process launch.
- **Billing is witnessed, not inferred.** The word `included` is insufficient. A route needs an unexpired local calibration proof tied to the exact route configuration.
- **Observe-plan is the installation default.** It may inspect local state and write private controller state. It cannot invoke a model, acquire claims, create worktrees, edit repositories, push branches, or create pull requests.
- **Apply is hash-bound.** Apply requires a current calibration record covering the controller version, configuration hash, route identity, billing proof, stale-SHA refusal, claim overlap, outside-path refusal, bounded process termination, and no-pay-go controls.
- **One attempt, exact subject.** A work order names one task, exact base SHA, exact path claims, semantic claims, required outcome, stop conditions, witnesses, role, route set, and expected review family.
- **Two claim layers.** Repository paths use the repository-owned `tools/node/dev/claim`; semantic boundaries use the controller’s private hierarchical claim store. Either conflict blocks dispatch.
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
8. leave acceptance to an independent reviewer and Idol’s normal admission gates.

Unused included allowance is not itself waste. The objective is accepted semantic progress per marginal token. When a reset approaches, the scheduler expands into counterexample search, fixture generation, reduction, differential tests, artifact inspection, and independent review before starting speculative implementation that cannot finish coherently.

## Runtime adapters

The controller uses explicit local commands. The primary supported adapter is `openclaw agent exec`, because it offers isolated execution, exact working-directory placement, stable JSON result/usage metadata, explicit model selection, timeout handling, and no silent gateway-to-local retry. Hermes one-shot routes are supported only when the host calibration proves their provider identity, billing class, result accounting, and containment.

Routes authenticate using either local model execution, a witnessed subscription/OAuth login, or a witnessed fixed token-plan account. API keys and provider balances are never assumed to be included allowance. Provider credentials are not written to work orders, journal records, Git, or Idol Live.

## Deployment

`install-observer` installs only `observe-plan` as a user service. Enabling `apply` is a separate local action requiring a fresh calibration record. The service operates from an existing checkout and stores all mutable state under a user-private state directory.

The bootstrap adapter is temporary. Its deletion witness is an Idol Live runtime capable of observing the same facts, producing the same proposals, enforcing the same claims and route proofs, and emitting the same append-only attempt history without Python owning collaboration semantics.
