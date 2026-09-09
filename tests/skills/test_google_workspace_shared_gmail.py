"""Real standalone resolver and Gmail discovery dispatch, with synthetic transport."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPTS=Path(__file__).resolve().parents[2]/'skills/productivity/google-workspace/scripts'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_fourteen_profiles_share_one_store_with_confined_local_override(tmp_path,monkeypatch):
    root=tmp_path/'.hermes';root.mkdir()
    token=root/'google_token.json';token.write_text(json.dumps({'scopes':['https://mail.google.com/']}))
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('HERMES_HOME',str(root))
    store=load('shared_google_credentials',SCRIPTS/'_google_credentials.py')
    profiles=[]
    for i in range(14):
        home=root/'profiles'/f'agent-{i}';home.mkdir(parents=True);profiles.append(home)
        assert store.token_path(home,root)==token
    local=profiles[0]/'google_token.json';local.write_text('{"scopes":["https://www.googleapis.com/auth/gmail.readonly"]}')
    assert store.token_path(profiles[0],root)==local
    # Execute the actual CLI outside the repository, no core package/PYTHONPATH.
    env={**os.environ,'HERMES_HOME':str(profiles[1])};env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,str(SCRIPTS/'google_api.py'),'gmail','auth-status'],cwd=tmp_path,env=env,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'credential_present':True,'scope_kind':'GLOBAL_SHARED','granted_scopes':['https://mail.google.com/'],'account_verified':False}
    assert list((root/'profiles').glob('*/google_token.json'))==[local]
    foreign=tmp_path/'foreign';foreign.mkdir();(foreign/'google_token.json').write_text('{}')
    link=root/'profiles'/'linked';link.symlink_to(foreign,target_is_directory=True)
    for home in [foreign,link]:
        with pytest.raises(ValueError):store.token_path(home,root)
    tomb=root/'profiles/.deleted';tomb.mkdir();(tomb/'agent-0').write_text('deleted')
    with pytest.raises(ValueError):store.token_path(profiles[0],root)
    # A symlinked profiles ancestor cannot bypass confinement with a local token.
    other=tmp_path/'other';other.mkdir();(other/'profiles').symlink_to(root/'profiles',target_is_directory=True)
    with pytest.raises(ValueError):store.token_path(other/'profiles/agent-0',other)


def test_native_gmail_discovery_requests_and_transport_boundary(tmp_path,monkeypatch,capsys):
    from googleapiclient.discovery import build_from_document
    from googleapiclient.discovery_cache import get_static_doc
    from googleapiclient.http import HttpMockSequence
    root=tmp_path/'.hermes';root.mkdir();monkeypatch.setenv('HERMES_HOME',str(root))
    api=load('native_gmail_full',SCRIPTS/'google_api.py')
    calls=[]
    responses=[({'status':'200','content-type':'application/json'},b'{}') for _ in range(9)]
    responses[5]=({'status':'204'},b'')  # Native messages.delete has no response body.
    http=HttpMockSequence(responses)
    native_request=http.request
    def request(uri,method='GET',body=None,headers=None,**kwargs):
        calls.append((uri,method,body));return native_request(uri,method=method,body=body,headers=headers,**kwargs)
    http.request=request
    service=build_from_document(get_static_doc('gmail','v1'),http=http)
    monkeypatch.setattr(api,'build_service',lambda name,version:service if (name,version)==('gmail','v1') else pytest.fail('foreign service'))
    operations=[('drafts','create',{}, {'message':{'raw':'c3ludGhldGlj'}}),
        ('drafts','send',{}, {'id':'synthetic-draft'}),
        ('messages','send',{}, {'raw':'c3ludGhldGlj'}),
        ('messages','trash',{'id':'synthetic-message'},None),
        ('messages','untrash',{'id':'synthetic-message'},None),
        ('messages','delete',{'id':'synthetic-message'},None),
        ('labels','create',{}, {'name':'Synthetic label'}),
        ('settings.filters','create',{}, {'criteria':{'from':'synthetic@example.invalid'},'action':{'addLabelIds':['INBOX']}}),
        ('settings','updateVacation',{}, {'enableAutoReply':False})]
    for resource,method,params,body in operations:
        api.gmail_api(SimpleNamespace(resource=resource,method=method,params=json.dumps(params),body=json.dumps(body) if body else None))
    assert len(calls)==9
    assert all(uri.startswith('https://gmail.googleapis.com/gmail/v1/users/me/') for uri,_,_ in calls)
    assert [method for _,method,_ in calls]==['POST','POST','POST','POST','POST','DELETE','POST','POST','PUT']
    assert json.loads(calls[0][2])==operations[0][3]
    # Reject arbitrary service paths, Workspace admin-only methods, foreign
    # account and SDK transport kwargs before any provider operation.
    for resource,method,params,body in [('drive','list',{},None),('settings.forwardingAddresses','create',{},{}),('messages','get',{'userId':'other@example.invalid','id':'x'},None),('messages','get',{'id':'x','headers':{'Authorization':'x'}},None),('messages','get',{'id':'x','access_token':'x'},None),('messages','delete',{'id':'x'},{'anything':'x'})]:
        with pytest.raises(ValueError):api.gmail_api(SimpleNamespace(resource=resource,method=method,params=json.dumps(params),body=json.dumps(body) if body is not None else None))
    assert len(calls)==9
