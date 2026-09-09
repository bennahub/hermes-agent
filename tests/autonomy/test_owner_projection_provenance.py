"""Display provenance follows real native work transitions, never prose guesses."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from agent.autonomy import owner_continuity as continuity, store
from agent.message_projection import projection, stamp_final
from agent.turn_context import _stage_turn_user_message
from hermes_state import SessionDB
from tui_gateway.server import _canonical_owner_read_state, _history_to_messages


def native_work(home):
    db = SessionDB(home / 'state.db')
    db.create_session('owner', source='desktop')
    mid = db.append_message('owner', 'user', 'After two minutes verify QA and report back')
    work = continuity.register_owner_request(db, 'owner', mid, hermes_home=home)
    return db, work


def test_native_wait_then_question_and_outbox_preserve_owner_audience(autonomy_home):
    db, work = native_work(autonomy_home)
    agent = SimpleNamespace(_owner_continuity_work_id=work['id'], _persist_user_message_idx=0)
    try:
        until = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        continuity.wait(work['id'], until=until, hermes_home=autonomy_home)
        messages = [{'role':'user','content':'Original'}, {'role':'assistant','content':'I will return after checking'}]
        stamp_final(agent, messages)
        assert projection(messages[-1]['display_metadata'])['purpose'] == 'progress'
        db.append_messages_batch('owner', messages[-1:])
        assert _canonical_owner_read_state(db,'owner')['latest_reply_at'] is None
        continuity.request_finish(work['id'], 'Which QA variant?', terminal='needs_owner', hermes_home=autonomy_home)
        waiting = store.get_work(work['id'], autonomy_home)
        assert continuity.deliver_pending(waiting, hermes_home=autonomy_home)
        rows = db.get_messages('owner')
        assert rows[-1]['content'] == 'Which QA variant?'
        assert projection(rows[-1]['display_metadata'])['purpose'] == 'decision'
        assert _canonical_owner_read_state(db,'owner')['latest_reply_at'] == rows[-1]['timestamp']
        visible = _history_to_messages(rows)
        assert [r['text'] for r in visible] == ['After two minutes verify QA and report back', 'Which QA variant?']
    finally:
        db.close()


@pytest.mark.parametrize('valid', [True, False])
def test_actual_continuation_nonce_and_source_control_input_typing(autonomy_home, monkeypatch, valid):
    db, work = native_work(autonomy_home)
    try:
        store.update_work(work['id'], refs={'resume_generation': 1, 'dispatch': {'nonce':'native-nonce', 'generation':1}}, hermes_home=autonomy_home)
        monkeypatch.setenv('HERMES_OWNER_CONTINUATION_ID', work['id'])
        monkeypatch.setenv('HERMES_OWNER_CONTINUATION_NONCE', 'native-nonce' if valid else 'foreign')
        agent = SimpleNamespace(session_id='owner', _session_db=db)
        text = '[Owner task continuation, not the user.]'
        row, _ = _stage_turn_user_message(agent, text, None, None, None, None, None)
        db.append_messages_batch('owner', [row])
        assert row['role'] == 'user' and row['content'] == text
        if valid:
            assert row['display_kind'] == 'hidden'
            assert projection(row['display_metadata'])['source_message_id'] == work['refs']['owner_request']['message_id']
            assert continuity.bind_turn(agent,row)
            assert agent._owner_continuity_work_id == work['id']
        else:
            assert 'display_kind' not in row
            with pytest.raises(ValueError,match='continuation identity'):
                continuity.bind_turn(agent,row)
    finally:
        db.close()
