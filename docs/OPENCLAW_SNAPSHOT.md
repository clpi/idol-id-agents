# OpenClaw execution snapshot

The fleet's saved-session inventory cannot prove that OpenClaw is idle: cron work can remain active after a job is removed, and queued or settling work is not a saved session. `plugins/openclaw-fleet-snapshot/` supplies one authenticated read-only method from OpenClaw's existing canonical execution aggregate. It adds no counters, job records, scheduler, background task, or model calls.

## Compatibility boundary

This is a temporary unsupported private-API bridge for exactly `2026.8.1-beta.3`. Its private package declares no installable dependencies: OpenClaw's existing loader supplies the public SDK, and the extension validates the installed host directly. It does not fetch or install OpenClaw. The producer pins these installed module bytes:

| Module under the OpenClaw package | SHA-256 |
| --- | --- |
| `dist/gateway-active-work-DHoQuaTC.js` | `f65a34729ee974c0c4e41e1e2ca7a504aa0bf626b584a11bae5fe66f00e379a7` |
| `dist/server-active-work-DvXAfFIM.js` | `8f266b5bde14c43be5d1c1cd426fcff13f2280f505b53775a3152f127b633e9d` |

A matching version string alone is insufficient. An upgrade requires a fresh source and handler-context review, new pins, focused tests, an authenticated live observation, and renewed controller calibration. Do not discover replacement hashed aliases automatically or weaken a refusal to an idle result. These checks detect configured package drift; they are not a complete transitive dependency or malicious-root attestation.

## Contract

The only method is `idol.fleet.activeWork.snapshot`, accepting empty parameters. Registration preserves the SDK's default `operator.admin` scope and required authenticated profile. It does not introduce a public HTTP route or bypass gateway authentication.

The response contains only `schema: "idol.openclaw.active-work"`, integer `version: 1`, `openclawVersion`, UTC millisecond `observedAt`, boolean `idle`, and `counts`. The component counters are `queueSize`, `pendingReplies`, `embeddedRuns`, `backgroundExecSessions`, `cronRuns`, `activeTasks`, `rootRequests`, `sessionAdmissions`, `sessionMutations`, `chatRuns`, `queuedTurns`, `terminalPersistence`, and `terminalSessions`. `totalActive` is their sum. Every value must be a nonnegative JavaScript safe integer. The counters overlap; their sum is an activity indicator, not a count of distinct jobs. The current snapshot request is excluded by OpenClaw's existing request context.

The Python inventory consumer requires a complete, consistent, fresh response. Any activity refuses additional admission. A valid idle response permits the existing local process inventory to be emitted, subject to the controller's other admission checks. Missing methods, errors, package drift, unknown fields, missing counters, stale timestamps, and inconsistent totals all refuse. It makes one bounded gateway call with explicit `--port 18789`, which selects loopback even if an environment URL is set, and brackets the call with the local listening process identity. Credentials remain in OpenClaw's protected config/environment handling. It never falls back to saved session pagination.

The observation is synchronous within one gateway event-loop turn. It neither closes admissions nor reserves capacity; new interactive work may start after it returns. Claims and controller admission remain necessary. No raw request context, session identity, blocker detail, prompt, tool output, or internal exception is returned.

## Activation requirements

Keep both fleet services in `observe-plan` while reviewing and activating this extension. The live gateway owner must coordinate installation, configuration, and any reload with ongoing provider work. Copy only the reviewed `index.js`, `snapshot.js`, `openclaw.plugin.json`, and `package.json` into a stable private directory. Configure that directory through `plugins.load.paths`, add `openclaw-fleet-snapshot` to the existing allowlist where one is configured, and enable its `plugins.entries` record with `config.openclawPackageRoot` set to the actual installed gateway package. Preserve all existing plugin entries and allowlist members. The package names only `index.js` as its extension entry; test files are not deployed. Do not replace the gateway configuration wholesale or change provider/auth settings as part of this installation.

Before removing any observation hold, verify local gateway mode and port 18789, the loaded package binding, default authenticated access, a complete idle observation when no work exists, and a blocking observation for controlled existing activity. Keep the initial response as private metadata-only evidence. Re-run the full inventory in the service environment, retain all fixed-cost route proofs and allowance requirements, and recalibrate the changed controller version. A fixture test or successfully registered method alone does not establish live execution coverage.

If activation fails or the package changes, disable this extension through the existing gateway configuration and retain observation mode. The consumer's refusal is intentional. There is no automatic downgrade to the earlier incomplete inventory path.

## Deletion witness

Delete the private bridge once OpenClaw exposes a supported, authenticated, coherent read-only execution aggregate covering the same queued, active, settling, persistence, and watcher work, with an equivalent fail-closed contract. The fleet consumer should then use that supported method. LIVE remains the owner of collaboration truth; this plugin does not become a second control plane.
