# AUTONOMY_RECON

Canonical issue: [BWM-802](https://iaalmuzaini.atlassian.net/browse/BWM-802)

Proven 2026-09-04 against live VPS `srv1945447` (`hermes-vps`) and this worktree.

## Concurrent work

| Ticket | Status | Implication |
|---|---|---|
| BWM-796 Persistent Computers | Done | Reuse Computer; do not rebuild |
| BWM-797 Asera Mobile | In Progress | Do not edit shared tui_gateway / web_server / unread contracts |
| BWM-794 Hermes for Mac | To Do | Expose reusable Hermes-side contracts only |
| BWM-798 Owner Authority | Done | Leave dirty VPS tree alone |

VPS running process: `hermes-gateway` + `hermes-serve`, `NRestarts=0`, code_sha `b2bc82cdb44a685eb1ec3c7850e6b09256347bcc` (`fix/live-bot-dm-session-owner`). Working tree on the VPS is dirty with BWM-797 leftovers — autonomy ships as additive files only.

## Roster (live)

`default`, `abu-saleh`, `abu-saud`, `badr`, `fahad`, `faisal`, `fares`, `hamad`, `joud`, `majed`, `mishari`, `nasser`, `nawaf`, `rashid`, `sami`, `turki`.

Pilots exist: Abu Saud, Badr, Sami, Nasser.

## Native answers (short)

1. Scheduler: `cron/scheduler.py` in-gateway ticker, per-profile `jobs.json`.
2. Routines: cron jobs with prompt/script/monitor_script, `deliver: bot-chat:<profile>`.
3. Todo: in-memory per session — not restart-safe. Kanban is a heavier board, not used.
4. Events: native webhook platform + plugin event bus (not a general work bus).
5. Activation without Owner chat: cron, webhook, `message_agent` / `hermes -p chat`, delegation, internal events.
6. Turn sources: CLI, gateway platforms, cron, webhook, DM, delegate_task, internal completion.
7. Same runtime: yes — they construct `AIAgent` / `run_conversation`.
8. Dedup: webhook event filters, LINE id LRU, cron fire claims, session live-owner exclusivity.
9. IDs: cron job id, execution rows, webhook delivery ids, session ids, delegation ids.
10. Survives restart: jobs.json, notepad.db, executions.db, SessionDB, SOUL, config. Todo does not.
11. A2A: `message_agent` (Bot Chat only) and `hermes -p <bot> chat --in ~ -c "Bot Chat"`.
12. Delegation: `delegate_task` (leaf/orchestrator). Not required for V1 A2A.
13. Groups: hosted rooms (BWM-797 surface — do not fork).
14. Profile config: `config.yaml`, `SOUL.md`, profile meta.
15. Identity: SOUL.md per profile + Bot Chat title.
16. Work state: todo cannot represent durable autonomous work.
17. Home/Activity/Needs You: not a Hermes-core inbox. Quiet UX = `[SILENT]` + Bot Chat.
18. Hidden vs visible: `[SILENT]` suppresses delivery; cron output still stored.
19. Unread: existing session unread. Do not add a second inbox (BWM-797).
20. Computer: `computer_use` toolset on the same profile. BWM-796 not rebuilt.
21. Quota: VPS Codex 429 on `badr-engineering-event-desk`. Observation jobs must not pin Codex.
22. Duplicate turns: session live-owner lock; cron fire claims.
23. Wake after restart: cron ticker + webhook routes + durable work rows.
24. Shared Mobile/Mac APIs: `hermes serve` / dashboard JSON-RPC. Autonomy does not add endpoints.
25. Genuine gaps: Standing Mission as an explicit charter; initiative cycle without Owner-authored routines; restart-safe finite work + idempotency; collaboration loop guards.

## Existing routines (not initiative)

Profile cron jobs today are mostly *report if something finished* or *read-health*. Many are paused. Badr’s reporter last failed on Codex quota and Bot Chat live-owner lock. These are not Standing Missions.
