# r16 health probe

This package is a read-only observer for the r16 IDOL/LIVE host. It performs no repairs, model calls, dispatch, remote mutation, or credential discovery. It stores only health metadata. Controller validation reads configured proof files to compute fingerprints; their contents, credentials, journal facts, prompts, and model histories are never written to health state.

## Health contract

A healthy result requires all of these independent facts:

- system `tailscaled.service` is active;
- obsolete system `cloudflared.service` is disabled or masked and inactive;
- user services `idol-fleet-idol`, `idol-fleet-live`, `r16-tunnel`, and `r16-legacy-secure` are enabled and active;
- the Mac mini is present and online in `tailscale status --json`;
- `~/.config/idol-hermes/role` is `active`, or it is `standby` and the primary-heartbeat file mtime is less than 55 seconds old;
- both controller configurations are explicitly bound, their effective systemd command uses `apply` mode, and the installed controller accepts every enabled route's trusted, unexpired, file-fingerprinted proof;
- the last complete IDOL and LIVE journal event fits within a 64 KiB tail window and its own hash is valid; and
- fixed HTTPS probes pass with normal TLS verification: `idol.id` and `live.idol.id` return 200, each Hermes `/health` returns 200 or its root returns 302, and each Claw root returns 200.

A timeout from `live.idol.id` is reported as `private_live_timeout`, so an intentionally private LIVE route is distinguishable from an unexpected response. It remains an unhealthy public check until the deployment contract explicitly changes.

An `active` role means r16 is the local primary; a stale or absent former-primary heartbeat does not fail that role. Public origin checks remain independently required. The heartbeat's plain epoch value is reported only as age metadata; standby freshness follows the existing failover contract and uses file mtime under 55 seconds.

Calibration is independent of service state. The observer calls the installed `fleet_control` config loader, calibration loader, file-fingerprint binding, and route policy without constructing or running a controller. Stale, untrusted, expired, mutated, or unbound proofs cannot be healthy. The effective mode is extracted from systemd without storing its command arguments. An `observe-plan` override is reported as `observation_only` even when the configuration says `apply`. `calibration_ready` means at least one enabled route has a valid file-bound proof; `all_routes_healthy` separately requires every enabled route. Neither proves dispatch readiness: live quota, claims, work orders, and admission checks remain controller responsibilities.

## State and bounds

The probe writes only to `~/.local/state/idol-health-probe` by default:

- `current.json` is atomically replaced on every observation and always mode 0600;
- `transitions.jsonl` receives a row only when stable health fields change;
- `transitions.jsonl` rotates to one mode-0600 `.1` file at 1 MiB;
- `health-probe.lock` serializes overlapping invocations and is mode 0600.

The state directory is mode 0700. Transition identity excludes observation times, heartbeat ages, journal sequence numbers, process IDs, and other volatile values. JSON reads stop at 1 MiB plus one sentinel byte. Journal reads use at most 64 KiB from the end of each real `fleet-history.jsonl` and validate the last row with the controller's self-hash encoding. The controller's own gate remains responsible for full-chain verification.

Each HTTPS request uses a curl subprocess with a maximum transfer time of 12 seconds. The eight fixed requests run concurrently, so an unavailable endpoint does not serialize the entire observation.

## Validate

Run the stdlib tests with the same Python 3.10+ interpreter used by the controller:

```sh
python3 -m unittest discover -s tests -v
```

Python 3.10 or newer is required because the observer deliberately uses the installed controller's native validation code.

For a local dry run with a disposable state directory:

```sh
IDOL_HEALTH_STATE_DIR=/tmp/idol-health-probe-check python3 scripts/fleet_health_probe.py
```

The command exits 0 only for an overall healthy observation. Exit 1 means `current.json` contains one or more explicit health reasons. No repair is attempted.

## Install on r16

Review the actual controller config paths first. Then install the observer and unit templates:

```sh
install -d -m 0700 "$HOME/.local/lib/idol-health-probe" "$HOME/.local/state/idol-health-probe" "$HOME/.config/systemd/user" "$HOME/.config"
install -m 0500 scripts/fleet_health_probe.py "$HOME/.local/lib/idol-health-probe/health_probe.py"
install -m 0600 config/systemd/idol-health-probe.service "$HOME/.config/systemd/user/idol-health-probe.service"
install -m 0600 config/systemd/idol-health-probe.timer "$HOME/.config/systemd/user/idol-health-probe.timer"
install -m 0600 config/idol-health-probe.env.example "$HOME/.config/idol-health-probe.env"
```

Edit `~/.config/idol-health-probe.env` to bind `PYTHONPATH` to the installed controller root, bind both exact fleet config files, and set the exact Mac mini Tailscale hostname. Leaving either config placeholder unset produces `unbound`, never healthy. The environment file contains paths and policy values only; do not put credentials in it. Validate and start the timer:

```sh
systemd-analyze --user verify "$HOME/.config/systemd/user/idol-health-probe.service" "$HOME/.config/systemd/user/idol-health-probe.timer"
systemctl --user daemon-reload
systemctl --user start idol-health-probe.service
systemctl --user enable --now idol-health-probe.timer
systemctl --user list-timers idol-health-probe.timer --no-pager
```

Inspect the compact result without dumping Tailscale peers, route proof bodies, or journal facts:

```sh
python3 -m json.tool "$HOME/.local/state/idol-health-probe/current.json"
```

The systemd service is deliberately a one-shot reader. The timer is the sole schedule; this package does not create another controller or dispatch plane.
