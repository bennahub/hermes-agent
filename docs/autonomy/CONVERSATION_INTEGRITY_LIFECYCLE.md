# Conversation integrity: continuation lifecycle contract

The existing profile-local autonomy work ledger remains the only work store. The native cron ticker and canonical session turn lease remain the execution owners.

| Scope | Stable identity | Contract |
|---|---|---|
| Logical responsibility | work.id + immutable owner_request session/message | May produce multiple results and obligations. |
| Scheduling generation | resume_generation | Fences one cron job name and wake event. Advancing it clears current execution/verification and stale dispatch pointers. |
| Dispatch attempt | dispatch.nonce | Admission belongs to this nonce. A new attempt starts unadmitted. |
| Native execution | native turn ID, falling back to canonical lease holder | Recorded at bind_turn after native admission. |
| Logical result | pending_owner_result.id | Minted before final persistence; repeated identical results receive different IDs. |
| Delivered result | result ID plus canonical session/message receipt | Contains only the verification snapshot captured when that result was requested. |
| Owner obligation | owner_obligation.id + delivery_id | Unresolved, adjudicated, or resolved. A decision about D1 cannot discharge D2. |

`refs.lifecycle` retains identity-keyed attempt, execution, verification, delivery and obligation records within the same transactional work row. Current pointers can change without deleting those records. Admission is monotonic for one nonce. Historical verification is never backfilled from a later current proof. Legacy proofs remain standalone unless an explicit delivery binding exists.

`Needs you` is projected from the current unresolved obligation. Legacy rows retain the existing exact-delivery projection until the bounded migration adds structured obligation records. No migration adjudicates or resolves a business decision. A delivery-bound historical adjudication is retained as a fact for that delivery.

Production finalization stamps the pending result ID at `message_projection.stamp_final`; outbox reconciliation accepts only that exact work/result identity. Content and timestamps are not result identities. The native lease and pending-result CAS serialize finalization and outbox delivery. The existing transcript finalizer still owns ordinary message persistence.

Pre-admission transport failures rearm silently up to the bounded ceiling, including workers killed before binding. Busy deferral does not spend the transport budget. At the ceiling, the structured outcome is `transport_exhausted`: the owner is explicitly asked to restart unfinished work, without a claim of uncertain effects from that attempt. An admitted interrupted outcome is `uncertain_execution` and is not automatically replayed. Successful but unfinished turns retain the separate bounded settlement budget.

Reconciliation uses stable work/generation job names under the native tick lock; run_resume uses a generation/state CAS. Rearm retains wake identity and historical facts. Lease-loss compensation cannot delete the historical facts recorded during the attempted transition.

Migration is additive metadata in existing `refs_json`, with SQLite online backups, an exact per-database population receipt, and an idempotent second-pass check. It does not infer missing history from prose, timestamps, command text, or process ancestry. Previously erased facts cannot be reconstructed by this migration.

Accepted Mac/iPhone behavior and the separate Turki compaction finding are outside this delta.
