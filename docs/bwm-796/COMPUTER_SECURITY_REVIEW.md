# BWM-796 — Hosted computer security review

Live probes on `srv1945447` / dashboard
`https://hermes-agent-y9zo.srv1945447.hstgr.cloud`. Synthetic computers only.

## Public exposure

| Probe | Result |
|---|---|
| New public control port | None |
| Chromium / DevTools on `0.0.0.0` | Absent (`ss -lntp`) |
| CDP bind | `127.0.0.1` only, port 0 (ephemeral) |
| Synthetic site | `127.0.0.1:8765` only |
| Dashboard | Traefik 80/443 → loopback 9119 |
| Unauthenticated `GET /api/agent-computers` | 401 (loopback and public) |
| Unauthenticated `GET /computer` | 302 `/login` |

## Authentication / principals

| Principal | Result |
|---|---|
| Real dashboard owner (basic auth cookie) | Can list/wake/takeover/act as owner |
| Unauthenticated client | 401 |
| `server-internal` | `authorize_read` / `authorize_owner` → `ForbiddenError` |
| Foreign agent `bwm796-agent-02` on synth computer | Forbidden |
| Empty principal | Forbidden |
| REST `act` as owner using an agent lease | 403 (agent lease required) — correct fencing |

`localhost` is not treated as owner. `OWNER_PRINCIPAL = "owner"` after
`_require_token`.

## IDOR / identity

| Probe | Result |
|---|---|
| Attach synth identity to `bwm796-agent-02` | 403 profile not authorized |
| Shared identity owned by agent-01 + agent-02, attach to 01 then 02 | 409 `BROWSER_IDENTITY_BUSY` |
| Agent lease from synth used on another computer | 409 / rejected |
| Artifact `..%2Fetc%2Fpasswd` | 404 |
| Upload filename `../x` | rejected (basename-only) |

## Replay / fencing

| Probe | Result |
|---|---|
| Stale agent during owner control | `STALE_CONTROLLER` |
| Takeover token reuse | 403 |
| Owner lease after disconnect | `STALE_CONTROLLER` |
| Owner lease after Give Back | `STALE_CONTROLLER` |
| Agent act after Give Back without observe | `OBSERVE_REQUIRED` |

## Secrets / audit

Audit table `audit.detail_json` (private 0700 sqlite):

- Password absent
- `profile_ref` / `user-data-dir` / `Cookie` absent
- Takeover token absent
- Typed text / cookie values redacted by `_audit`
- New `lease_id` values truncated to 11 characters + `…` (fencing forensics
  without a full bearer)

Public observe/list payloads omit CDP URLs, PIDs, profile paths.

## Storage

`/home/hermes/.hermes/agent-computers` mode `0700`, hermes-owned, not
world-readable, not `/tmp`.

## Residual operational note

A temporary systemd drop-in overrode dashboard basic-auth for UAT. It is
**not** a public control endpoint. Remove it after UAT so only the Owner’s
rotated hash remains.
