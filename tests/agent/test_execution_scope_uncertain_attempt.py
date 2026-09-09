"""Lost effects fence new IDs without collapsing concurrent native siblings."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent import execution_scope as scopes
from hermes_state import SessionDB


def _binding(tmp_path, monkeypatch, turn='attempt-one'):
    db = SessionDB(tmp_path / 'state.db')
    record = db.create_or_get_scope('original:one', {'message_id': 1}, {'objective': 'test'})
    binding = scopes.Binding(db, record['scope_id'])
    monkeypatch.setitem(scopes._TURNS, turn, binding)
    monkeypatch.setattr(scopes, 'judge_action', lambda *a, **kw: (True, 'test'))
    return binding


def test_uncertain_effect_blocks_new_ids_and_abandoned_prior_attempt_survives_restart(tmp_path, monkeypatch):
    binding = _binding(tmp_path, monkeypatch)
    sid = binding.scope_id
    effects = []
    def uncertain(args):
        effects.append(args)
        raise RuntimeError('lost result after effect')
    try:
        with pytest.raises(RuntimeError, match='lost result'):
            scopes.execute_scoped('terminal', {'step': 1}, uncertain,
                                  turn_id='attempt-one', invocation_id='call-a')
        with monkeypatch.context() as outage:
            outage.setattr(scopes, 'judge_action', lambda *a, **kw: pytest.fail('known uncertainty must precede policy'))
            denied = scopes.execute_scoped('terminal', {'step': 1}, lambda args: pytest.fail('replayed'),
                                          turn_id='attempt-one', invocation_id='call-b')
        assert isinstance(denied, scopes.ExecutionScopeDenied) and denied.uncertain
        assert len(effects) == 1
        assert binding.db.get_scope_action(sid, 'turn:attempt-one:call:call-b') is None
    finally:
        binding.db.close()
    db = SessionDB(tmp_path / 'state.db')
    try:
        monkeypatch.setitem(scopes._TURNS, 'attempt-two', scopes.Binding(db, sid))
        denied = scopes.execute_scoped('terminal', {'step': 1}, lambda args: pytest.fail('restarted replay'),
                                      turn_id='attempt-two', invocation_id='call-c')
        assert denied.uncertain
        # Actual late settlement resolves this exact admitted operation.
        db.complete_scope_action(sid, 'turn:attempt-one:call:call-a', 'known-result')
        assert scopes.execute_scoped('terminal', {'step': 2}, lambda args: 'next',
                                     turn_id='attempt-two', invocation_id='call-d') == 'next'
        # A crash can leave admitted without an explicit uncertain write.
        db.claim_scope_action(sid, 'abandoned', 'terminal', {}, attempt_id='attempt-two')
        monkeypatch.setitem(scopes._TURNS, 'attempt-three', scopes.Binding(db, sid))
        denied = scopes.execute_scoped('terminal', {}, lambda args: pytest.fail('abandoned replay'),
                                      turn_id='attempt-three', invocation_id='call-e')
        assert denied.uncertain
        assert db.get_scope_action(sid, 'abandoned')['state'] == 'admitted'
    finally:
        db.close()


def test_parallel_siblings_share_attempt_and_timeout_only_marks_unresolved_rows(tmp_path, monkeypatch):
    binding = _binding(tmp_path, monkeypatch)
    both_admitted = threading.Barrier(2)
    def effect(args):
        both_admitted.wait(timeout=10)
        return str(args['step'])
    def execute(step):
        return scopes.execute_scoped('terminal', {'step': step}, effect,
                                     turn_id='attempt-one', invocation_id='call-' + str(step))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(execute, (1, 2))) == ['1', '2']
        binding.db.claim_scope_action(binding.scope_id, 'late', 'terminal', {}, attempt_id='attempt-one')
        assert scopes.mark_attempt_uncertain('attempt-one') == 1
        assert binding.revoked
        assert binding.db.get_scope_action(binding.scope_id, 'late')['uncertain'] == 1
        for step in (1, 2):
            row = binding.db.get_scope_action(binding.scope_id, 'turn:attempt-one:call:call-' + str(step))
            assert row['state'] == 'completed' and row['uncertain'] == 0
    finally:
        binding.db.close()


def test_timeout_during_policy_revokes_late_worker_without_claiming_an_effect(tmp_path, monkeypatch):
    binding = _binding(tmp_path, monkeypatch)
    judging, release = threading.Event(), threading.Event()
    def judge(*args, **kwargs):
        judging.set()
        assert release.wait(10)
        return True, 'allowed after delay'
    monkeypatch.setattr(scopes, 'judge_action', judge)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(scopes.execute_scoped, 'terminal', {},
                lambda args: pytest.fail('late policy worker executed'),
                turn_id='attempt-one', invocation_id='late-worker')
            assert judging.wait(10)
            assert scopes.mark_attempt_uncertain('attempt-one') == 0
            release.set()
            result = pending.result(timeout=10)
            assert isinstance(result, scopes.ExecutionScopeDenied) and not result.uncertain
        assert binding.db.get_scope_action(binding.scope_id, 'turn:attempt-one:call:late-worker') is None
    finally:
        release.set()
        binding.db.close()


@pytest.mark.parametrize('outcome', [
    {'status': 'uncertain', 'error': 'Native command lost its outcome'},
    {'effect_disposition': 'unknown'},
    {'effect_disposition': 'uncertain'},
])
def test_native_unknown_result_is_not_recorded_as_completed(tmp_path, monkeypatch, outcome):
    import json
    binding = _binding(tmp_path, monkeypatch)
    try:
        result = scopes.execute_scoped('terminal', {}, lambda args: json.dumps(outcome),
                                       turn_id='attempt-one', invocation_id='unknown')
        assert isinstance(result, scopes.ExecutionScopeDenied) and result.uncertain
        row = binding.db.get_scope_action(binding.scope_id, 'turn:attempt-one:call:unknown')
        assert row['state'] == 'admitted' and row['uncertain'] == 1
        denied = scopes.execute_scoped('terminal', {}, lambda args: pytest.fail('replay'),
                                       turn_id='attempt-one', invocation_id='new-id')
        assert denied.uncertain
    finally:
        binding.db.close()


def test_known_failure_result_does_not_invent_an_uncertain_effect(tmp_path, monkeypatch):
    binding = _binding(tmp_path, monkeypatch)
    try:
        result = scopes.execute_scoped('terminal', {}, lambda args: '{"exit_code":1,"output":"uncertain is ordinary output"}',
                                       turn_id='attempt-one', invocation_id='known-failure')
        assert not isinstance(result, scopes.ExecutionScopeDenied)
        assert scopes.execute_scoped('terminal', {}, lambda args: 'observed',
                                      turn_id='attempt-one', invocation_id='next-step') == 'observed'
    finally:
        binding.db.close()
