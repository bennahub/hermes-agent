"""Native upstream ports: real profile paths, contextvars, grants and file reads."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json
import os
import pytest
import hermes_constants
from agent import file_safety, secret_scope
from tools import owner_task_authority as authority, file_tools_write_guards as guards
from tools.approval_context import set_current_observability_context, reset_current_observability_context

@pytest.fixture
def homes(tmp_path,monkeypatch):
    root=tmp_path/'hermes'; a=root/'profiles'/'a'; b=root/'profiles'/'b'
    for home in (root,a,b):
        home.mkdir(parents=True,exist_ok=True)
        (home/'config.yaml').write_text('credentials:\n  inherit_process_env: false\n')
        (home/'SOUL.md').write_text('charter')
    monkeypatch.setenv('HERMES_HOME',str(root))
    token=hermes_constants.set_hermes_home_override(a)
    authority.clear_all();guards.clear_task_instruction_authority()
    yield root,a,b
    hermes_constants.reset_hermes_home_override(token)
    authority.clear_all();guards.clear_task_instruction_authority()


def test_profile_secret_scope_and_daemon_context_are_isolated(homes,monkeypatch):
    from tools.daemon_pool import DaemonThreadPoolExecutor
    root,a,b=homes
    monkeypatch.setenv('GITHUB_TOKEN','process-only-test')
    (a/'.env').write_text('ANTHROPIC_API_KEY=test-a\n')
    (b/'.env').write_text('ANTHROPIC_API_KEY=test-b\n')
    secret_scope.set_multiplex_active(False)
    with DaemonThreadPoolExecutor(max_workers=1) as pool:
        for home,expected in [(a,'test-a'),(b,'test-b')]:
            ht=hermes_constants.set_hermes_home_override(home)
            st=secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(home))
            try:
                assert pool.submit(secret_scope.get_secret,'ANTHROPIC_API_KEY').result()==expected
                assert pool.submit(secret_scope.get_secret,'GITHUB_TOKEN').result() is None
                assert pool.submit(hermes_constants.get_hermes_home).result()==home
            finally:
                secret_scope.reset_secret_scope(st);hermes_constants.reset_hermes_home_override(ht)


def test_deployment_signing_secrets_never_resolve_or_reach_subprocess(homes,monkeypatch):
    from tools.environments.local import hermes_subprocess_env
    keys=['HERMES_DASHBOARD_BASIC_AUTH_SECRET','HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH',
          'HERMES_DASHBOARD_BASIC_AUTH_USERNAME','SENTRY_ACCESS_TOKEN']
    for key in keys: monkeypatch.setenv(key,'test-secret')
    st=secret_scope.set_secret_scope({key:'test-scoped' for key in keys})
    try:
        for key in keys: assert secret_scope.get_secret(key) is None
        child=hermes_subprocess_env()
        for key in keys: assert key not in child
    finally: secret_scope.reset_secret_scope(st)


def test_config_hard_block_tracks_actual_profile_context(homes):
    root,a,b=homes
    guards.reset_hermes_config_cache();guards.reset_real_hermes_home_cache()
    for home in (a,b,a):
        token=hermes_constants.set_hermes_home_override(home)
        try:
            assert guards._get_real_hermes_home()==str(home.resolve())
            assert guards._check_sensitive_path(str(home/'config.yaml')) is not None
            assert guards._protected_instruction_reason(str(home/'SOUL.md')) is not None
        finally: hermes_constants.reset_hermes_home_override(token)


def test_browser_identity_read_block_includes_root_and_profile_and_symlinks(homes,tmp_path):
    root,a,b=homes
    for home in (root,a):
        path=home/'agent-computers'/'identity'/'Cookies';path.parent.mkdir(parents=True);path.write_text('test')
        assert file_safety.get_read_block_error(str(path))
        link=tmp_path/(home.name+'-link');link.symlink_to(path)
        assert file_safety.get_read_block_error(str(link))


def test_context_file_exception_does_not_become_folder_or_symlink_permission(homes,tmp_path):
    from agent.context_references import preprocess_context_references
    root,a,b=homes
    outside=tmp_path/'outside';outside.mkdir();f=outside/'memo.txt';f.write_text('approved file content')
    kwargs=dict(cwd=a,context_length=32000,allowed_root=a,allowed_file_paths=(f.resolve(),))
    result=preprocess_context_references('@file:'+str(f),**kwargs)
    assert 'approved file content' in result.message
    assert preprocess_context_references('@folder:'+str(outside),**kwargs).warnings
    target=outside/'other.txt';target.write_text('not approved')
    f.unlink();f.symlink_to(target)
    result=preprocess_context_references('@file:'+str(f),**kwargs)
    assert result.warnings and 'not approved' not in result.message


def test_native_nonce_bind_queue_and_terminal_cleanup(homes):
    from agent.turn_context import _bind_owner_task_authority
    from agent.agent_runtime_helpers import clear_turn_authority
    root,a,b=homes
    n1=authority.mint_pending(['s'],profile_home=str(a));n2=authority.mint_pending(['s'],profile_home=str(a))
    agent=SimpleNamespace(session_id='s',_pending_owner_task_nonce=n1,_current_turn_id='s:t1')
    _bind_owner_task_authority(agent,'s:t1')
    assert authority.turn_authority('s:t1') is not None
    assert agent._pending_owner_task_nonce is None
    guards._grant_task_authority('s','s:t1',[str(a/'SOUL.md')])
    clear_turn_authority(agent)
    assert authority.turn_authority('s:t1') is None
    assert not guards._task_authority_covers('s','s:t1',[str(a/'SOUL.md')])
    agent._pending_owner_task_nonce=n2
    _bind_owner_task_authority(agent,'s:t2')
    assert authority.turn_authority('s:t2') is not None


@pytest.mark.parametrize('kind,persist_disabled',[('auto_continue',False),(None,True)])
def test_synthesized_or_review_turn_cannot_claim_owner_nonce(homes,kind,persist_disabled):
    from agent.turn_context import _bind_owner_task_authority
    root,a,b=homes
    nonce=authority.mint_pending(['s'],profile_home=str(a))
    agent=SimpleNamespace(session_id='s',_pending_owner_task_nonce=nonce,_persist_disabled=persist_disabled)
    _bind_owner_task_authority(agent,'s:t',kind)
    assert authority.turn_authority('s:t') is None
    assert agent._pending_owner_task_nonce is None


def test_delegated_context_cannot_claim_nonce(homes):
    from agent.delegation_context import delegated_child_context
    root,a,b=homes
    nonce=authority.mint_pending(['s'],profile_home=str(a))
    with delegated_child_context('child'):
        assert not authority.bind_turn(['s'],'t',nonce=nonce)
    assert authority.bind_turn(['s'],'t',nonce=nonce)

@pytest.mark.parametrize('fail',[False,True])
def test_actual_conversation_entrypoint_clears_grants_on_return_or_exception(homes,monkeypatch,fail):
    from agent import conversation_loop
    root,a,b=homes
    nonce=authority.mint_pending(['s'],profile_home=str(a))
    assert authority.bind_turn(['s'],'s:terminal',nonce=nonce)
    agent=SimpleNamespace(session_id='s',_current_turn_id='s:terminal')
    guards._grant_task_authority('s','s:terminal',[str(a/'SOUL.md')])
    def stop(*args,**kwargs):
        if fail: raise RuntimeError('provider failed before finalize')
        return {'failed':False}
    monkeypatch.setattr(conversation_loop,'_run_conversation_with_authority',stop)
    if fail:
        with pytest.raises(RuntimeError): conversation_loop.run_conversation(agent,'owner task')
    else: assert conversation_loop.run_conversation(agent,'owner task')=={'failed':False}
    assert authority.turn_authority('s:terminal') is None
    assert not guards._task_authority_covers('s','s:terminal',[str(a/'SOUL.md')])
