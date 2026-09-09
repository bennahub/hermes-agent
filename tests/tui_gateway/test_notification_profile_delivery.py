"""Native profile ledgers stay authoritative across poller and post-turn delivery."""
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from tools import async_delegation as delegation
from tui_gateway import server


@pytest.mark.parametrize('path',['poller','post_turn'])
@pytest.mark.parametrize('admitted',[True,False,'started_then_raised','rejected_exception'])
def test_native_profile_delivery_claim_ack_and_duplicate(tmp_path,monkeypatch,path,admitted):
    root=tmp_path/'root';root.mkdir();profile=root/'profiles'/'qa';profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME',str(root))
    event={'type':'async_delegation','delegation_id':'owned-child','session_key':'owned-source','origin_ui_session_id':'owned-ui'}
    token=set_hermes_home_override(profile)
    try:
        delegation._persist_dispatch({'delegation_id':'owned-child','parent_session_id':'owned-source','session_key':'owned-source','dispatched_at':time.time()})
        delegation._persist_completion({**event,'status':'completed','completed_at':time.time()},{'results':[{'summary':'QA'}]})
    finally:reset_hermes_home_override(token)
    submitted=[]
    monkeypatch.setattr(server,'_emit',lambda *a:None)
    monkeypatch.setattr(server,'_wire_desktop_sinks',lambda:None)
    def submit(*args,**kwargs):
        submitted.append((get_hermes_home(),kwargs))
        if admitted=='started_then_raised':
            worker=threading.Thread(target=lambda:None);session['_run_thread']=worker
            worker.start();worker.join(timeout=5)
            raise RuntimeError('start acknowledgement failed')
        if admitted=='rejected_exception':raise RuntimeError('not started')
        return admitted
    monkeypatch.setattr(server,'_run_prompt_submit',submit)
    session={'profile_home':str(profile),'session_key':'owned-source','running':False,'history_lock':threading.RLock()}
    def once(stop,sid,owned):
        assert get_hermes_home()==profile
        assert server._notif_claim_turn(owned)
        server._notif_dispatch_event(sid,owned,event,'QA child completion')
        server._notif_release_turn(owned)
    from tools import process_registry
    root_existed = (root/'state.db').exists()
    def dispatch():
        if path=='poller':
            monkeypatch.setattr(server,'_notification_poller_loop_in_home',once)
            stop=server._start_notification_poller('owned-ui',session)
            thread=server._notification_pollers[-1][1];thread.join(timeout=5);stop.set()
            assert not thread.is_alive()
        else:
            from tools import process_registry
            monkeypatch.setattr(server,'_drain_queued_prompt',lambda *a:False)
            monkeypatch.setattr(process_registry,'process_registry',SimpleNamespace(drain_notifications=lambda **kw:[(event,'QA child completion')]))
            server._run_post_turn_followups('rid','owned-ui',session,{},None)
            server._notif_release_turn(session)
    dispatch()
    assert get_hermes_home()==root and len(submitted)==1
    assert submitted[0][0]==profile and submitted[0][1]['display_metadata']['delegation_id']=='owned-child'
    with sqlite3.connect(profile/'state.db') as c:
        state,attempts,claim=c.execute("select delivery_state,delivery_attempts,delivery_claim from async_delegations where delegation_id='owned-child'").fetchone()
    assert (state,attempts,claim)==('delivered' if admitted in (True,'started_then_raised') else 'pending',1,None)
    assert (root/'state.db').exists() == root_existed
    if root_existed:
        with sqlite3.connect(root/'state.db') as c:
            assert c.execute("select count(*) from async_delegations where delegation_id='owned-child'").fetchone()[0]==0
    if admitted in (True,'started_then_raised'):
        dispatch()
        assert len(submitted)==1 and session['running'] is False
        import queue
        q=queue.Queue();token=set_hermes_home_override(profile)
        try:assert delegation.restore_undelivered_completions(q)==0
        finally:reset_hermes_home_override(token)


@pytest.mark.parametrize('kind',['foreign_path','symlink_profile','symlink_profiles','foreign_source'])
def test_notification_rejects_foreign_home_or_source(tmp_path,monkeypatch,kind):
    root=tmp_path/'root';root.mkdir();profiles=root/'profiles';profiles.mkdir()
    foreign=tmp_path/'foreign';foreign.mkdir();home=profiles/'qa'
    monkeypatch.setenv('HERMES_HOME',str(root))
    if kind=='foreign_path':home=foreign
    elif kind=='symlink_profile':home.symlink_to(foreign,target_is_directory=True)
    elif kind=='symlink_profiles':profiles.rmdir();profiles.symlink_to(foreign,target_is_directory=True);home.mkdir()
    else:home.mkdir()
    submitted=[];monkeypatch.setattr(server,'_notif_submit',lambda *a,**kw:submitted.append(a))
    session={'profile_home':str(home),'session_key':'owned-source','running':False,'history_lock':threading.RLock()}
    event={'type':'async_delegation','delegation_id':'foreign-child','session_key':'foreign-source'}
    if kind=='foreign_source':
        monkeypatch.setattr(server,'_sessions',{'owned-ui':session})
        import queue
        registry=SimpleNamespace(completion_queue=queue.Queue())
        assert server._notif_handle_event('owned-ui',session,event,set(),registry,lambda _:'Foreign result',None)
    else:
        assert server._notif_claim_turn(session)
        with pytest.raises(ValueError):server._notif_dispatch_event('owned-ui',session,event,'Foreign result')
    assert submitted==[] and session['running'] is False
    assert not (foreign/'state.db').exists() and not (root/'state.db').exists()


@pytest.mark.parametrize('path',['poller','post_turn'])
def test_ack_storage_failure_preserves_admitted_worker_and_claim(tmp_path,monkeypatch,path):
    root=tmp_path/'root';profile=root/'profiles'/'qa';profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME',str(root))
    event={'type':'async_delegation','delegation_id':'owned-child','session_key':'owned-source','origin_ui_session_id':'owned-ui'}
    token=set_hermes_home_override(profile)
    try:
        delegation._persist_dispatch({'delegation_id':'owned-child','parent_session_id':'owned-source','session_key':'owned-source','dispatched_at':time.time()})
        delegation._persist_completion({**event,'status':'completed','completed_at':time.time()},{'results':[{'summary':'QA'}]})
    finally:reset_hermes_home_override(token)
    session={'profile_home':str(profile),'session_key':'owned-source','running':False,'history_lock':threading.RLock()}
    stop=threading.Event();started=threading.Event()
    def work():started.set();stop.wait(5)
    worker=threading.Thread(target=work)
    def submit(*a,**kw):session['_run_thread']=worker;worker.start();assert started.wait(2);return True
    monkeypatch.setattr(server,'_run_prompt_submit',submit)
    monkeypatch.setattr(server,'_emit',lambda *a:None)
    def failed_ack(*a,**kw):raise OSError('owned ACK unavailable')
    monkeypatch.setattr(delegation,'complete_event_delivery',failed_ack)
    try:
        if path=='poller':
            assert server._notif_claim_turn(session)
            with pytest.raises(OSError,match='ACK unavailable'):server._notif_dispatch_event('owned-ui',session,event,'QA')
        else:
            from tools import process_registry
            monkeypatch.setattr(server,'_drain_queued_prompt',lambda *a:False)
            monkeypatch.setattr(process_registry,'process_registry',SimpleNamespace(drain_notifications=lambda **kw:[(event,'QA')]))
            server._run_post_turn_followups('rid','owned-ui',session,{},None)
        assert worker.is_alive() and session['running'] is True
        assert server._notif_claim_turn(session) is False
        with sqlite3.connect(profile/'state.db') as c:
            state,claim=c.execute("select delivery_state,delivery_claim from async_delegations where delegation_id='owned-child'").fetchone()
        assert state=='pending' and claim
    finally:stop.set();worker.join(timeout=5)
