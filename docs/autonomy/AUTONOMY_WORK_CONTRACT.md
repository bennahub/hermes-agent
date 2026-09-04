# AUTONOMY_WORK_CONTRACT

A work unit is finite:

- `why` — why this is worth doing now
- `outcome` — concrete outcome
- `done_contract` — evidence sufficient to stop
- `idempotency_key` — stable across event/tick/reconnect
- `objective` — short human line

CLI (no model-tool schema):

```
hermes autonomy work-start --key … --why … --outcome … --done … --objective … [--jira KEY]
hermes autonomy work-update --state waiting|needs_owner --waiting-reason … [--jira KEY]
hermes autonomy work-complete --id … --result …
hermes autonomy work-drop --id … --reason …
hermes autonomy delegate --to <profile> --work <id> --goal … --deliverable … --evidence …
```

Rules:

- Max 3 open work items per profile.
- Already-working blocks a second initiative unless it is the same key.
- Unrelated findings use a new key / optional `--parent`.
- Delegation: no self, no Hamad, no duplicate goal hash, no reverse ping-pong, fan-out cap 3.
- Ordinary tools do not prompt the Owner. `needs_owner` is only for real consequence boundaries.
- Self-initiated ordinary work does not wait for Owner «ابدأ». If tracking is required, create or bind the existing Jira identity and record it with `--jira`.
- `needs_owner` is projected into the canonical Bot Chat unread surface. Replay of the same work id does not duplicate the owner line.
