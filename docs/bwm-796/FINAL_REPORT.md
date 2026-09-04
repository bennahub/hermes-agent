# BWM-796 — Hosted activation final report

## VERDICT

**REAL_CANARY_READY** — hosted synthetic acceptance is complete.
Real-account canary is `OWNER_PARTICIPATION_REQUIRED`.

Parking:

```
BWM796_HOSTED_SYNTHETIC_ACCEPTED
REAL_CANARY_READY
WAITING_FOR_OWNER
```

Do not mark Jira Done until the Owner either completes the canary or
explicitly classifies it as a separate external/provider gate.

## SHAS

| Role | SHA |
|---|---|
| Initial merged implementation | `620b7ef59c237f43f0f232f7d25b2bb51263505f` |
| Current `bennahub/main` at activation start | `620b7ef59c237f43f0f232f7d25b2bb51263505f` |
| VPS git HEAD (unrelated live branch) | `b2bc82cdb44a685eb1ec3c7850e6b09256347bcc` |
| Deployed product | Overlay of `620b7ef` + hosted-activation repairs (upload session, attach, UI, audit truncate) |
| Repair branch | `grok/bwm-796-hosted-activation` (must merge to `bennahub/main` so VPS overlay is not the source of truth) |

## VPS

- service: `hermes-serve` (9119), `hermes-gateway` (8644)
- runtime user: `hermes`
- HERMES_HOME: `/home/hermes/.hermes`
- storage: `/home/hermes/.hermes/agent-computers` (0700)
- Chromium: Playwright chromium-1234, `--no-sandbox` on this AppArmor host
- public ports: Traefik 80/443 only for Hermes HTTP; no public CDP
- config: `agent_computer.runtime: chromium`

## DEPLOYMENT

- baseline snapshot: `/home/hermes/backups/bwm796-pre-deploy-20260904T161011Z/`
- inert deploy: PASS (memory-safe land, then chromium key, no auto-start)
- Chromium activation: PASS
- rollback readiness: snapshot present; persistent data not deleted on rollback

## HOSTED AGENT COMPUTER

- durable id: `ac_ff6aad74fb914d86a30c0d58d20f430d` (profile `bwm796-synth`)
- BrowserIdentity: `bi_7f2d9fe9c1ad4af3b9c0d70c7f2f6258`
- Chromium: one active runtime, loopback CDP
- sleep/wake: Chrome gone on sleep; wake 0.45–0.70 s
- restart recovery: same computer + identity after `hermes-serve` restart
- cookie/localStorage: `ready cookie persist` after restart + navigate

## HUMAN TAKEOVER

- owner client: dashboard `GET /computer`
- screenshot / viewport: PASS
- pixel input: PASS (`owner-pixel-clicked`)
- text: PASS (`synthetic-human-796` harness; `owner-ui-796` owner UI)
- scroll/key: scroll PASS; key supported in contract
- same runtime: `same_environment: true`
- stale agent / stale owner: `STALE_CONTROLLER`
- disconnect: PASS
- Give Back: PASS; UI “Bwm796 Synth resumed”
- resume exactly once: observe-required then one resume path

## FILES

- download → `bwm796-artifact.txt` via artifacts API
- upload → site `received:bwm796-upload.txt`
- boundary: path escape 404; basename-only upload

## MULTI_AGENT

- 14 durable `bwm796-agent-01`…`14` computers
- active concurrency tested: 1 / 2 / 4
- foreign profile cannot attach synth identity (403)
- shared identity contention: `BROWSER_IDENTITY_BUSY`
- lease cannot drive another computer

## CAPACITY

See `RESOURCE_CAPACITY_REPORT.md`. Safe default: **2 active** Chromiums;
**4** proven; **14 always-on rejected**.

## SECURITY

Owner auth required. `server-internal` is not owner. No public CDP. Audit has
no password / cookie / profile path / takeover token. New lease ids truncated.

## REGRESSION

- `/api/health` ok, `auth_required: true`
- `/api/sessions` 200 after owner login
- gateway still active
- ordinary conversations do not auto-start AgentComputer
- dashboard remains `--skip-build` (no SPA rebuild; Mobile/Mac not touched)
- pre-existing Codex 429s on unrelated desks were not treated as this ticket

## INDEPENDENT REVIEW

- Reviewer: [independent review](b159a09b-82c6-4f99-9cb9-ed3edf520bce)
- Initial verdict: **PASS_WITH_FINDINGS** — P0=0, P1=3
- P1s closed in this same ticket:
  1. Land hosted repairs on `bennahub/main` and redeploy from that SHA (not a kitchen overlay).
  2. Remove `30-bwm796-tmp-auth.conf` after UAT.
  3. Truncate historical full `ls_` lease ids in `audit`; restore harness check that new/full ids are absent.
- Re-review target after those closes: P0=0 / P1=0 required for Done.

## REAL CANARY

See `# REAL_CANARY_READY` in the supervisor report. Stopped. No stored Mac
credentials. No Apple/Google/Meta/financial sign-in performed.

## JIRA

BWM-796 remains **In Progress** until Owner canary decision.
