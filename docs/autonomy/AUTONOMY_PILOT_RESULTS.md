# AUTONOMY_PILOT_RESULTS

Live on `srv1945447`, 2026-09-04. Gateway PID 888523, `NRestarts=0`, no service restart.

## 1. Self-started useful work (Badr)

`hermes -p badr cron run 8dfc69b92351` succeeded (`51b1618b15c7478aadb3af4a07f49e08`) without an Owner chat message and without an Owner-authored routine for this work.

Badr inspected live GitHub Actions and found six path-filter workflows invalid since BWM-795 commit `4e74785` (`[[]locale[]]` escapes). PR lanes have not fired since 2026-09-02 17:58. Required checks still pass, so he did not treat it as a merge emergency.

He correctly stopped at the existing SOUL gate: do not open a Jira key without «ابدأ».

Work row (persisted after the turn; first observe prompt did not yet force the CLI):

- id `aw_3c713a35c37e49f5`
- key `ci:path-filter-locale-escape:4e74785`
- state `needs_owner`
- waiting_reason: Jira key start requires Owner ابدأ

Bot Chat delivery failed on the pre-existing live-owner lock (Desktop tui pid 926544). The finding is in cron output `2026-09-04_18-14-03.md`.

## 2. Event wake + replay

`autonomy_webhook_filter.py` on Badr/Sami routes. Live replay of `{workflow_run.id: 999002}`:

- first: JSON pass-through with original GitHub fields + `_autonomy`
- second: `[SILENT]`

## 3. NOTHING_WORTH_DOING

`hermes -p fares autonomy noop` → `[SILENT]`. Fares status: `cooldown_active=true`, `nothing_worth_doing=1`. No Owner message.

## 4. Collaboration

Nasser work `w_974772883c69459d` (`erp:pilot-collab-1`) → dry-run delegate to Sami allowed. Replay rejected `duplicate_delegation`. Nasser row is `waiting` on Sami. No Owner approval.

## 5. Isolation

Sami work `w_63da52bf439a42af` completed independently. Nasser list does not contain Sami’s row.

## 6. Restart / replay

Event and tick claims on live Badr home: second delivery `duplicate=true`. Store is profile-local SQLite. Gateway was not restarted; work.db survives process restart by construction (same API as cron notepad).

## Computer

`tools/computer_use_tool.py` is present on the VPS. This observe turn used existing `gh`/tools, not a new Computer runtime. BWM-796 was not reopened.
