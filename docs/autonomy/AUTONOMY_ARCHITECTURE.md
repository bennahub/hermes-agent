# AUTONOMY_ARCHITECTURE

Smallest correct architecture: **native cron + native webhooks + SOUL + one SQLite ledger + `hermes autonomy` CLI**.

```
Standing Mission (SOUL + autonomy/mission.md)
        |
        +-- native webhook event ----+
        |                            |
        +-- native cron observe -----+
                                     v
                          Initiative turn (same AIAgent)
                          "Anything worth doing?"
                           |                 |
                          No                Yes
                           |                 |
                     [SILENT] / noop     finite work row
                                             |
                              tools / A2A / computer_use
                                             |
                                        verify → complete
                                        or needs_owner
```

## Classification

| Component | Class | Why |
|---|---|---|
| Cron ticker / jobs.json | NATIVE_REUSE | Observation wake |
| `--monitor-script` hash suppress | NATIVE_REUSE | Cost discipline |
| `--script` context inject | NATIVE_REUSE | Observe context |
| `deliver: bot-chat:<profile>` | NATIVE_REUSE | Owner projection for cron output |
| Bot Chat `state.db` append + unread watermark | NATIVE_REUSE | `needs_owner` without taking the live-owner lock |
| `[SILENT]` | NATIVE_REUSE | Quiet UX |
| Webhook adapter | NATIVE_REUSE | Event wake |
| SOUL.md | NATIVE_REUSE | Identity + mission |
| `hermes -p chat` Bot Chat | NATIVE_REUSE | A2A |
| `computer_use` | NATIVE_REUSE | Computer; BWM-796 not rebuilt |
| Provider routing | NATIVE_REUSE | Avoid Codex pin |
| `autonomy/work.db` | SMALL_EXTENSION | Todo is not durable |
| `hermes autonomy` CLI | SMALL_EXTENSION | No new model tool |
| New orchestrator / event bus / task backend | NOT_NEEDED | |
| New Computer runtime | NOT_NEEDED | BWM-796 |
| New Home/inbox | NOT_NEEDED | Would collide with BWM-797 |
| Memory platform / skills marketplace | DEFERRED | |

## Isolation

Additive modules only: `agent/autonomy/*`, `hermes_cli/autonomy_cmd.py`, `hermes_cli/subcommands/autonomy.py`, two lines in `main.py`. No edits to conversation_loop, turn_finalizer, tui_gateway, web_server, hosted_rooms, unread.

## Why no second platform

Cron already wakes agents without an Owner message. Webhooks already wake on CI/Sentry. Bot Chat is already the product identity. The only proven hole is durable finite work + idempotency + a Standing Mission charter.
