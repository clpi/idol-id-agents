# idol-id-agents

Portable agent setup and the fail-closed continuity controller for IDOL and LIVE hosts.

## Contents
- `scripts/bootstrap.sh` — portable prerequisites, Claude Code, Codex, and an observed agent inventory
- `config/.shared-env.template` — env template (copy to `~/.shared-env` and fill in keys)
- `fleet_control/` — quota-aware, claim-safe continuous work controller
- `docs/FLEET_CONTROLLER.md` — controller law, deployment, and deletion witness
- `scripts/discover-fleet-host.py` — no-inference host and provider discovery

## Bootstrap

Run as the account that will use the agents. macOS uses an existing Homebrew
installation for missing prerequisites; Debian/Ubuntu/Raspberry Pi OS uses
`apt-get` with sudo only for system packages. Other Linux distributions work
when `git`, `curl`, `python3`, Node.js 18+, and `npm` are already on `PATH`.

From a checkout:

```bash
bash scripts/bootstrap.sh
```

Or stream the script:

```bash
curl -fsSL https://raw.githubusercontent.com/clpi/idol-id-agents/main/scripts/bootstrap.sh | bash
```

The checkout path uses its sibling environment template. The streamed path
downloads a template pinned to an immutable revision and verifies its SHA-256.
A new `~/.shared-env` has mode `600`; existing files and secret-manager symlinks
are preserved and never sourced or printed. Agent packages install into
`~/.local`, so add `~/.local/bin` to your shell's `PATH`.

The bootstrap installs missing `claude` and `codex` executables using their
official scoped npm packages. Existing agents, including Hermes, are preserved.
Hermes and optional tools such as Grok, OpenCode, Oh My Pi, Poolside, Agy, and
Kilo appear in the final inventory only when their executables are found; missing
tools are explicitly reported. For a missing Hermes installation, use the
[official Hermes installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).
Bootstrap does not authenticate agents, select paid providers, download models,
or start the fleet. Route proofs and allowance admission still apply.

The bootstrap regression tests simulate macOS/Linux commands with an isolated
`PATH`, without installing packages, launching agents, or making network calls:

```bash
python3 -m unittest discover -s tests -p test_bootstrap.py -v
```

## Per-Pi notes
- **r8a** (`r8a.idol.id`): primary graph host, already configured.
- **r16** (`r16.idol.id`): deploy from Mac mini or this script when reachable.
- **r8b** (`r8b.idol.id`): same. If password auth is disabled, enable it temporarily:
  ```bash
  echo 'PasswordAuthentication yes' | sudo tee -a /etc/ssh/sshd_config.d/99-allow-password.conf
  sudo systemctl reload ssh
  ```
- **Mac mini** (`mm.local`): clone this repo under `/Volumes/d 1/idol-id/` and run bootstrap.

## Security
Never commit real API keys. Use `~/.shared-env` locally (gitignored) or a secrets manager.

## Continuous development

The production controller uses exact-SHA work orders, two claim layers, live
session inventory, fixed-cost route proofs, allowance snapshots, per-provider
circuit breakers, and independent admission receipts. It never enables PAYG,
redeems resets, or merges its own attempts. Start with the inert example and
follow [the fleet controller guide](docs/FLEET_CONTROLLER.md).
