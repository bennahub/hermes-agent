import json
import types
from unittest.mock import patch

import pytest
from hermes_cli import google_gmail_verification as gmail


def test_receipt_requires_recent_exact_credentials(tmp_path):
    path=tmp_path/'google_token.json'
    payload={'token':'synthetic-only','refresh_token':'synthetic-refresh','scopes':gmail.SCOPES}
    payload[gmail.META]={'account':'owner@example.com','verified_at':100,
                         'credential_fingerprint':gmail.fingerprint(payload)}
    path.write_text(json.dumps(payload))
    assert gmail.receipt(path,101)=={'account':'owner@example.com','verified_at':100}
    assert gmail.receipt(path,100+gmail.MAX_AGE+1) is None
    assert gmail.receipt(path,99) is None
    payload['token']='changed-synthetic-token';path.write_text(json.dumps(payload))
    assert gmail.receipt(path,101) is None
    for raw in ['[]','{"_hermes_gmail_verification": []}','invalid']:
        path.write_text(raw);assert gmail.receipt(path,101) is None


def test_verify_refreshes_and_only_reads_gmail_identity_and_labels(tmp_path,monkeypatch):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    path=tmp_path/'google_token.json'
    original={'token':'synthetic-token','refresh_token':'synthetic-refresh',
              'client_id':'fixture.apps.googleusercontent.com','client_secret':'synthetic-secret',
              'scopes':gmail.SCOPES}
    path.write_text(json.dumps(original));calls=[]
    creds=Credentials.from_authorized_user_info(original)
    def refresh(self,request):
        calls.append('refresh');self.token='refreshed-synthetic'
    def get(self,url,**kwargs):
        calls.append(url)
        payload={'emailAddress':'owner@example.com'} if url.endswith('/profile') else {'labels':[{'id':'INBOX'}]}
        return types.SimpleNamespace(raise_for_status=lambda:None,json=lambda:payload)
    monkeypatch.setattr(Credentials,'refresh',refresh)
    monkeypatch.setattr(AuthorizedSession,'get',get)
    result=gmail.verify(path)
    assert result=={'ok':True,'detail_code':'google_verified','account':'owner@example.com',
                    'refresh_verified':True,'gmail_verified':True,'labels_count':1}
    assert calls==['refresh','https://gmail.googleapis.com/gmail/v1/users/me/profile',
                   'https://gmail.googleapis.com/gmail/v1/users/me/labels']
    assert path.stat().st_mode&0o777==0o600
    assert gmail.receipt(path)['account']=='owner@example.com'
    assert 'synthetic' not in json.dumps(result)
    # A failed refresh invalidates Connected; no stale receipt or exception text is published.
    monkeypatch.setattr(Credentials,'refresh',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic provider failure')))
    with pytest.raises(RuntimeError):gmail.verify(path)
    assert gmail.receipt(path) is None


def test_connections_inventory_requests_only_full_gmail_scopes(tmp_path,monkeypatch):
    from hermes_cli import google_workspace_onboarding as adapter
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr('importlib.metadata.version',lambda name: 'absent')
    info=adapter.describe(tmp_path)
    assert info['requested_scopes']==gmail.SCOPES
    assert info['verification'] is None


@pytest.mark.parametrize("stored,granted", [
    (["https://www.googleapis.com/auth/gmail.metadata"],None),
    (gmail.SCOPES,["https://www.googleapis.com/auth/gmail.metadata"]),
    (gmail.SCOPES,[]),
])
def test_insufficient_stored_or_refreshed_grant_never_connected(tmp_path,monkeypatch,stored,granted):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    path=tmp_path/'google_token.json'
    payload={'token':'synthetic','refresh_token':'synthetic','client_id':'fixture.apps.googleusercontent.com',
             'client_secret':'synthetic','scopes':stored}
    payload[gmail.META]={'account':'owner@example.com','verified_at':100,
                         'credential_fingerprint':gmail.fingerprint(payload)}
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(Credentials,'refresh',lambda self,request:setattr(self,'_granted_scopes',granted))
    monkeypatch.setattr(AuthorizedSession,'get',lambda *a,**kw:pytest.fail('insufficient grant reached Gmail'))
    with pytest.raises(ValueError,match='google_verification_failed'):gmail.verify(path)
    assert gmail.receipt(path) is None
