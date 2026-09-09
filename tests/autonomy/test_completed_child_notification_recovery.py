"""Only a verified source-bound final may reconcile an old missing child ACK."""
import queue
import time
from types import SimpleNamespace

import pytest
from hermes_state import SessionDB
from agent.autonomy import owner_continuity as c, store
from tools import async_delegation as delegation


@pytest.mark.parametrize('case',['complete','hidden_only','unverified','wrong_child','wrong_parent','wrong_final_hash','stopped'])
def test_legacy_child_recovery_requires_exact_completed_owner_receipt(tmp_path,monkeypatch,case):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    db=SessionDB(tmp_path/'state.db');db.create_session('owner-source',source='desktop')
    try:
        mid=db.append_message('owner-source','user','Complete the entire QA task and return the verified final result without another Owner prompt.')
        work=c.register_owner_request(db,'owner-source',mid,hermes_home=tmp_path)
        event={'type':'async_delegation','delegation_id':'qa-child','session_key':'owner-source','parent_session_id':'owner-source','status':'completed','completed_at':time.time()}
        delegation._persist_dispatch({'delegation_id':'qa-child','session_key':'owner-source','parent_session_id':'owner-source','dispatched_at':time.time()})
        c.observe_dispatch(SimpleNamespace(_owner_continuity_work_id=work['id']),'delegate_task',{}, {'status':'dispatched','delegation_id':'qa-child'})
        delegation._persist_completion(event,{'results':[{'summary':'QA child complete'}]})
        hidden=db.append_message('owner-source','user','Native child completion',display_kind='hidden',display_metadata={'delegation_id':'other-child' if case=='wrong_child' else 'qa-child'})
        if case=='stopped':c.cancel_owner_session('owner-source',hermes_home=tmp_path)
        if case not in {'hidden_only','unverified','stopped'}:
            fixture=tmp_path/'owned.txt';fixture.write_text('QA complete')
            c.verify(work['id'],file=str(fixture),hermes_home=tmp_path)
        if case not in {'hidden_only','stopped'}:
            if case=='unverified':
                # Native unfinished work must remain recoverable; no counterfeit verification.
                c.request_finish(work['id'],'Need verification',terminal='needs_owner',hermes_home=tmp_path)
            else:c.request_finish(work['id'],'Verified final',hermes_home=tmp_path)
            c.deliver_pending(store.get_work(work['id'],tmp_path),tmp_path)
        if case=='wrong_parent':
            with db._conn:
                import json
                event['parent_session_id']='foreign-source'
                db._conn.execute("update async_delegations set event_json=? where delegation_id='qa-child'",(json.dumps(event),))
        if case=='wrong_final_hash':
            delivery=store.get_work(work['id'],tmp_path)['refs']['owner_delivery']
            with db._conn:db._conn.execute('update messages set content=? where id=?',('Changed final',delivery['message_id']))
        q=queue.Queue();count=delegation.restore_undelivered_completions(q)
        row=db._conn.execute("select delivery_state,delivery_claim from async_delegations where delegation_id='qa-child'").fetchone()
        if case=='complete':
            assert count==0 and q.empty() and row[0]=='delivered' and row[1] is None
            assert delegation.restore_undelivered_completions(q)==0
        else:
            assert count==1 and q.get_nowait()['delegation_id']=='qa-child' and row[0]=='pending'
    finally:db.close()
