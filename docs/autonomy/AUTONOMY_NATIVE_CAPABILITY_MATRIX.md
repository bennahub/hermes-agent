# AUTONOMY_NATIVE_CAPABILITY_MATRIX

| Need | Native primitive | Gap | V1 treatment |
|---|---|---|---|
| Standing Mission | SOUL.md | No explicit charter | `autonomy/mission.md` + SOUL section |
| Observation wake | cron job | Jobs are reporter-shaped | `autonomy-observe` job |
| Event wake | webhook platform | No initiative contract / dup claim | webhook script `autonomy_webhook_filter.py` |
| Cost control | monitor_script | Unused on current jobs | fingerprint + cooldown |
| Finite work | todo tool | In-memory | `autonomy/work.db` |
| Idempotency | cron fire claim, webhook filters | No work-level key | `claims` table |
| Collaboration | message_agent / hermes -p | Cron may lack message_agent | CLI `autonomy delegate` |
| Loop safety | none | Ping-pong / fanout | collab table |
| Quiet UX | [SILENT] | — | reuse |
| Needs You | Bot Chat message | No second inbox | `needs_owner` + owner_line |
| Computer | computer_use | Deploy availability | reuse; record if missing |
| Restart wake | cron ticker | Work lost | durable rows |
| Quota | provider pins | Codex 429 live | do not pin Codex |
