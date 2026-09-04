# AUTONOMY_REVIEW

Supervisor synthesis after live pilot. Child-agent reviews were used as independent readers, not as implementation.

## R1 — architecture / native simplicity

Reuse is cron + webhook + SOUL + `[SILENT]` + Bot Chat. The only new state is a notepad-shaped SQLite ledger. Computer was not rebuilt. BWM-797 files were not edited.

Verdict: **APPROVE_WITH_FINDINGS** (nonblocking: ledger is extra state because `todo` is not durable).

## R2 — autonomy / loop / idempotency

Unit coverage: event/tick replay, duplicate work keys, already-working block, parent secondary, fanout/ping-pong, webhook `[SILENT]`, restart same row. Live: webhook replay `[SILENT]`; delegate replay `duplicate_delegation`.

Finding (nonblocking, repaired in prompt): first Badr observe reported a real CI break but did not call `work-start`. Prompt now requires the CLI.

Verdict: **APPROVE_WITH_FINDINGS**

## R3 — reliability / boundaries

Profile isolation holds. Hidden ticks stay `[SILENT]`. Webhook secrets were not logged by autonomy code. Live-owner Bot Chat delivery failure is pre-existing, not introduced here.

Finding: clearing all inference pins would send Codex-default profiles into a known 429. Restored: pin `anthropic` only when the profile default is Codex. Badr (already Anthropic) stays unpinned.

Verdict: **APPROVE_WITH_FINDINGS**

## Repair after R1/R2/R3 (same candidate)

Blocking items repaired and covered by tests:

- Cron observe skip now emits `{"wakeAgent": false}` so native `_parse_wake_gate` skips the LLM. Webhook duplicates still use `[SILENT]`.
- `--parent` must be a real open work id.
- `work-complete` accepts positional `id` or `--work-id`.
- Failed A2A send undoes the collab claim and is retryable; `_send_bot_chat` pops `HERMES_HOME` and uses a 600s timeout.
- Resume-bucket fingerprints apply only to active execution states, not `waiting` / `needs_owner`.
- Completed idempotency keys are no longer pruned.
- Bare `hermes autonomy rollout` defaults to the four-agent pilot; `--all` is required for the rest of the catalog.

`scripts/run_tests.sh tests/autonomy/` → 55 passed.

Remaining nonblocking: paraphrased/triangle A2A, GitHub action-spam event keys, notices table unused, duplicate docs trees.

## External

`codex` is installed locally. A full exact-head/diff Codex review was not completed for this candidate. Do not treat Grok subagents as that review.
