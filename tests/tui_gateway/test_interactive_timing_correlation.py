"""Content-free exact request attribution; no model/provider or secret logging."""
import hashlib
import json
import logging
from types import SimpleNamespace
from uuid import uuid4
from tui_gateway.interactive_progress import OwnerProgressWatch, record
from agent.interactive_timing import emit_stage


class Schedule:
    def __call__(self,*args):return self
    def cancel(self):pass


def test_native_callback_log_binds_exact_request_and_counts_requests_without_content(caplog):
    secret='private-session-title-or-credential'
    agent=SimpleNamespace(session_id=secret)
    sid='private-ui-session';message_id=str(uuid4());session={}
    watch=OwnerProgressWatch(session,lambda *a:None,lambda:False,scheduler=Schedule(),
        ui_session_id=sid,client_message_id=message_id)
    try:
        watch.bind_agent(agent)
        record(session,'message.start',{})
        for _ in range(2):
            emit_stage(agent,'MODEL_REQUEST_START');emit_stage(agent,'MODEL_REQUEST_END')
        with caplog.at_level(logging.INFO,logger='tui_gateway.interactive_progress'):
            record(session,'message.complete',{'text':'private prompt and response'})
        line=next(r for r in caplog.records if 'interactive_turn_timings' in r.message)
        correlation=json.loads(line.args[2])
        assert correlation=={'ui_session_sha256':hashlib.sha256(sid.encode()).hexdigest(),
            'canonical_session_sha256':hashlib.sha256(secret.encode()).hexdigest(),'client_message_id':message_id}
        assert line.args[3]==2
        assert secret not in line.message and sid not in line.message and 'private prompt' not in line.message
    finally:watch.close()


def test_invalid_identifier_content_and_later_turn_never_reuse_prior_join(caplog):
    session={};agent=SimpleNamespace(session_id='canonical')
    first=OwnerProgressWatch(session,lambda *a:None,lambda:False,scheduler=Schedule(),ui_session_id='one',client_message_id=str(uuid4()))
    first.bind_agent(agent);first.close()
    second=OwnerProgressWatch(session,lambda *a:None,lambda:False,scheduler=Schedule(),ui_session_id='two',client_message_id='private malformed content')
    try:
        second.bind_agent(agent)
        first.record_stage('MODEL_REQUEST_START',first.clock())
        with caplog.at_level(logging.INFO,logger='tui_gateway.interactive_progress'):
            record(session,'message.complete',{})
        line=next(r for r in caplog.records if 'interactive_turn_timings' in r.message)
        correlation=json.loads(line.args[2])
        assert correlation['client_message_id'] is None and 'private malformed content' not in line.message
        assert correlation['ui_session_sha256']!=first.state['correlation']['ui_session_sha256']
        assert line.args[3]==0
    finally:second.close()
