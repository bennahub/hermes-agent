# AUTONOMY_EVENT_CONTRACT

## Event-driven

Native webhook payload → `autonomy_webhook_filter.py`:

- Derive a stable key (delivery id, GitHub/Jira id, else SHA of payload).
- `claims(kind=event, key)` insert-or-ignore.
- Duplicate → stdout `[SILENT]` (webhook ignores).
- Fresh → JSON envelope with observe context + initiative prompt; existing route still wakes the profile.

V1 event domains already on the VPS: Hermes webhooks (Badr engineering desk, Sami Sentry). No generic enterprise bus.

## Observation-driven

Native cron `autonomy-observe`:

- `monitor_script` emits a fingerprint (open work + cooldown + resume bucket).
- Unchanged fingerprint → no LLM call.
- Changed / first run → script injects context; agent decides.

Tick claim key: `{profile}:{YYYY-MM-DDTHH}` so scheduler replay in the same hour does not start a second initiative.

## Owner-visible vs hidden

Scheduler ticks, fingerprints, claims, and `[SILENT]` results are not Owner unread. Only Bot Chat deliveries that are not `[SILENT]` are visible.
