# BWM-796 — Hosted Human Takeover UAT

Synthetic only. No real account. Owner surface is the existing authenticated
dashboard page `GET /computer` (not a new app, not Mobile, not Hermes for Mac).

## Topology proven

```
Owner browser (Mac)
  → HTTPS Traefik
  → hermes-serve 127.0.0.1:9119
  → AgentComputer / ControlLease
  → same hosted Chromium (loopback CDP)
```

No raw CDP, no SSH as the human-control path, no public debug port.

## Harness (REST + AgentDriver)

`docs/bwm-796/hosted_acceptance.py` against `http://127.0.0.1:9119` as user
`hermes`, real `/auth/password-login` cookies.

PASS:

1. Wake hosted Chromium (~694 ms first wake; ~459 ms after service restart).
2. Agent navigates `http://127.0.0.1:8765/` (loopback synthetic site).
3. Control `AGENT_CONTROLLED`; agent click `#agent-ready`.
4. Owner takeover → `OWNER_CONTROLLED`; takeover token single-use (403 replay).
5. Stale agent act → `STALE_CONTROLLER`.
6. Owner pixel click → `owner-pixel-clicked`.
7. Owner type → `synthetic-human-796` visible to later agent observe.
8. Owner scroll; screenshot present; `live_view.same_environment == true`.
9. Owner disconnect → stale owner `STALE_CONTROLLER`.
10. Second takeover; Give Back; `resume_observe_required`.
11. Agent act without observe → `OBSERVE_REQUIRED` (no pre-takeover replay).
12. Agent re-observe sees owner text; resumes once via later download click.
13. Checkpoint `payment` → `CHECKPOINT_REQUIRED`; approve; exactly one
    consequential act; second payment blocked; ordinary click does not
    checkpoint.

## Owner client surface (production page)

`https://hermes-agent-y9zo.srv1945447.hstgr.cloud/computer`

Human copy present: needs you / Take Control / You have control /
Give Control Back / resumed.

Unauthenticated `/computer` redirects to `/login?next=/computer`.

Live browser UAT (2026-09-04):

| Step | Result |
|---|---|
| Login (real dashboard basic auth) | PASS |
| List includes `bwm796-synth` | PASS |
| Live screenshot of synthetic page | PASS (`BWM-796 Synthetic · http://127.0.0.1:8765/`, `ready cookie persist`) |
| Take Control | Headline **You have control** |
| Pixel click on screenshot | Page status `owner-pixel-clicked` |
| Type `owner-ui-796` | Status and `#text-input` show `owner-ui-796` |
| Scroll down | Control accepted |
| Give Control Back | Headline **Bwm796 Synth resumed** |

UI auto-wakes the selected computer so a sleeping workspace is not a blank
`about:blank` dead-end. Type without a selector focuses the first text input
when Chromium focus is on `body`.

## Disconnect / reconnect

Harness owner-disconnect while `OWNER_CONTROLLED`:

- stale owner lease rejected
- no second active owner
- second takeover reconnects
- Give Back then stale owner rejected again
- agent cannot resume until observe

## Same runtime

Observe payloads include `same_environment: true` and never include CDP URLs,
`profile_ref`, or `user-data-dir`.
