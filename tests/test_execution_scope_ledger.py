"""Native state.db authority identities survive reopening and concurrent calls."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_state import SessionDB


def test_original_source_is_immutable_and_completed_history_cannot_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB()
    assert db.get_scope_for_source('original:conversation:3') is None
    source = {'session_id': 'conversation', 'message_id': 3}
    scope = {'instruction': 'Run harmless probe'}
    first = db.create_or_get_scope('original:conversation:3', source, scope)
    assert db.create_or_get_scope('original:conversation:3', source, scope) == first
    with pytest.raises(ValueError, match='rebound'):
        db.create_or_get_scope('original:conversation:3', source, {'instruction': 'expanded'})
    with pytest.raises(ValueError, match='rebound'):
        db.create_or_get_scope('original:conversation:3', {'message_id': 999}, scope)
    db.close_scope(first['scope_id'])
    db.close()
    db = SessionDB()
    try:
        closed = db.create_or_get_scope('original:conversation:3', source, scope)
        assert closed['scope_id'] == first['scope_id'] and closed['state'] == 'closed'
        assert db.get_scope_for_source('original:conversation:3') == closed
        assert db.close_scope(first['scope_id']) == closed
        with pytest.raises(ValueError, match='not active'):
            db.claim_scope_action(first['scope_id'], 'reconstructed-call', 'terminal', {'command': 'echo probe'})
        # Same legitimate owner text with a fresh original identity is distinct authority.
        second = db.create_or_get_scope('original:conversation:40', {**source, 'message_id': 40}, scope)
        assert second['scope_id'] != first['scope_id'] and second['state'] == 'active'
        assert db.get_scope(second['scope_id']) == second
    finally:
        db.close()


def test_native_transaction_claim_is_once_and_uncertainty_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB()
    sid = db.create_or_get_scope('work:one', {'work_id': 'one'}, {'instruction': 'test'})['scope_id']
    peer = SessionDB()
    barrier = threading.Barrier(2)
    def claim(connection):
        barrier.wait(timeout=10)
        return connection.claim_scope_action(sid, 'call-one', 'terminal', {'command': 'echo harmless'})['status']
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (db, peer)))
        assert sorted(outcomes) == ['claimed', 'uncertain']
    finally:
        db.close()
        peer.close()
    db = SessionDB()
    try:
        assert db.claim_scope_action(sid, 'call-one', 'terminal', {'command': 'echo harmless'}) == {'status': 'uncertain'}
        with pytest.raises(ValueError, match='different action'):
            db.claim_scope_action(sid, 'call-one', 'terminal', {'command': 'changed'})
        assert db.get_scope_action(sid, 'call-one')['state'] == 'admitted'
        assert db.get_scope_action(sid, 'absent') is None
        result = {'output': 'harmless', 'exit_code': 0}
        db.complete_scope_action(sid, 'call-one', result)
        assert db.get_scope_action(sid, 'call-one')['result'] == result
        assert db.claim_scope_action(sid, 'call-one', 'terminal', {'command': 'echo harmless'}) == {'status': 'completed', 'result': result}
        with pytest.raises(ValueError, match='immutable'):
            db.complete_scope_action(sid, 'call-one', {'output': 'different'})
        # Equal arguments do not collapse legitimate independent invocations.
        assert db.claim_scope_action(sid, 'call-two', 'terminal', {'command': 'echo harmless'}) == {'status': 'claimed'}
        db.close_scope(sid)
        assert db.complete_scope_action(sid, 'call-two', result)['status'] == 'completed'
        with pytest.raises(ValueError, match='not active'):
            db.claim_scope_action(sid, 'call-three', 'terminal', {})
        with pytest.raises(ValueError, match='never admitted'):
            db.complete_scope_action(sid, 'missing-call', result)
    finally:
        db.close()
