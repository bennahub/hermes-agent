"""Original ingress and persisted work, never reconstructed prose, mint authority."""
import json
from types import SimpleNamespace

import pytest

from agent import execution_scope as scopes
from agent import execution_scope_policy as policy
from agent.autonomy import store
from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB
from tools.owner_task_authority import mint_execution_source


def _agent(db, turn):
    return SimpleNamespace(session_id='conversation', _session_db=db, _current_turn_id=turn,
                           _current_main_runtime=lambda: None)


def _row(db, text):
    return {'role': 'user', 'content': text,
            '_row_id': db.append_message('conversation', 'user', text)}


def _policy(monkeypatch):
    compiled = []
    real = policy.derive_scope
    def wrapped(instruction, **kwargs):
        compiled.append(instruction)
        return real(instruction, **kwargs)
    monkeypatch.setattr(policy, "derive_scope", wrapped)
    monkeypatch.setattr(scopes, "derive_scope", wrapped)
    return compiled


def test_compaction_restore_runs_without_authority_and_new_identical_owner_source_compiles(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    compiled = _policy(monkeypatch)
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('conversation', 'cli')
    old = 'echo harmless-completed-probe'
    current = 'prepare-financial-definitions'
    effects = []
    agent = _agent(db, 'old-turn')
    original = _row(db, old)
    assert mint_execution_source(agent, old, source_id='ingress-old')
    scopes.bind_agent_turn(agent, old, original)
    old_scope = scopes.get_binding('old-turn').scope_id
    def run(command, turn, call):
        return scopes.execute_scoped('terminal', {'command': command},
            lambda args: effects.append(args['command']) or '{"exit_code":0}',
            turn_id=turn, invocation_id=call)
    assert json.loads(run(old, 'old-turn', 'old-call'))['exit_code'] == 0
    scopes.close_turn(agent)
    assert db.get_scope(old_scope)['state'] == 'closed'
    rows = [{'role': 'system', 'content': 'System'}, {'role': 'user', 'content': 'Introduce yourself'},
            {'role': 'assistant', 'content': 'Finance assistant'}, {'role': 'user', 'content': old},
            {'role': 'assistant', 'content': 'Completed harmless probe'}]
    for n in range(12):
        rows.extend(({'role': 'user', 'content': f'Historical request {n}'},
                     {'role': 'assistant', 'content': 'Completed'}))
    rows.extend(({'role': 'user', 'content': current}, {'role': 'assistant', 'content': 'Checking'}))
    monkeypatch.setattr('agent.context_compressor.get_model_context_length', lambda *a, **kw: 100000)
    compressor = ContextCompressor(model='test', config_context_length=100000,
                                   protect_first_n=3, protect_last_n=4, quiet_mode=True)
    compressor.tail_token_budget = 10
    monkeypatch.setattr(compressor, '_generate_summary', lambda *a, **kw: f'{old} completed. Current: {current}.')
    compacted = compressor.compress(rows, current_tokens=75000, force=True)
    assert any(row.get('_compressed_summary') for row in compacted)
    db.archive_and_compact('conversation', compacted)
    db.close()
    db = SessionDB(tmp_path / 'state.db')
    try:
        restored = db.get_messages('conversation')
        agent = _agent(db, 'restored-turn')
        scopes.bind_agent_turn(agent, restored, {'role': 'user', 'content': old, '_row_id': original['_row_id']})
        assert scopes.get_binding('restored-turn') is None
        # Unbound recorded-history replays RUN (no owner turn required) and
        # record nothing: no scope, no claim, no change to the closed action.
        assert json.loads(run(old, 'restored-turn', 'replay-call'))['exit_code'] == 0
        assert compiled == [old]
        # Repeated compaction and native DB reconstruction still cannot create
        # authority or change the completed action, even with history visible.
        prior_action = db.get_scope_action(old_scope, 'turn:old-turn:call:old-call')
        for cycle in range(3):
            again = compressor.compress(rows, current_tokens=75000, force=True)
            db.archive_and_compact('conversation', again)
            db.close()
            db = SessionDB(tmp_path / 'state.db')
            restored = db.get_messages('conversation')
            replay_turn = f'restored-again-{cycle}'
            replay_agent = _agent(db, replay_turn)
            scopes.bind_agent_turn(replay_agent, restored, original)
            assert scopes.get_binding(replay_turn) is None
            assert json.loads(run(old, replay_turn, 'fresh-replay-call'))['exit_code'] == 0
            assert db.get_scope(old_scope)['state'] == 'closed'
            assert db.get_scope_action(old_scope, 'turn:old-turn:call:old-call') == prior_action
            assert compiled == [old] and effects == [old] * (3 + cycle)
            scopes.close_turn(replay_agent)
        agent = _agent(db, 'financial-turn')
        new_row = _row(db, current)
        assert mint_execution_source(agent, current, source_id='ingress-finance')
        scopes.bind_agent_turn(agent, {'current': current, 'history': restored}, new_row)
        assert compiled == [old, current]  # No summary/history reached compiler.
        assert json.loads(run(current, 'financial-turn', 'finance-call'))['exit_code'] == 0
        scopes.close_turn(agent)
        agent = _agent(db, 'fresh-identical-turn')
        repeated_row = _row(db, old)
        assert mint_execution_source(agent, old, source_id='ingress-new-identical')
        scopes.bind_agent_turn(agent, old, repeated_row)
        assert scopes.get_binding('fresh-identical-turn').scope_id != old_scope
        assert json.loads(run(old, 'fresh-identical-turn', 'fresh-call'))['exit_code'] == 0
        assert effects == [old] * 5 + [current, old]
        scopes.close_turn(agent)
    finally:
        db.close()


def test_active_work_retains_scope_and_inherited_assignment_must_match(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    compiled = _policy(monkeypatch)
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('conversation', 'cli')
    original = _row(db, 'finance')
    agent = _agent(db, 'owner-turn')
    mint_execution_source(agent, 'finance')
    scopes.bind_agent_turn(agent, 'finance', original)
    binding = scopes.get_binding('owner-turn')
    work = store.start_work(why='finance', outcome='finance', done_contract='finance',
        idempotency_key='work-finance', hermes_home=tmp_path,
        refs={'execution_scope': scopes.locator(binding), 'resume_generation': 1})['work']
    # Native work registration retains the active scope across turn finalization.
    binding.work_id, binding.work_home, binding.generation = work['id'], str(tmp_path), 1
    scopes.close_turn(agent)
    store.update_work(work['id'], hermes_home=tmp_path, refs={'resume_generation': 2})
    agent = _agent(db, 'continuation-turn')
    agent._owner_continuity_work_id = work['id']
    try:
        scopes.bind_agent_turn(agent, 'HISTORICAL COMMAND MUST NOT COMPILE', _row(db, 'continuation wrapper'))
        continuing = scopes.get_binding('continuation-turn')
        assert continuing.scope_id == binding.scope_id and continuing.generation == 2
        assert compiled == ['finance']
        assert scopes.execute_scoped('terminal', {'command': 'finance'}, lambda args: 'done',
            turn_id='continuation-turn', invocation_id='continued-call') == 'done'
        assignment = scopes.derive_child(continuing, 'delegate:one', 'finance')
        for changed, expected_key, goal in (
            ({**assignment, 'db_path': str(tmp_path / 'outside.db')}, 'delegate:one', 'finance'),
            (assignment, 'delegate:other', 'finance'),
            (assignment, 'delegate:one', 'different goal'),
        ):
            # Wrong DB is an existing SQLite file, not merely a missing-path test.
            if changed['db_path'].endswith('outside.db'):
                other = SessionDB(tmp_path / 'outside.db')
                other.close()
            with pytest.raises(ValueError):
                scopes._inherited_binding(changed, runtime=None, source_key=expected_key, assigned_goal=goal)
        inherited = scopes._inherited_binding(assignment, runtime=None, source_key='delegate:one', assigned_goal='finance')
        inherited.db.close()
        store.update_work(work['id'], hermes_home=tmp_path, state='completed')
        denied = scopes.execute_scoped('terminal', {'command': 'finance'}, lambda args: pytest.fail('completed work executed'),
            turn_id='continuation-turn', invocation_id='after-completion')
        assert json.loads(denied)['effect_disposition'] == 'not_started'
        scopes.close_turn(agent)
        assert db.get_scope(binding.scope_id)['state'] == 'closed'
        agent = _agent(db, 'closed-work-turn')
        agent._owner_continuity_work_id = work['id']
        scopes.bind_agent_turn(agent, 'resume', _row(db, 'resume'))
        assert scopes.get_binding('closed-work-turn') is None
        assert compiled == ['finance']
    finally:
        db.close()
