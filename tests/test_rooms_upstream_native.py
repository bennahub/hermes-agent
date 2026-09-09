"""Room membership, reply and attachment behavior over the native SQLite log."""
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest
import hermes_constants
from gateway import hosted_rooms as store, hosted_room_discussion as policy
from tui_gateway.hosted_room_service import HostedRoomService
from tui_gateway.hosted_room_driver import _bounded_terminal_result

PROFILES=('a','b','c')
MEMBERS=[{'member_id':p,'profile':p,'handle':p} for p in PROFILES]

@pytest.fixture
def home(tmp_path,monkeypatch):
    root=tmp_path/'hermes';root.mkdir()
    for p in PROFILES:
        profile=root/'profiles'/p;profile.mkdir(parents=True);(profile/'config.yaml').write_text('{}')
    monkeypatch.setenv('HERMES_HOME',str(root))
    token=hermes_constants.set_hermes_home_override(root)
    yield root
    hermes_constants.reset_hermes_home_override(token)

@pytest.fixture
def room(home):
    db=home/'state.db'
    raw=store.create_room(db,room_id='r',name='Room',members=MEMBERS,authority_gateway_id=store.local_authority_gateway_id())
    return db,raw

def events(db): return store.read_events(db,room_id='r',since_seq=0)['events']

def user(db,raw,text='Report',eid='u'):
    return store.append_event(db,room_id='r',event_id=eid,kind='message.user',actor={'kind':'user','id':'owner'},
                             payload={'text':text,'thread_id':'t'},authority_gateway_id=raw['authority_gateway_id'],authority_epoch=raw['authority_epoch'])

def settle(db,raw,text='Done',attachments=None):
    task=policy.plan_next_task(raw,events(db),local_profiles=PROFILES).task
    assert task
    result=_bounded_terminal_result({'text':text,**({'attachments':attachments} if attachments is not None else {})})
    publication=policy.plan_publication(raw,events(db),task,status='settled',result=result,local_profiles=PROFILES)
    for event in publication.events: store.append_event(db,**event.append_kwargs('r'))
    return task


def test_remove_retains_history_and_next_planning_ignores_removed_member(room):
    db,raw=room;user(db,raw)
    first=settle(db,raw)
    retained=[m for m in MEMBERS if m['member_id']!=first.member.member_id]
    updated=store.set_room_members(db,room_id='r',event_id='remove',members=retained)
    removed=next(m for m in updated['members'] if m['member_id']==first.member.member_id)
    assert removed['active'] is False
    assert len(updated['members'])==3
    assert any(e['kind']=='message.member' and e['payload']['member_id']==removed['member_id'] for e in events(db))
    user(db,updated,eid='u2')
    next_task=policy.plan_next_task(updated,events(db),local_profiles=PROFILES).task
    assert next_task and next_task.member.active
    assert next_task.member.member_id!=removed['member_id']


def test_membership_retry_conflict_reactivate_and_atomic_duplicate_rejection(room):
    db,raw=room
    updated=store.set_room_members(db,room_id='r',event_id='remove',members=MEMBERS[:2])
    retry=store.set_room_members(db,room_id='r',event_id='remove',members=MEMBERS[:2])
    assert retry['idempotent'] and retry['event']['seq']==updated['event']['seq']
    before=store.room_state(db,room_id='r')
    with pytest.raises(ValueError):
        store.set_room_members(db,room_id='r',event_id='reuse',members=MEMBERS[:2]+[dict(MEMBERS[2],member_id='other-c')])
    assert store.room_state(db,room_id='r')==before
    with pytest.raises(store.EventConflictError):
        store.set_room_members(db,room_id='r',event_id='remove',members=MEMBERS)
    restored=store.set_room_members(db,room_id='r',event_id='restore',members=MEMBERS)
    assert len(policy.validate_room(restored,local_profiles=PROFILES).active_members)==3
    assert len(restored['members'])==3


def test_attachment_survives_terminal_projection_publication_and_log_replay(room):
    db,raw=room;user(db,raw)
    ref={'artifact_id':'a'*32,'filename':'report.pdf','mime_type':'application/pdf','size_bytes':42}
    settle(db,raw,attachments=[ref])
    stored=next(e for e in events(db) if e['kind']=='message.member')
    assert stored['payload']['attachments']==[ref]
    # Re-reading SQLite and replaying must admit the optional reference shape.
    assert policy.plan_next_task(store.room_state(db,room_id='r'),events(db),local_profiles=PROFILES).status=='task'
    assert policy.validate_user_payload({'text':'reply','thread_id':'t','reply_to':stored['seq']})['reply_to']==stored['seq']


def test_invalid_attachment_drops_reference_without_losing_text(room):
    db,raw=room;user(db,raw)
    settle(db,raw,text='Preserve this reply',attachments=[{'artifact_id':'../secret'}])
    stored=next(e for e in events(db) if e['kind']=='message.member')
    assert stored['payload']['text']=='Preserve this reply'
    assert 'attachments' not in stored['payload']


def test_reply_reference_checked_in_this_room_before_any_append(room):
    db,raw=room;original=user(db,raw)
    service=HostedRoomService(SimpleNamespace(_methods={},_sessions={},_sessions_lock=threading.Lock()),db_path=db)
    # No RPC/model dispatch is started; this tests the real pre-append service gate.
    with pytest.raises((policy.DiscussionValidationError, store.HostedRoomError)):
        service.send(room_id='r',event_id='bad',payload={'text':'reply','thread_id':'t','reply_to':999})
    assert [e['event_id'] for e in events(db)]==[original['event_id']]
    # Stop after durable append by removing the transport binding (no real turn).
    service.bindings=lambda: ()
    with pytest.raises(store.RoomNotFoundError):
        service.send(room_id='r',event_id='good',payload={'text':'reply','thread_id':'t','reply_to':original['seq']})
    assert events(db)[-1]['payload']['reply_to']==original['seq']


def test_membership_service_preserves_upstream_owned_room_boundary(home):
    db=home/'state.db'
    store.create_room(db,room_id='foreign',name='Remote',members=MEMBERS,authority_gateway_id='other-host')
    service=HostedRoomService(SimpleNamespace(_methods={},_sessions={},_sessions_lock=threading.Lock()),db_path=db)
    with pytest.raises(store.AuthorityConflictError):
        service.set_members(room_id='foreign',event_id='no',members=MEMBERS[:2])
    assert len(store.room_state(db,room_id='foreign')['members'])==3


def test_registered_members_rpc_executes_native_service_and_returns_roster(room):
    from tui_gateway import methods_groups
    db,raw=room
    server=SimpleNamespace(_methods={},_sessions={},_sessions_lock=threading.Lock())
    service=HostedRoomService(server,db_path=db)
    server.get_hosted_room_service=lambda:service
    server._ok=lambda rid,result: {'id':rid,'result':result}
    server._err=lambda rid,code,message,data=None:{'id':rid,'error':{'code':code,'message':message}}
    methods_groups.register(server)
    assert 'groups.members' in methods_groups.LONG_HANDLERS
    answer=server._methods['groups.members'](1,{'room_id':'r','event_id':'native-rpc','members':MEMBERS[:2]})
    assert 'error' not in answer,answer
    result=answer['result']['room']
    assert len(result['members'])==3
    assert sum(m.get('active',True) for m in result['members'])==2
    # Same handler's unavailable-service branch remains a structured UI error.
    server.get_hosted_room_service=lambda:None
    assert server._methods['groups.members'](2,{})['error']['code']==4123
