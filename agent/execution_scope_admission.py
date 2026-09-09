"""Linearize native continuation authority with durable tool admission.

Lock order is autonomy work DB then SessionDB. The fence covers only validation
and claim, never policy inference or tool execution. A stop after admission may
prevent future calls but cannot undo an already accepted external operation.
"""
from __future__ import annotations


def claim_action(binding, invocation_id: str, tool: str, arguments: dict,
                 *, attempt_id: str = 'native-programmatic') -> dict:
    record = binding.db.get_scope(binding.scope_id)
    kind = (record or {}).get('source', {}).get('assignment_kind')
    is_cron = kind in {'cron', 'cron_run'}
    if is_cron and binding.work_id:
        raise ValueError('Ambiguous cron and continuation execution authority')
    if is_cron:
        from cron.execution_scope import job_admission_fence
        with job_admission_fence(record, binding.db):
            return binding.db.claim_scope_action(binding.scope_id, invocation_id, tool, arguments, attempt_id=attempt_id)
    if not binding.work_id:
        return binding.db.claim_scope_action(binding.scope_id, invocation_id, tool, arguments, attempt_id=attempt_id)

    from agent.autonomy import store

    with store.transaction(binding.work_home) as conn:
        row = conn.execute('SELECT * FROM work WHERE id=?', (binding.work_id,)).fetchone()
        work = store._row_to_work(row) if row is not None else None
        if (binding.revoked or not work
                or work['state'] not in {'working', 'waiting', 'investigating', 'actionable'}
                or work['refs'].get('owner_stop')
                or work['refs'].get('resume_generation') != binding.generation
                or work['refs'].get('execution_scope', {}).get('scope_id') != binding.scope_id
                or work['refs'].get('execution_scope', {}).get('db_path') != str(binding.db.db_path)):
            raise ValueError('Structured continuation authority is no longer current')
        return binding.db.claim_scope_action(binding.scope_id, invocation_id, tool, arguments, attempt_id=attempt_id)
