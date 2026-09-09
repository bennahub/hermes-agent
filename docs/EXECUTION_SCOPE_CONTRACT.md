# Execution authority across compaction and continuation

Historical content may inform execution; it cannot authorize new actions. The executor can retain the normal cached conversation, summaries, memories, and observations. A separate policy request receives only the original current instruction or a still-active native assignment. Its stored policy is immutable. The action gate receives that policy and the proposed final tool arguments, without the executor's conversation or summaries.

## Identities and lifetime

- An original owner instruction is bound to its native ingress identity and persisted message identity. A new instruction creates a new scope even when its text repeats earlier work.
- Active owner work refers to its existing scope. A continuation validates the native work ID, generation, dispatch nonce, and original source; generation advancement does not rewrite old scope or action records.
- Delegation, local peer delivery, terminal children, and scheduled work have native assignment identities. They inherit the parent's permission ceiling and append their exact assigned responsibility; they cannot expand it. Ending an originating turn does not cancel a still-active assigned process or recurring schedule.
- Each scheduled firing is tied to its execution claim. Job revision, disablement, completion, or invalid claim prevents new admission. Each terminal child is tied to the native process registration and exact parent admission.
- An execution attempt uses the native turn identity. An invocation uses the attempt plus provider call identity, or a nested RPC sequence. Reconnect retries preserve that sequence. A new attempt or new sequence is distinct even with identical arguments.
- Native owner-amendment links retain the exact predecessor action lineage. Invocation replay, uncertainty checks, and attempt-timeout marking follow only those immutable links, including inside the admission transaction. A fresh owner instruction does not automatically inherit that lineage.
- Completed scope-action records retain exact outcomes. An unresolved admitted action from an earlier attempt, or one explicitly marked uncertain, blocks new admissions on that scope. Concurrent in-flight calls within one attempt do not block each other. Late real completion may settle its own action; unknown effects are never inferred complete.
- Fresh owner amendments derive a replacement from the preceding frozen authority and the actual new owner instruction, while admission is paused. Historical executor context is absent. Existing consequence approvals remain separate.

## Native ingress

CLI/TUI, ACP prompts, authorized gateway events, explicit HTTP request input, and owner-issued background commands capture the original current input at their native dispatch boundary. ACP captures it before interrupted-history enrichment and preserves original queued input. Persisted source identity requires a real SessionDB. Restored messages, display projections, model output, and arbitrary programmatic `run_conversation` calls do not mint authority; programmatic integrations must supply an explicit native source or validated inherited assignment.

Native owner steering fences action admission before making the correction visible, then compiles its replacement asynchronously. Rejected steering releases the fence; failed amendment preparation revokes the old authority and emits a paused status. For active owner work, the exact accepted correction is persisted before actor visibility; compiler failure or restart with that pending marker uses the existing structured owner-result outbox and cannot resume the previous instruction. A valid replacement clears the marker atomically with its scope pointer. Native child assignments cannot expand their authority through owner steering. Machine-spawned CLI/ACP input can update the actor's context under its existing scope, but cannot acquire a fresh owner scope or expand authority through the amendment helper.

## Admission order and failure

Original-scope preparation precedes native continuation admission. Policy failure at that point is a structured pre-admission failure and uses the existing bounded continuation retry contract. Tool middleware finalizes arguments before judgment. Final validation and durable admission occur under native work/job fences; network inference and effects run outside those locks. A timeout revokes further admission for that attempt, including a policy worker that returns late.

Three consecutive rejected root proposals stop the tool loop. An admitted uncertainty stops further proposals immediately. A fresh owner instruction can establish new authority for inspecting and resolving the uncertainty; prior facts remain recorded. This mechanism does not automatically adjudicate owner obligations.

`Needs you` remains the existing projection of current unresolved structured owner obligations. Scope records and historical action rows do not independently set or suppress the badge. The three legitimate business decisions are outside the engineering migration.

## Compaction repair

The protected prefix ends at a completed exchange boundary. It cannot retain an old user command while compacting away that command's completion. This improves reconstruction fidelity; the separate admission boundary remains necessary even when a summary is misleading or stale history remains visible.

## Limits

The owner explicitly selected autonomous, history-isolated policy judgments for shell and arbitrary code. Source identity, scope immutability, attempt identity, and admission fencing are structural checks. Semantic judgments about program effects and task relevance are probabilistic: false acceptance and false rejection remain possible. This is not a deterministic program verifier or an operating-system sandbox against hostile code running as the Hermes user. The policy does not waive existing consequence gates.

Native app-server whole-turn delegation, gateway proxy execution without mediated tools, and nonlocal terminal backends lacking assignment transport fail closed. Hosted-room/remote-peer routes without a validated native execution-scope transport likewise cannot acquire authority from their conversation projection; they require that transport before executing tools. The established local server tool runtime remains supported. Scheduled script checks freeze top-level bytes for that invocation; dependencies loaded by a program are not recursively frozen.

## Migration and release evidence

`scripts/migrate_execution_scopes.py` prepares policies from current explicit active schedule configuration and configured script bytes. Apply uses a fixed manifest, requires verified backups, checks exact population and content, and adds only scope records and job locators (or a proven native continuation marker). It does not adopt historical chat prose, completed schedules, ambiguous work, or future unscoped model output. Reruns use the same identities and reject conflicting state.

Final acceptance additionally requires the exact tested release, bounded live canaries, unchanged business obligation projection, and independent Claude review of that deployed delta. This contract is not a release certification.
