# AUTONOMY_STATE_CONTRACT

Per-profile file: `$HERMES_HOME/autonomy/work.db`.

Work states used: `working`, `waiting`, `needs_owner`, `completed`, `dropped` (plus optional `observed` / `investigating` / `actionable`).

Product-facing projection (not extra tables):

| Internal | Owner language |
|---|---|
| no open work | Available / quiet |
| observation tick, nothing material | silent |
| working / investigating | short “is investigating …” if delivered |
| waiting | stays in work row; not Owner noise |
| needs_owner | “needs you — …” written into the canonical Bot Chat and marked unread. Replay of the same work id does not duplicate. The writer does not take the live-owner lock. |
| completed + material | “resolved …” |
| dropped | silent |

Idle is not “incapable of initiative.” Cooldown only suppresses empty observation.

Completed / dropped rows do not reopen on the same idempotency key.
