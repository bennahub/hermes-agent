"""Native HTTP -> isolated skill writer and in-chat discovery contracts."""
import json
import hashlib
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def surface(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    profile = home / 'profiles' / 'review-agent'
    profile.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}\n')
    (profile / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home))
    from hermes_cli.web_routers.connections import router
    from hermes_cli import web_server
    app = FastAPI()
    app.state.auth_required = False
    app.include_router(router)
    client = TestClient(app)
    yield client, home, profile, {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    client.close()
    reset_hermes_home_override(token)


def test_private_json_submission_uses_native_scoped_store_and_rejects_untrusted_input(surface):
    client, home, profile, headers = surface
    credential = {'installed': {'client_id':'synthetic.apps.googleusercontent.com',
        'client_secret':'synthetic-secret-never-returned',
        'auth_uri':'https://accounts.google.com/o/oauth2/auth',
        'token_uri':'https://oauth2.googleapis.com/token'}}
    data = {'kind':'integration', 'id':'google-workspace', 'scope':'review-agent',
            'action':'save_credential', 'field_id':'google_client_secret_json',
            'value':json.dumps(credential, indent=2)}
    assert client.post('/api/connections/actions', json=data).status_code == 401
    assert not (profile/'google_client_secret.json').exists()
    response = client.post('/api/connections/actions', json=data, headers=headers)
    assert response.status_code == 200 and response.json()['ok'] is True
    assert 'synthetic-secret' not in response.text
    assert json.loads((profile/'google_client_secret.json').read_text()) == credential
    assert (profile/'google_client_secret.json').stat().st_mode & 0o777 == 0o600
    assert not (home/'google_client_secret.json').exists()
    # No arbitrary file/env writer and no caller-supplied authenticated identity.
    for changed in ({'field_id':'GH_TOKEN'}, {'owner_id':'other-owner'}):
        rejected = client.post('/api/connections/actions', json={**data, **changed}, headers=headers)
        assert rejected.status_code == 400 or rejected.json()['ok'] is False
    assert not (profile/'.env').exists()
    # Recovering a consumed exchange after worker loss must not invite a replay
    # or present it as an ordinary "waiting for sign-in" operation.
    pending = {'_hermes_operation':{'id':'synthetic-operation',
        'owner':hashlib.sha256(b'local-dashboard').hexdigest(), 'scope':'review-agent',
        'expires_at':time.time()+300, 'status':'exchanging'}}
    (profile/'google_oauth_pending.json').write_text(json.dumps(pending))
    recovered = client.post('/api/connections/actions', headers=headers, json={
        'kind':'integration','id':'google-workspace','scope':'review-agent',
        'action':'auth_poll','session_id':'synthetic-operation'})
    assert recovered.json()['auth']['status'] == 'error'
    assert recovered.json()['detail_code'] == 'google_exchange_incomplete'
    assert recovered.json()['ok'] is False


def test_gmail_discovery_card_and_missing_client_use_real_inventory_without_auth_side_effects(surface, monkeypatch):
    client, home, profile, headers = surface
    from tools import connection_request_tool as cards
    monkeypatch.setattr(cards, '_profile', lambda: 'review-agent')
    listing = json.loads(cards.list_connections(query='Gmail'))
    assert listing['ok'] and listing['total'] == 1
    row = listing['entries'][0]
    assert row['id'] == 'google-workspace' and row['scope'] == 'review-agent'
    assert row['state'] == 'needs_configuration' and row['detail_code'] == 'google_client_required'
    assert row['scope_kind'] == 'PROFILE_SPECIFIC'
    assert row['actions'] == ['save_credential']
    card = json.loads(cards.request_connection('integration', row['id'], 'Connect Gmail privately'))
    assert card['connection_request']['scope'] == 'review-agent'
    assert 'value' not in card and 'verification_uri' not in card
    response = client.post('/api/connections/actions', headers=headers,
        json={'kind':'integration','id':row['id'],'scope':'review-agent','action':'connect'})
    assert response.json() == {'ok':False,'lifecycle':'immediate','detail_code':'google_client_required'}
    assert not (profile/'google_oauth_pending.json').exists()
    assert not (profile/'google_token.json').exists()


def test_gmail_connected_requires_verified_scoped_credential_receipt(surface, monkeypatch):
    client, home, profile, headers = surface
    from hermes_cli import google_gmail_verification as gmail
    payload={'token':'synthetic-token','refresh_token':'synthetic-refresh','scopes':gmail.SCOPES}
    target=profile/'google_token.json';target.write_text(json.dumps(payload))
    def entry():
        data=client.get('/api/connections',headers=headers).json()
        return next(x for x in data['entries'] if x['id']=='google-workspace' and x['scope']=='review-agent')
    assert entry()['state']=='configured'
    payload[gmail.META]={'account':'owner@example.com','verified_at':time.time(),
                        'credential_fingerprint':gmail.fingerprint(payload)}
    target.write_text(json.dumps(payload))
    row=entry();assert row['state']=='connected' and row['detail_code']=='google_verified'
    assert 'synthetic-token' not in json.dumps(row)
    assert not (home/'google_token.json').exists()
    payload['token']='replaced-synthetic';target.write_text(json.dumps(payload))
    assert entry()['state']=='configured'


def test_gmail_test_action_requires_owner_auth_and_returns_only_safe_proof(surface, monkeypatch):
    client, home, profile, headers = surface
    from hermes_cli import google_workspace_onboarding as adapter
    calls=[]
    def verify(scope,owner,action,**kwargs):
        calls.append((scope,owner,action))
        return {'ok':True,'account':'owner@example.com','refresh_verified':True,'gmail_verified':True}
    monkeypatch.setattr(adapter,'run_operation',verify)
    data={'kind':'integration','id':'google-workspace','scope':'review-agent','action':'test'}
    assert client.post('/api/connections/actions',json=data).status_code==401
    assert calls==[]
    response=client.post('/api/connections/actions',json=data,headers=headers)
    body=response.json();assert body.pop('last_checked_at') > 0
    assert body=={'ok':True,'lifecycle':'immediate','detail_code':'check_passed','state':'connected',
                             'account':'owner@example.com','refresh_verified':True,'gmail_verified':True}
    assert calls==[('review-agent','local-dashboard','test')]


def test_desktop_loopback_port_is_private_google_connect_only(surface,monkeypatch):
    client,home,profile,headers=surface
    from hermes_cli import google_workspace_onboarding as adapter
    calls=[]
    def operation(scope,owner,action,**kwargs):
        calls.append(kwargs);return {'ok':False,'error':'google_client_required'}
    monkeypatch.setattr(adapter,'run_operation',operation)
    data={'kind':'integration','id':'google-workspace','scope':'review-agent','action':'connect','loopback_port':43217}
    assert client.post('/api/connections/actions',json=data).status_code==401
    assert calls==[]
    client.post('/api/connections/actions',json=data,headers=headers)
    assert calls[-1]['loopback_port']==43217
    count=len(calls)
    for invalid in [{**data,'loopback_port':True},{**data,'loopback_port':1},
                    {**data,'loopback_port':65536},{**data,'action':'test'},
                    {**data,'id':'other'}]:
        response=client.post('/api/connections/actions',json=invalid,headers=headers)
        assert response.status_code==400
    assert len(calls)==count


def test_gmail_failed_check_replaces_success_in_native_client_contract(surface,monkeypatch,tmp_path):
    client,home,profile,headers=surface
    from hermes_cli import google_workspace_onboarding as adapter
    outcomes=iter([{'ok':True,'account':'owner@example.com','refresh_verified':True,'gmail_verified':True},
                   {'ok':False,'error':'google_verification_failed'},
                   {'ok':False,'error':'google_operation_busy'}])
    monkeypatch.setattr(adapter,'run_operation',lambda *a,**kw:next(outcomes))
    data={'kind':'integration','id':'google-workspace','scope':'review-agent','action':'test'}
    passed=client.post('/api/connections/actions',json=data,headers=headers).json()
    failed=client.post('/api/connections/actions',json=data,headers=headers).json()
    busy=client.post('/api/connections/actions',json=data,headers=headers).json()
    assert passed['ok'] is True and passed['detail_code']=='check_passed' and passed['state']=='connected'
    assert failed['ok'] is False and failed['detail_code']=='check_failed' and failed['state']=='error'
    assert failed['last_checked_at'] >= passed['last_checked_at'] > 0
    assert failed['check_detail_code']=='google_verification_failed'
    assert 'account' not in failed and 'refresh_verified' not in failed and 'gmail_verified' not in failed
    assert busy=={'ok':False,'lifecycle':'immediate','detail_code':'google_operation_busy'}
    # Exact HTTP payloads are also consumed by the external unchanged Swift-client proof.
    (tmp_path/'gmail-check-responses.json').write_text(json.dumps([passed,failed,busy]))


def test_fourteen_agents_discover_root_account_with_local_precedence(surface,monkeypatch):
    client,home,profile,headers=surface
    from hermes_cli import google_gmail_verification as gmail
    payload={'token':'synthetic-shared','refresh_token':'synthetic','scopes':gmail.SCOPES}
    payload[gmail.META]={'account':'owner@example.com','verified_at':time.time(),
                        'credential_fingerprint':gmail.fingerprint(payload)}
    (home/'google_token.json').write_text(json.dumps(payload))
    for i in range(13):
        p=home/'profiles'/f'shared-{i}';p.mkdir();(p/'config.yaml').write_text('{}')
    rows=[e for e in client.get('/api/connections',headers=headers).json()['entries'] if e['id']=='google-workspace']
    assert len(rows)==15  # default installation plus all 14 named agents
    assert all(e['scope_kind']=='GLOBAL_SHARED' and e['state']=='connected' for e in rows)
    local={**payload,'token':'local-readonly','scopes':['https://www.googleapis.com/auth/gmail.readonly']}
    (profile/'google_token.json').write_text(json.dumps(local))
    rows=[e for e in client.get('/api/connections',headers=headers).json()['entries'] if e['id']=='google-workspace']
    override=next(e for e in rows if e['scope']=='review-agent')
    assert override['scope_kind']=='PROFILE_SPECIFIC' and override['state']=='configured'
    assert all(e['state']=='connected' for e in rows if e['scope']!='review-agent')
    assert len(list((home/'profiles').glob('*/google_token.json')))==1
