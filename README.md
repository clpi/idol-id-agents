# idol-id-agents

Portable agent setup for the Idol platform Pis (r8a / r16 / r8b) and Mac mini.

## Contents
- `scripts/bootstrap.sh` — one-liner to install all agents on a fresh Pi/Mac
- `config/.shared-env.template` — env template (copy to `~/.shared-env` and fill in keys)
- `config/agents/` — per-agent config snippets

## Bootstrap
```bash
curl -fsSL https://raw.githubusercontent.com/clpi/idol-id-agents/main/scripts/bootstrap.sh | bash
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
