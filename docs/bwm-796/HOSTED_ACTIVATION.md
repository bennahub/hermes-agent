# BWM-796 — Hosted activation (operational)

Append-only operational truth after the accepted implementation landed on
`bennahub/main` at `620b7ef59c237f43f0f232f7d25b2bb51263505f`.

This is not a redesign of AgentComputer / BrowserIdentity / ControlLease.

## Repository

| Item | Value |
|---|---|
| Canonical writable | `bennahub/hermes-agent` |
| Merged implementation SHA | `620b7ef59c237f43f0f232f7d25b2bb51263505f` |
| Supervisor worktree | `/Users/abdulrahman/hermes-worktrees/bwm-796-canonical-merge` |
| Work branch | `grok/bwm-796-hosted-activation` (targeted hosted repairs) |
| NousResearch PR #102113 | Optional; not a deploy dependency |

Do not use `~/.hermes/hermes-agent` (dirty detached owner-authority work) or
concurrent Mac/Mobile worktrees for this overlay.

## Hosted runtime (live recon, 2026-09-04)

| Item | Value |
|---|---|
| Host | `srv1945447.hstgr.cloud` (Hostinger KVM2) |
| SSH | `hermes-vps` as `root` |
| Services | `hermes-serve.service` (`127.0.0.1:9119 --skip-build --no-open`), `hermes-gateway.service` (`127.0.0.1:8644`) |
| Runtime user | `hermes` |
| HERMES_HOME | `/home/hermes/.hermes` (0700) |
| Deploy dir | `/home/hermes/.hermes/hermes-agent` |
| DEPLOY_BASE_SHA | `b2bc82cdb44a685eb1ec3c7850e6b09256347bcc` on `fix/live-bot-dm-session-owner` (dirty live tree) |
| Overlay method | Additive rsync of BWM-796 files into the live tree. **Never** `hermes update` or `git checkout 620b7ef` on the VPS. |
| Public HTTP | Traefik `:80` / `:443` → `https://hermes-agent-y9zo.srv1945447.hstgr.cloud` |
| New public ports | None |
| Chromium | Playwright `AGENT_BROWSER_EXECUTABLE_PATH=/home/hermes/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome` |
| AppArmor | `apparmor_restrict_unprivileged_userns=1` → launch adds `--no-sandbox --disable-dev-shm-usage` |
| CDP | `--remote-debugging-address=127.0.0.1 --remote-debugging-port=0` only |

## Rollback

Snapshot: `/home/hermes/backups/bwm796-pre-deploy-20260904T161011Z/` (0700, hermes-owned).

Restores previous code SHA + dirty tree + config + service state. Does **not**
delete `/home/hermes/.hermes/agent-computers`.

## Config

Official `config.yaml` (not a permanent env export):

```yaml
agent_computer:
  runtime: chromium
```

Default implementation remains `memory`. Chromium is opt-in. Ordinary chat does
not auto-start AgentComputer.

## Inert deploy

Passed before Chromium activation:

- `hermes-serve` healthy
- unauthenticated `GET /api/agent-computers` → 401
- unauthenticated `GET /computer` → 302 `/login`
- no Chromium until explicit wake
- no new public port

## Chromium activation

After inert pass, `runtime: chromium` was set. Wake starts one headed-off
Chromium per computer, loopback CDP only. Verified with `ss -lntp`: no public
chrome/devtools listen.

## Persistence

| Path | Role |
|---|---|
| `/home/hermes/.hermes/agent-computers` | 0700 hermes-owned store |
| `state.db` | computers, identities, leases, `audit` |
| `identities/<bi_*>/` | durable Chromium user-data-dir |
| `computers/<profile>/<ac_*>/workspace/` | uploads + downloads |

Not `/tmp`. Survives `systemctl restart hermes-serve`. PID / CDP port / live DOM
are not persisted.

Synthetic proof (same ids after restart):

- computer `ac_ff6aad74fb914d86a30c0d58d20f430d`
- identity `bi_7f2d9fe9c1ad4af3b9c0d70c7f2f6258`
- observe text after restart navigate: `ready cookie persist`

## Temporary dashboard login override

`/etc/systemd/system/hermes-serve.service.d/30-bwm796-tmp-auth.conf` was used
only so hosted UAT could pass the rotated basic-auth hash. It must be removed
after this ticket’s UAT so the Owner’s rotated hash is the only login.

The Sep 1 Mac file `~/.ssh/hermes_vps_dashboard.pw` does **not** match the
rotated hash.

## Deployment record (latest overlay)

| Field | Value |
|---|---|
| SOURCE_SHA | `620b7ef59c237f43f0f232f7d25b2bb51263505f` + hosted-activation repairs on `grok/bwm-796-hosted-activation` |
| DEPLOYED_SHA | Live git HEAD remains `b2bc82cd…`; product files overlaid and `chown hermes` |
| CONFIG_CHANGE | `agent_computer.runtime: chromium` |
| SERVICE_RESTART | `hermes-serve` (gateway left running) |
| HEALTH_RESULT | `GET /api/health` → `{"ok":true,"auth_required":true}` |

VPS-only edits are not the final state. Targeted repairs must land on
`bennahub/main` and be re-overlaid from that SHA.
