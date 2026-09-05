# Remote development

Use one supervised controller and separate authority clones for IDOL and LIVE.
Tailscale provides the private operator path. Cloudflare Tunnel and Access can
provide an authenticated alternate SSH path. Neither transport reconciles
concurrent writers: exact-SHA orders, repository claims, controller leases, and
independent admission remain mandatory. See [FLEET_CONTROLLER.md](FLEET_CONTROLLER.md).

## Persistent terminal

Install `tmux` on the development host and copy `scripts/idol-dev` to a directory
on the operator's PATH. Configure these two SSH aliases locally:

```sshconfig
Host idol-dev
  HostName <development-node>.<tailnet>.ts.net
  User <development-user>
  StrictHostKeyChecking yes
  ServerAliveInterval 15
  ServerAliveCountMax 3
  ControlMaster auto
  ControlPersist 60
  ControlPath ~/.ssh/idol-%C

Host idol-dev-cf
  HostName <protected-ssh-hostname>
  User <development-user>
  ProxyCommand cloudflared access ssh --hostname %h
  HostKeyAlias idol-development-origin
  UserKnownHostsFile ~/.ssh/idol-development-known-hosts
  StrictHostKeyChecking yes
  ServerAliveInterval 15
  ServerAliveCountMax 3
  ControlMaster auto
  ControlPersist 60
  ControlPath ~/.ssh/idol-%C
```

Install verified host keys before connecting. The OpenSSH origin key used by
Cloudflare may differ from the Tailscale SSH server key. Obtain it through an
already authenticated administrator channel and pin it under the configured
HostKeyAlias. Do not bypass host-key verification to make the fallback connect.
Keep SSH configuration and known-host files private. Put an alias-specific
Include before broad defaults because SSH uses the first value for each option.

Run `idol-dev` to create or resume the same remote terminal; use
`idol-dev --cloudflare` for the alternate path. Detach with the tmux detach key
sequence, or disconnect SSH. The session remains on the host. A host reboot still
ends terminal processes; continuous jobs belong in supervised services.

Cloudflare Access authentication and OpenSSH authentication are separate checks.
Use an existing narrowly scoped Access policy and an authorized SSH key. Where
cloudflared connects to loopback, the origin key can be restricted to loopback
source addresses. Never place Access secrets in ProxyCommand arguments, commit
them, or copy private SSH keys between devices. Keep public SSH reachable only
through the intended private network or authenticated tunnel.

## Verify operation

Validate private SSH, Access authentication, the origin SSH login, and terminal
reattachment separately, then test the complete fallback path. A tunnel's healthy
status does not establish that its origin or login policy works.

Enable the existing controller services and user lingering on Linux. Verify
provider proofs, queue/claim state, and actual completed cycles; an active service
is not evidence of accepted work. Retain bounded persistent service journals so
a reboot does not erase its cause. Periodic health observation can run without
model calls; unchanged state should not create repeated notifications.

For macOS GUI Tailscale installations, availability begins after login. Do not
promise pre-login availability or automatic recovery from a FileVault unlock,
power failure, or missing network based on a login agent alone. Keep an always-on
supervised host as the development authority.

## References

- [Tailscale connection types](https://tailscale.com/docs/reference/connection-types)
- [Tailscale macOS variants](https://tailscale.com/docs/concepts/macos-variants)
- [Cloudflare Access SSH](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/non-http/cloudflared-authentication/)
- [Cloudflare Tunnel availability](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/)
