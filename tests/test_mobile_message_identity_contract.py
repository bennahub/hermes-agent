"""Identity correlation is presentation metadata, never text-based delivery proof."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import uuid
import pytest

from hermes_state import SessionDB
from agent.turn_context import _stage_turn_user_message
from tui_gateway import server


def test_native_user_row_and_completion_identity(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('canonical', 'gui')
    db.create_session('other', 'gui')
    session = {'session_key': 'canonical'}
    agent = SimpleNamespace(session_id='canonical')
    monkeypatch.setattr(server, '_session_db', lambda session: nullcontext(db))
    client_id = str(uuid.uuid4())
    owner, _ = _stage_turn_user_message(agent, 'same words', None, None, None, None,
                                      {'client_message_ids': [client_id], 'reply_to': {'message_id': 1}})
    owner_id = db.append_message('canonical', 'user', owner['content'],
                                 display_metadata=owner['display_metadata'])
    reply_id = db.append_message('canonical', 'assistant', 'same reply', timestamp=100)
    candidate = {'role': 'assistant', 'content': 'same reply', '_row_id': reply_id}
    result = {'messages': [owner, candidate]}
    assert server._persisted_completion_identity(session, result, agent) == {
        'message_session_id': 'canonical', 'message_root_session_id': 'canonical',
        'message_row_id': reply_id, 'message_timestamp': 100.0}
    assert server._persisted_completion_identity(session, result, agent, [candidate]) == {}
    for bad in [owner_id, -1, True, 99999]:
        assert server._persisted_completion_identity(session, {'messages': [dict(candidate, _row_id=bad)]}, agent) == {}
    assert server._persisted_completion_identity(session, result, SimpleNamespace(session_id='other')) == {}
    rows = db.get_messages('canonical')
    assert rows[0]['display_metadata']['client_message_ids'] == [client_id]
    assert rows[0]['content'] == 'same words'
    # Separate equal text rows retain separate canonical identities.
    second_id = db.append_message('canonical', 'assistant', 'same reply', timestamp=101)
    assert second_id != reply_id
    assert server._persisted_completion_identity(session, {'messages': [dict(candidate, _row_id=second_id)]}, agent)['message_row_id'] == second_id
    # The real completion projection carries the same identity, not just the helper.
    from tui_gateway.prompt_turn import _TurnRun
    monkeypatch.setattr(server, '_get_usage', lambda agent: {})
    session['history_lock'] = threading.Lock()
    st = _TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None,
                  receipt_committed=False, result=dict(result, final_response='same reply'))
    payload, _, status = server._complete_turn_payload(session, st, None, 80)
    assert status == 'complete'
    assert payload['message_row_id'] == reply_id
    assert payload['message_session_id'] == 'canonical'
    db.end_session('canonical', 'compression')
    db.create_session('compressed-child', 'gui', parent_session_id='canonical')
    child_id = db.append_message('compressed-child', 'assistant', 'new reply')
    agent.session_id = 'compressed-child'
    result['messages'] = [{'role': 'assistant', 'content': 'new reply', '_row_id': child_id}]
    identity = server._persisted_completion_identity(session, result, agent)
    assert identity['message_session_id'] == 'compressed-child'
    assert identity['message_root_session_id'] == 'canonical'
    db.close()


def test_normal_and_queued_submission_preserve_correlations(monkeypatch):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    session = {'running': True, 'history_lock': threading.Lock(), 'attached_images': [],
               'session_key': 'canonical', 'history': [], 'agent': SimpleNamespace(),
               'inflight_turn': {'user': 'same words'}}
    captured = []
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
    monkeypatch.setattr(server, '_resolve_reply_reference', lambda *a: ({'message_id': 1}, 'quote\n'))
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: captured.append((a, kw)) or True)
    server._run_after_agent_ready('r', 'handle', session, 'same words', None, None,
                                  client_message_id=ids[0])
    metadata = captured[-1][1]['display_metadata']
    assert {k: v for k, v in metadata.items() if k != 'projection'} == {'reply_to': {'message_id': 1}, 'client_message_ids': [ids[0]]}
    assert metadata['projection']['origin'] == 'owner'
    assert metadata['projection']['audience'] == 'owner'
    assert captured[-1][1]['persist_text'] == 'same words'
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda s: False)
    for client_id in ids:
        response = server._handle_busy_submit('r', 'handle', session, 'same words', None,
                                             client_message_id=client_id)
        assert response['result']['status'] == 'queued'
    queued = session['queued_prompt']
    assert queued['display_metadata']['client_message_ids'] == ids
    assert server._sanitize_queued_entry_vs_inflight_user(queued, 'same words') == queued
    session['running'] = False
    assert server._drain_queued_prompt('r', 'handle', session)
    assert captured[-1][1]['display_metadata']['client_message_ids'] == ids


@pytest.mark.parametrize('origin,audience,kind', [('owner','owner',None), ('peer','peer','a2a_message'), ('runtime','internal','hidden')])
def test_real_agent_turn_preserves_identity_through_gateway_persistence(tmp_path, monkeypatch, origin, audience, kind):
    """Only the provider I/O boundary is synthetic; user staging/flush/adoption are native."""
    from unittest.mock import MagicMock, patch
    from tests.test_tui_gateway_server import _configure_immediate_prompt_run, _session
    from tests.run_agent.test_run_agent import _mock_response
    from run_agent import AIAgent

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    _configure_immediate_prompt_run(monkeypatch, tmp_path, immediate_threads=False)
    db = SessionDB(tmp_path / 'state.db')
    sid = 'native-identity'
    db.create_session(sid, 'desktop', model='test/model')
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('model_tools.check_toolset_requirements', return_value={}), \
         patch('agent.process_bootstrap.OpenAI'):
        agent = AIAgent(api_key='synthetic-not-a-real-key', base_url='https://provider.invalid/v1',
                        model='test/model', quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=db, session_id=sid)
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(content='Synthetic answer')
    agent._session_db_created = True
    agent._cached_system_prompt = 'Synthetic test system'
    agent._skip_mcp_refresh = True
    session = _session(session_key=sid, agent=agent, running=True, profile_home=str(tmp_path))
    monkeypatch.setattr(server, '_session_db', lambda s: nullcontext(db))
    monkeypatch.setattr(server, '_sync_agent_compression_with_config', lambda *a: None)
    monkeypatch.setattr(server, '_sync_bot_capabilities', lambda *a: None)
    monkeypatch.setattr(server, '_apply_pending_model_switch', lambda *a: None)
    events = []
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: events.append(a))
    server._sessions['native-handle'] = session
    try:
        for reply in [False, True]:
            client_id = str(uuid.uuid4())
            from agent.message_projection import native_metadata, projection
            metadata = native_metadata(origin, audience, 'collaboration' if origin == 'peer' else 'control' if origin == 'runtime' else 'message',
                                       metadata={'client_message_ids': [client_id]})
            if reply:
                metadata['reply_to'] = {'message_id': 1, 'excerpt': 'Quoted context'}
            session['running'] = True
            assert server._run_prompt_submit('r', 'native-handle', session,
                'quote\nOwner input' if reply else 'Owner input',
                display_metadata=metadata, display_kind=kind, persist_text='Owner input' if reply else None)
            session['_run_thread'].join(timeout=15)
            assert not session['_run_thread'].is_alive(), 'synthetic native turn did not settle'
            rows = db.get_messages(sid)
            owners = [r for r in rows if r['role'] == 'user']
            assert owners, events
            assert owners[-1]['content'] == 'Owner input'
            assert owners[-1]['display_metadata'] == metadata
            complete = [a[2] for a in events if a[0] == 'message.complete'][-1]
            assert complete['status'] == 'complete', complete
            assert complete['message_row_id'] == rows[-1]['id']
            assert complete['message_session_id'] == sid
            assert complete['display_metadata'] == rows[-1]['display_metadata']
            assert projection(rows[-1]['display_metadata'])['audience'] == ('peer' if origin == 'peer' else 'owner')
            for call in agent.client.chat.completions.create.call_args_list:
                assert all('display_metadata' not in message and 'display_kind' not in message for message in call.kwargs['messages'])
            assert session['history'][-2]['display_metadata'] == metadata
        assert len([r for r in db.get_messages(sid) if r['role'] == 'user']) == 2
        assert agent.client.chat.completions.create.call_count == 2
    finally:
        server._sessions.pop('native-handle', None)
        db.close()
