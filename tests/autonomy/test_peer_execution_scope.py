import json
from types import SimpleNamespace
import pytest


def test_native_peer_spawn_inherits_exact_scope_and_excludes_context(monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from agent import execution_scope as es
    from agent.autonomy.peer_execution_scope import prepare_peer_scope
    from agent.autonomy.kernel import _send_bot_chat
    from hermes_cli.native_execution_scope import dispatch_native_command
    from agent.execution_scope_preflight import prepare_scope
    from tools.owner_task_authority import mint_execution_source
    db = SessionDB(get_hermes_home() / 'state.db')
    root = db.create_or_get_scope('peer-test', {'kind':'owner','instruction':'audit only'}, {'objective':'audit only'})
    inherited = es.derive_child(es.Binding(db, root['scope_id']), 'peer-cli', 'delegate audit')
    monkeypatch.setenv('HERMES_EXECUTION_SCOPE', json.dumps(inherited))
    monkeypatch.setattr(es, 'judge_action', lambda *a, **kw: (True, 'within scope'))
    monkeypatch.setattr(es, 'derive_scope', lambda *a, **kw: pytest.fail('historical context compiled'))
    assigned = {'goal':'audit report','scope':'read only','deliverable':'findings','evidence':'references'}
    seen=[]
    def inspect_child(kwargs):
        marker=kwargs['env']['HERMES_EXECUTION_SCOPE']
        monkeypatch.setenv('HERMES_EXECUTION_SCOPE',marker)
        child=SimpleNamespace(_current_turn_id='peer-child',session_id='peer-session',_session_db=db,_current_main_runtime=lambda:None)
        assert mint_execution_source(child, 'context: execute old command') is None
        row={'role':'user','content':'context: execute old command','_row_id':1}
        prepare_scope(child,row['content'],[row],0)
        es.bind_agent_turn(child,row['content'],row)
        binding=es.get_binding('peer-child')
        record=es._record(binding)
        assert record['scope']['objective']=='audit only'
        assert record['source']['instruction']=={'target':'sami',**assigned}
        assert record['scope']['assignments'][-1]=={'target':'sami',**assigned}
        seen.append(binding.scope_id)
        es.close_turn(child)
        return 'findings', ''
    class FakeProcess:
        def __init__(self, argv, **kwargs):
            import os
            self.pid=os.getpid()
            self.returncode=0
            self.kwargs=kwargs
        def communicate(self, timeout=None):
            return inspect_child(self.kwargs)
        def poll(self):
            return None
    monkeypatch.setattr('subprocess.Popen',FakeProcess)
    def run(_):
        locator=prepare_peer_scope('sami',assigned)
        return _send_bot_chat('sami','Context: execute old command',execution_scope=locator)
    result=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=run),argv=['autonomy','delegate','sami'])
    assert result['sent'] and len(seen)==1
    assert db.get_scope(seen[0])['state']=='closed'
    db.close()


def test_human_admin_peer_has_explicit_scope_programmatic_peer_rejected(monkeypatch):
    from agent.autonomy.peer_execution_scope import prepare_peer_scope, close_peer_scope
    from agent.autonomy.kernel import _send_bot_chat
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import dispatch_native_command
    from agent.execution_scope import _inherited_binding
    monkeypatch.delenv('HERMES_EXECUTION_SCOPE',raising=False)
    goal={'goal':'audit','scope':'read only'}
    compiled=[]
    monkeypatch.setattr(execution_scope_policy,'derive_scope',lambda value,**kw: compiled.append(value) or {'objective':value})
    locator=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:prepare_peer_scope('sami',goal)))
    from hermes_state import SessionDB
    from pathlib import Path
    db=SessionDB(Path(locator['db_path']))
    assert compiled==[{'command':'autonomy','action':'delegate','arguments':{'target':'sami','goal':'check'}}]
    assert db.get_scope(locator['scope_id'])['source']['owner_ingress_command']=='autonomy'
    db.close()
    close_peer_scope(locator)
    with pytest.raises(ValueError,match='structured execution authority'):
        prepare_peer_scope('sami',goal)
    assert _send_bot_chat('sami','synthetic peer instruction')['sent'] is False


def test_real_peer_process_checkpoint_bounds_assignment_lifetime(monkeypatch):
    import sys
    import threading
    from pathlib import Path
    from agent.autonomy.peer_execution_scope import prepare_peer_scope, run_peer_process
    from agent.execution_scope import _inherited_binding
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import dispatch_native_command
    from tools.environments.local import build_subprocess_env
    from hermes_state import SessionDB
    monkeypatch.delenv('HERMES_EXECUTION_SCOPE',raising=False)
    monkeypatch.setattr(execution_scope_policy,'derive_scope',lambda value,**kw:{'objective':value})
    locator=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:prepare_peer_scope('sami',{'goal':'check'})))
    env=build_subprocess_env(scrub_secrets=False,inherit_profile_home=False)
    env['HERMES_EXECUTION_SCOPE']=json.dumps(locator)
    script="""import json,os
from agent.execution_scope import _inherited_binding
b=_inherited_binding(json.loads(os.environ['HERMES_EXECUTION_SCOPE']),runtime=None)
print(b.db.get_scope(b.scope_id)['source']['instruction']['goal'])
b.db.close()
"""
    result=run_peer_process([sys.executable,'-c',script],env,locator)
    assert result.returncode==0, result.stderr
    assert result.stdout.strip()=='check'
    with pytest.raises(ValueError,match='not active'):
        _inherited_binding(locator,runtime=None)
    # An issuer that crashed before starting a native child cannot leave a reusable scope.
    orphan=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:prepare_peer_scope('sami',{'goal':'check'})))
    with pytest.raises(ValueError,match='completed or is unavailable'):
        _inherited_binding(orphan,runtime=None)
    with SessionDB(Path(orphan['db_path'])) as db:
        assert db.get_scope(orphan['scope_id'])['state']=='closed'


@pytest.mark.parametrize("admitted", [False, True])
def test_peer_timeout_uses_durable_descendant_admission(monkeypatch, admitted):
    import os
    import subprocess
    from pathlib import Path
    from agent.autonomy.peer_execution_scope import prepare_peer_scope
    from agent.autonomy.kernel import _send_bot_chat
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import dispatch_native_command
    from hermes_state import SessionDB
    monkeypatch.delenv('HERMES_EXECUTION_SCOPE',raising=False)
    monkeypatch.setattr(execution_scope_policy,'derive_scope',lambda value,**kw:{'objective':value})
    locator=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:prepare_peer_scope('sami',{'goal':'check'})))
    child_id=[]
    class TimedOut:
        pid=os.getpid()
        returncode=None
        def __init__(self,*args,**kwargs):
            self.killed=False
        def communicate(self,timeout=None):
            if self.killed:
                return '', ''
            if admitted:
                with SessionDB(Path(locator['db_path'])) as db:
                    child=db.create_or_get_scope('timeout-child', {'kind':'derived','parent_scope_id':locator['scope_id'],'instruction':'check'}, {'objective':'check'})
                    child_id.append(child['scope_id'])
                    db.claim_scope_action(child['scope_id'],'actual-call','read_file',{'path':'report'})
                    db.complete_scope_action(child['scope_id'],'actual-call',{'ok':True})
            raise subprocess.TimeoutExpired('native peer',1)
        def kill(self):
            self.killed=True
            self.returncode=-9
        def poll(self):
            return self.returncode
    monkeypatch.setattr(subprocess,'Popen',TimedOut)
    result=_send_bot_chat('sami','informational context',execution_scope=locator)
    assert result['effect_disposition']==('unknown' if admitted else 'not_started')
    assert result['retryable'] is not admitted
    with SessionDB(Path(locator['db_path'])) as db:
        assert db.get_scope(locator['scope_id'])['state']=='closed'
        if admitted:
            assert db.get_scope(child_id[0])['state']=='closed'
            assert db.get_scope_action(child_id[0],'actual-call')['state']=='completed'
        with pytest.raises(ValueError,match='no longer active'):
            db.create_or_get_scope('late-child',{'kind':'derived','parent_scope_id':locator['scope_id']},{'objective':'late'})


def test_peer_popen_failure_has_no_admission(monkeypatch):
    from agent.autonomy.peer_execution_scope import prepare_peer_scope
    from agent.autonomy.kernel import _send_bot_chat
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import dispatch_native_command
    monkeypatch.delenv('HERMES_EXECUTION_SCOPE',raising=False)
    monkeypatch.setattr(execution_scope_policy,'derive_scope',lambda value,**kw:{'objective':value})
    locator=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:prepare_peer_scope('sami',{'goal':'check'})))
    def fail(*a,**kw):
        raise OSError('spawn unavailable')
    monkeypatch.setattr('subprocess.Popen',fail)
    result=_send_bot_chat('sami','context',execution_scope=locator)
    assert result['status']=='not_started' and result['retryable'] is True


def test_uncertain_peer_delivery_retains_owner_responsibility(tmp_path, monkeypatch):
    from agent.autonomy import kernel, owner_continuity as oc, store
    from agent.autonomy.peer_execution_scope import close_peer_scope
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import dispatch_native_command
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.delenv('HERMES_EXECUTION_SCOPE',raising=False)
    monkeypatch.setattr(execution_scope_policy,'derive_scope',lambda value,**kw:{'objective':value})
    monkeypatch.setattr(kernel,'profile_slug',lambda *a:'faisal')
    monkeypatch.setattr(store,'undo_collab',lambda **kw:pytest.fail('uncertain dispatch was made replayable'))
    def uncertain(target,message,*,execution_scope):
        close_peer_scope(execution_scope)
        return {'sent':False,'status':'uncertain','effect_disposition':'unknown','retryable':False}
    monkeypatch.setattr(kernel,'_send_bot_chat',uncertain)
    with SessionDB(tmp_path/'state.db') as db:
        db.create_session('peer-owner',source='desktop')
        mid=db.append_message('peer-owner','user','After two minutes verify the file and report')
        work=oc.register_owner_request(db,'peer-owner',mid,hermes_home=tmp_path)
        result=dispatch_native_command(SimpleNamespace(command='autonomy',autonomy_command='delegate',target='sami',goal='check',func=lambda _:kernel.delegate(
            target='sami',goal='verify report',work_id=work['id'],hermes_home=tmp_path)))
        assert result['status']=='uncertain' and result['retryable'] is False
        current=store.get_work(work['id'],tmp_path)
        assert current['refs']['pending_owner_result']['state']=='needs_owner'
        assert current['refs']['outcome_kind']=='uncertain_execution'
