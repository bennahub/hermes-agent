"""Native work transitions are fenced through consume-once action admission."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from agent.autonomy import store
from agent.autonomy.paths import work_db_path
from agent.execution_scope import Binding
from agent.execution_scope_admission import claim_action
from hermes_state import SessionDB


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB()
    scope_id = db.create_or_get_scope('work:test', {'work': 'test'}, {'objective': 'test'})['scope_id']
    work = store.start_work(why='test', outcome='test', done_contract='test', idempotency_key='test',
        hermes_home=tmp_path, refs={'resume_generation': 1,
        'execution_scope': {'scope_id': scope_id, 'db_path': str(db.db_path)}})['work']
    return Binding(db, scope_id, work_id=work['id'], work_home=str(tmp_path), generation=1)


def test_prior_generation_change_or_stop_prevents_new_admission(tmp_path, monkeypatch):
    binding = _setup(tmp_path, monkeypatch)
    try:
        store.update_work(binding.work_id, hermes_home=tmp_path, refs={'resume_generation': 2})
        with pytest.raises(ValueError, match='no longer current'):
            claim_action(binding, 'stale', 'terminal', {})
        assert binding.db.get_scope_action(binding.scope_id, 'stale') is None
        binding.generation = 2
        assert claim_action(binding, 'current', 'terminal', {}) == {'status': 'claimed'}
        store.update_work(binding.work_id, hermes_home=tmp_path, refs={'owner_stop': {'reason': 'owner'}})
        with pytest.raises(ValueError, match='no longer current'):
            claim_action(binding, 'after-stop', 'terminal', {})
        # An uncertain already-admitted result survives stop; nothing re-arms it.
        assert binding.db.get_scope_action(binding.scope_id, 'current')['state'] == 'admitted'
        assert binding.db.get_scope_action(binding.scope_id, 'after-stop') is None
    finally:
        binding.db.close()


def test_work_write_fence_precedes_claim_and_releases_before_effect(tmp_path, monkeypatch):
    binding = _setup(tmp_path, monkeypatch)
    at_claim, update_attempted = threading.Event(), threading.Event()
    original_claim = binding.db.claim_scope_action
    def paused_claim(*args, **kwargs):
        at_claim.set()
        assert update_attempted.wait(10)
        # A separate SQLite connection bypasses the process RLock and still
        # cannot obtain the write fence. No timing-based absence assertion.
        independent = sqlite3.connect(str(work_db_path(tmp_path)), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                independent.execute('BEGIN IMMEDIATE')
        finally:
            independent.close()
        return original_claim(*args, **kwargs)
    monkeypatch.setattr(binding.db, 'claim_scope_action', paused_claim)
    def advance():
        assert at_claim.wait(10)
        update_attempted.set()
        return store.update_work(binding.work_id, hermes_home=tmp_path, refs={'resume_generation': 2})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(claim_action, binding, 'accepted', 'terminal', {})
            generation = pool.submit(advance)
            assert admission.result(timeout=15) == {'status': 'claimed'}
            assert generation.result(timeout=15)['refs']['resume_generation'] == 2
        assert binding.db.get_scope_action(binding.scope_id, 'accepted')['state'] == 'admitted'
        # Actual effect/result settlement runs after both locks have released.
        binding.db.complete_scope_action(binding.scope_id, 'accepted', {'ok': True})
        with pytest.raises(ValueError, match='no longer current'):
            claim_action(binding, 'late', 'terminal', {})
    finally:
        binding.db.close()
