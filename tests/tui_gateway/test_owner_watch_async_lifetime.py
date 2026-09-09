"""The admitted turn's real daemon worker owns its watch, not the dispatch thread."""
import threading
from types import SimpleNamespace
import pytest

from tui_gateway import server, interactive_progress
from agent.interactive_timing import emit_stage


@pytest.fixture
def asynchronous_turn(monkeypatch):
    previous=lambda *args:None
    agent=SimpleNamespace(session_id='stored-qa',_interactive_stage_callback=previous)
    session={'running':True,'history_lock':threading.RLock(),'history':[], 'agent':agent}
    events=[];clock=[100.];watches=[];entered=threading.Event();release=threading.Event()
    from agent import interactive_timing
    monkeypatch.setattr(interactive_timing,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    monkeypatch.setattr(server,'_sessions',{'qa':session})
    monkeypatch.setattr(server,'_wait_agent_for_prompt',lambda *a:None)
    monkeypatch.setattr(server,'_resolve_reply_reference',lambda *a:(None,''))
    monkeypatch.setattr(server,'_session_live_status',lambda *a:'running')
    monkeypatch.setattr(server,'_admit_prompt_turn',lambda *a:([],agent))
    monkeypatch.setattr(server,'_record_turn_marker',lambda *a:'marker')
    for name in ['_revoke_prompt_owner_nonce','_finish_turn','_clear_inflight_turn','_retire_turn_marker','_emit_settled_session_info']:
        monkeypatch.setattr(server,name,lambda *a,**k:None)
    def emit(event,sid,payload=None):
        events.append((event,payload or {}))
        interactive_progress.record(session,event,payload or {},monotonic=clock[0])
    monkeypatch.setattr(server,'_emit',emit)
    def prepare(*args):
        emit_stage(agent,'CONTEXT_BUILD_START')
        emit_stage(agent,'MODEL_REQUEST_START')
        entered.set()
        assert release.wait(5),'fixture worker release timed out'
        emit_stage(agent,'CONTEXT_BUILD_DONE')
        emit_stage(agent,'MODEL_FIRST_STREAM_DELTA')
        emit_stage(agent,'MODEL_REQUEST_END')
        return None  # native preparation refusal still runs the actual worker finally
    monkeypatch.setattr(server,'_prepare_turn_input',prepare)
    followups=[]
    monkeypatch.setattr(server,'_run_post_turn_followups',lambda *a:followups.append(agent._interactive_stage_callback))
    real=interactive_progress.OwnerProgressWatch
    class Schedule:
        cancelled=False
        def __call__(self,*a):return self
        def cancel(self):self.cancelled=True
    def factory(*a,**k):
        watch=real(*a,**k,clock=lambda:clock[0],scheduler=Schedule());watches.append(watch);return watch
    monkeypatch.setattr(interactive_progress,'OwnerProgressWatch',factory)
    try:yield SimpleNamespace(agent=agent,session=session,events=events,clock=clock,watches=watches,entered=entered,release=release,previous=previous,followups=followups)
    finally:
        release.set()
        if session.get('_run_thread'):session['_run_thread'].join(5)
        for watch in watches:watch.close()


def start():
    server._run_after_agent_ready('rpc','qa',server._sessions['qa'],'Harmless QA',None,None,owner_task_nonce='synthetic-owner',received_monotonic=100.)


def test_watch_survives_async_dispatch_and_warns_until_real_worker_finishes(asynchronous_turn):
    t=asynchronous_turn
    start()
    assert t.entered.wait(2)
    watch=t.watches[0]
    assert not watch.closed  # dispatch returned while actual turn remains blocked
    assert t.session['_run_thread'].is_alive()
    assert t.agent._interactive_stage_callback is watch._stage_callback
    t.clock[0]=131.;watch.tick()
    assert any(event=='notification.show' for event,_ in t.events)
    t.release.set();t.session['_run_thread'].join(2)
    assert not t.session['_run_thread'].is_alive()
    assert watch.closed and watch.handle.cancelled
    assert t.agent._interactive_stage_callback is t.previous
    assert {'CONTEXT_BUILD_START','CONTEXT_BUILD_DONE','MODEL_REQUEST_START','MODEL_FIRST_STREAM_DELTA','MODEL_REQUEST_END'}<=set(watch.state['stages'])
    assert watch.tick() is False


def test_refused_dispatch_closes_watch_in_outer_owner(asynchronous_turn,monkeypatch):
    t=asynchronous_turn
    monkeypatch.setattr(server,'_admit_prompt_turn',lambda *a:None)
    start()
    assert t.watches[0].closed and t.agent._interactive_stage_callback is t.previous
    assert '_run_thread' not in t.session


def test_actual_terminal_closes_watch_before_post_turn_followup(asynchronous_turn,monkeypatch):
    t=asynchronous_turn
    def prepare(*args):return 'prompt','message',80,None
    def invoke(sid,session,st,*args):
        t.entered.set();assert t.release.wait(5)
        st.result={'completed':True,'final_response':'Synthetic completion'}
    monkeypatch.setattr(server,'_prepare_turn_input',prepare)
    monkeypatch.setattr(server,'_invoke_agent',invoke)
    monkeypatch.setattr(server,'_absorb_turn_result',lambda *a:None)
    monkeypatch.setattr(server,'_complete_turn_payload',lambda *a:({},'Synthetic completion','complete'))
    monkeypatch.setattr(server,'_goal_followup_after_turn',lambda *a:None)
    monkeypatch.setattr(server,'_after_complete_turn',lambda *a:None)
    start();assert t.entered.wait(2)
    assert not t.watches[0].closed
    t.release.set();t.session['_run_thread'].join(2)
    assert any(event=='message.complete' for event,_ in t.events)
    assert t.watches[0].closed and t.followups==[t.previous]


def test_worker_finalizer_exception_still_detaches_watch(asynchronous_turn,monkeypatch):
    t=asynchronous_turn;errors=[]
    monkeypatch.setattr(threading,'excepthook',lambda args:errors.append(args.exc_value))
    def fail(*args):raise RuntimeError('synthetic finalizer failure')
    monkeypatch.setattr(server,'_finish_turn',fail)
    start();assert t.entered.wait(2)
    t.release.set();t.session['_run_thread'].join(2)
    assert errors and t.watches[0].closed
    assert t.agent._interactive_stage_callback is t.previous


def test_thread_start_failure_leaves_outer_watch_owner(asynchronous_turn,monkeypatch):
    t=asynchronous_turn
    class BrokenThread:
        def __init__(self,**kwargs):pass
        def start(self):raise RuntimeError('synthetic thread failure')
        def join(self,*args):pass
        def is_alive(self):return False
    monkeypatch.setattr(server,'threading',SimpleNamespace(Thread=BrokenThread,current_thread=threading.current_thread,Event=threading.Event))
    with pytest.raises(RuntimeError,match='thread failure'):start()
    assert t.watches[0].closed and t.agent._interactive_stage_callback is t.previous


def test_old_watch_does_not_clobber_replacement_observer(asynchronous_turn):
    t=asynchronous_turn
    start();assert t.entered.wait(2)
    replacement=lambda *args:None
    t.agent._interactive_stage_callback=replacement
    t.release.set();t.session['_run_thread'].join(2)
    assert t.watches[0].closed and t.agent._interactive_stage_callback is replacement


def test_start_ack_failure_after_worker_launch_preserves_worker_ownership(asynchronous_turn,monkeypatch):
    t=asynchronous_turn
    revoked=[]
    monkeypatch.setattr(server,'_revoke_prompt_owner_nonce',lambda nonce:revoked.append(nonce))
    class StartedThenRaised(threading.Thread):
        def start(self):
            super().start()
            assert t.entered.wait(2)
            raise RuntimeError('started but acknowledgement failed')
    monkeypatch.setattr(server,'threading',SimpleNamespace(Thread=StartedThenRaised,current_thread=threading.current_thread,Event=threading.Event))
    with pytest.raises(RuntimeError,match='acknowledgement failed'):start()
    assert t.session['_run_thread'].is_alive() and t.session['running'] is True
    watch=t.watches[0]
    assert watch.worker_owned and not watch.closed
    assert revoked==[]
    assert t.agent._interactive_stage_callback is watch._stage_callback
    t.clock[0]=131.;watch.tick()
    assert any(event=='notification.show' for event,_ in t.events)
    t.release.set();t.session['_run_thread'].join(2)
    assert watch.closed and t.agent._interactive_stage_callback is t.previous
    assert revoked==['synthetic-owner']


def test_hidden_turn_start_ack_failure_preserves_actual_running_state(asynchronous_turn,monkeypatch):
    t=asynchronous_turn
    class StartedThenRaised(threading.Thread):
        def start(self):
            super().start();assert t.entered.wait(2)
            raise RuntimeError('hidden start acknowledgement failed')
    monkeypatch.setattr(server,'threading',SimpleNamespace(Thread=StartedThenRaised,current_thread=threading.current_thread))
    with pytest.raises(RuntimeError,match='acknowledgement failed'):
        server._run_after_agent_ready('rpc','qa',t.session,'Synthetic hidden QA','hidden',None)
    assert not t.watches and t.session['running'] is True
    t.release.set();t.session['_run_thread'].join(2)
    assert t.session['running'] is False


def test_new_turn_started_during_old_settled_info_restores_original_observer(asynchronous_turn, monkeypatch):
    t = asynchronous_turn
    old_settled = threading.Event()
    release_settled = threading.Event()
    new_entered = threading.Event()
    release_new = threading.Event()
    old_thread = None
    def settled(*args):
        if threading.current_thread() is old_thread:
            old_settled.set()
            assert release_settled.wait(5)
    monkeypatch.setattr(server, '_emit_settled_session_info', settled)
    start()
    assert t.entered.wait(2)
    old_thread = t.session['_run_thread']
    old_watch = t.watches[0]
    try:
        t.release.set()
        assert old_settled.wait(2)
        assert t.session['running'] is False
        assert old_watch.closed
        def new_prepare(*args):
            new_entered.set()
            assert release_new.wait(5)
            return None
        monkeypatch.setattr(server, '_prepare_turn_input', new_prepare)
        # Native prompt.submit permits a fresh admission after running=False.
        with t.session['history_lock']:
            t.session['running'] = True
        start()
        assert new_entered.wait(2)
        new_thread = t.session['_run_thread']
        new_watch = t.watches[1]
        assert t.agent._interactive_stage_callback is new_watch._stage_callback
        release_settled.set()
        old_thread.join(2)
        assert old_watch.closed
        assert t.agent._interactive_stage_callback is new_watch._stage_callback
        release_new.set()
        new_thread.join(2)
        assert new_watch.closed
        assert t.agent._interactive_stage_callback is t.previous
    finally:
        release_settled.set()
        release_new.set()
        old_thread.join(2)
