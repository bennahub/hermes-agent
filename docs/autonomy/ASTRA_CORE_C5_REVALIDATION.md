# Astra independent core and C5 revalidation

Evidence collected 2026-09-05. The previous handoff is historical evidence;
its acceptance labels are not inherited.

## Current verdict

Architecture decision: **REPAIR**, retaining native cron, native webhook
filtering, native SessionDB and the existing profile-local work ledger.
No scheduler, task platform, or model tool was added.

Core and C5 were **REOPEN_REQUIRED** on first independent inspection.
The candidate repairs below pass local regression tests. Deployment and
independent review remain required before a final verified classification.
No live code, profile, job, message, or service was modified by this agent.

## Provenance

| Identity | Evidence |
|---|---|
| Canonical fork | `bennahub/hermes-agent` |
| Fork main, live remote read | `1d48bd42cffdf7ce9e1db5a50076604d0ad13881` |
| Upstream main, live remote read | `71f8c60f6a6ab8f2fab1e4d5c22d6dd9c856b63e` |
| Original core base | `180291162ff4df0d42b5dc4fecd08005cf7cebf9` |
| Source core HEAD | `dbcd325f5cb9bb8f48dc97a0266bc204713dc34e` |
| Isolated candidate | `astra/bwm-802-core-continuity`, worktree `astra-bwm-802-core` |
| Live VPS git HEAD | `b2bc82cdb44a685eb1ec3c7850e6b09256347bcc` |
| Live core overlay | All seven `agent/autonomy/*.py` files plus both autonomy CLI files match source core HEAD by SHA-256 |
| Live cron jobs | Matches source exactly, SHA-256 `2dbedcf6c3e38b081efb70117b596394935d761039a316c6252233ccfc7193d3` |
| Live scheduler | SHA-256 `f0b4f987722e67094f93c68702e28bc9466f79404115cd3462dea4a6d2da8367`; has 19 lines absent from source (background delivery propagation and hidden cron session creation) |

Source worktree dirty docs, telephony changes and personal-operations
files were left intact. Live BWM-797/Owner Authority/C0 changes were left
intact. The scheduler, config defaults and SessionDB changes must be
applied as narrow reviewed patches to the live files, never replacements.

## Reproduced defects and repairs

| Contract | Reproduction | Repair |
|---|---|---|
| Repeated observation initiative | Idle fingerprint remained `mission:<digest>` after a quiet run and three hours; native monitor suppressed all subsequent discovery | Idle discovery fingerprint changes after the 90-minute cooldown; active work retains its resume bucket; waiting/needs_owner stays quiet |
| Fresh event vs replay | Two different Jira updates on the same issue were both keyed only by issue/action; GitHub explicit delivery IDs were ignored when run ID existed | Delivery identities take precedence; Jira fallback keys include payload revision; GitHub run attempt distinguishes reruns |
| Bounded concurrent work | A competing work row inserted after admission checks allowed a second top-level initiative | SQLite `BEGIN IMMEDIATE` plus admission recheck inside insertion transaction |
| Restartable waiting state | Observation prompt omitted persisted waiting reason, completion contract and Jira ref | Resume context includes those durable facts |
| Scope authorization | Prompt demanded Owner for every merge/deploy even when already within authorized scope | Prompt honors existing scope authorization and identifies genuine external consequences |
| Last meaningful routine state | Real scheduler sequence: useful response, `[SILENT]`, failed run; next prompt contained only failure trace | One atomic response snapshot, at most 8,000 characters plus SHA-256; quiet, error and failed delivery do not overwrite it |
| Repeated unchanged alert | Native continuity only asked the model to avoid repetition | Identical successful response is suppressed at delivery using the persisted digest, including responses larger than the context cap |
| Nested prompt growth | Self continuity injected the entire previous run document including its prior prompt | Preserve response only; legacy history fallback skips silent/metadata runs and extracts the response section |
| Future session history growth | Native `sessions.auto_prune` was disabled on all profiles | Opt-in `cron.session_retention_days`; use existing native pruning only for ended cron sessions created after activation, excluding pinned sessions and exact canonical `Bot Chat`, even if misclassified as cron |

The continuity snapshot survives native output-file retention and remains
profile-local. Native no-change monitor suppression, wake gates, cron fire
claims, bounded notepad and execution ledger remain in use. Semantic
paraphrases of the same finding still require the native stable monitor
source or the agent's durable notepad; text equality cannot establish
semantic equivalence.

## Live pilot reconciliation

| Pilot | Independently found live evidence |
|---|---|
| Abu Saud | Work `aw_2adca88204eb4f29` completed; `aw_e2480607177c4e8a` waiting with Jira BWM-805; matching outbound collaboration row to Badr; one canonical Bot Chat `20260831_211630_020ed9` |
| Badr | Work `aw_3c713a35c37e49f5` completed with BWM-804; `aw_5def20d8c82f48c4` needs_owner; matching inbound collaboration row; owner projection metric 1 and dedup metric 2; one canonical Bot Chat `20260831_211713_ea4fe3` |
| Sami | Two completed work rows including `aw_06fb4cd1bf804e6e`; nothing-worth-doing metric 1; last observe run 2026-09-04 20:33:41+03:00 successful; one canonical Bot Chat `20260831_211744_8f1ef2` |
| Nasser | Current profile intentionally deleted. `/home/hermes/.hermes/profiles/.deleted/nasser` is the native `deleted` tombstone dated 2026-09-04 20:18:44 UTC. No active profile/job remains. Historical pilot DB/result cannot be independently recovered from currently located state. Do not resurrect or claim a current four-agent pilot. |

Current roster: 13 workforce profiles have enabled observation jobs. Hamad
has one disabled/paused observation job and remains isolated. The default
home has no observation job. Nasser is absent from current roster. Badr's
last observation status is `error`: `Interrupted by shutdown before
terminal completion.` This is preserved as a failure, not relabeled pass.

The live Sami monitor snapshot contained only its mission digest, directly
corroborating the idle-suppression reproduction. Tests and existing live
records independently establish the core contracts; no new cross-agent
message or Owner-visible pilot notice was sent during this inspection.

## Verification

- Original source autonomy suite: **61 passed** (8 files).
- Original native C5 context/monitor/jobs/notepad/execution suites:
  **196 passed** (5 files).
- New initial regressions: five core failures and one actual scheduler
  continuity failure reproduced on the original implementation.
- Repaired core + continuity + scheduler + context/monitor/jobs/notepad/
  execution suites: **372 passed** (16 files).
- Forward-only retention: **2 real SessionDB tests passed**, including
  active, pinned, pre-activation history, user sessions, NULL titles and a
  canonical Bot Chat with intentionally incorrect cron source.
- Final combined expanded run: **391 passed** (20 files), including native
  prune filters/pinned protection and cron session isolation. Independent
  review remains required against the frozen candidate.

## Deployment and activation boundary

The supervisor owns deployment. Back up exact live files and selected
profile configs first; verify expected digests immediately before change.
Apply selected additive core files and the narrow cron/SessionDB/config
hunks. Preserve the live scheduler's 19 existing additions and all other
dirty work. Import/compile and run the focused tests before service reload.

Activation is `cron.session_retention_days: 30` in the intended profile
configs. On the first enabled execution the native state DB records
`cron_session_retention_started_at`; this call deletes nothing. Later
maintenance selects only ended, unpinned `source=cron` rows started after
that boundary and inactive beyond 30 days, with exact `Bot Chat` excluded.
Previously existing history remains outside the policy. No global
`sessions.auto_prune` activation is needed.

After activation, verify config, boundary, service health, profile roster,
canonical chat IDs, existing work IDs and no unexpected notices. The live
runtime identity is a per-file manifest, not the VPS git HEAD alone.
