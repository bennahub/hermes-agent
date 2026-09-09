"""Real native FTS projection, authenticated routing, and profile file boundaries."""
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_native_file_search_http_profile_and_readonly_contract(tmp_path, monkeypatch):
    from hermes_cli import profiles, web_server
    from hermes_state import SessionDB
    root = tmp_path / 'hermes'
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(profiles, '_get_default_hermes_home', lambda: root)
    stores = {}
    for name in ('alpha', 'beta'):
        home = root / 'profiles' / name
        (home / 'attachments').mkdir(parents=True)
        paths = [home / 'attachments' / f'needle-{name}-{n}.txt' for n in range(2)]
        for p in paths:
            p.write_text('synthetic attachment')
        db = SessionDB(home / 'state.db')
        db.create_session(name, source='desktop', profile_name=name, cwd=str(root.parent))
        ids = [db.append_message(name, 'user', f'@file:{p.relative_to(root.parent)}', timestamp=100+n) for n,p in enumerate(paths)]
        db.append_message(name, 'assistant', f'@file:{home / "attachments" / "needle-assistant.txt"}', timestamp=200)
        db.close()
        stores[name] = (home, paths, ids)
    # Native SQLite files must not change merely because a client searches.
    before = {name: hashlib.sha256((row[0]/'state.db').read_bytes()).hexdigest() for name,row in stores.items()}
    client = TestClient(web_server.app)
    assert client.get('/api/sessions/search', params={'q':'needle','kind':'file','profile':'alpha'}).status_code in (401,403)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    params = {'q':'needle', 'kind':'file', 'profile':'alpha', 'limit':1}
    response = client.get('/api/sessions/search',params=params)
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['kind']=='file' and first['has_more'] is True
    assert [r['path'] for r in first['results']]==[str(stores['alpha'][1][1])]
    second = client.get('/api/sessions/search',params=dict(params,offset=1)).json()
    assert second['has_more'] is False
    assert second['results'][0]['message_id']==stores['alpha'][2][0]
    beta = client.get('/api/sessions/search',params=dict(params,profile='beta')).json()
    assert beta['results'][0]['session_id']=='beta'
    assert client.get('/api/sessions/search',params=dict(params,exclude_sources='desktop')).json()['results']==[]
    assert client.get('/api/sessions/search',params=dict(params,kind='unknown')).status_code==400
    for name,(home,_,_) in stores.items():
        assert hashlib.sha256((home/'state.db').read_bytes()).hexdigest()==before[name]


def test_native_file_projection_rejects_sensitive_and_cross_profile_refs(tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.session_file_search import search_profile_files
    home = tmp_path/'alpha'
    attachments = home/'attachments'
    attachments.mkdir(parents=True)
    outside = tmp_path/'beta'/'attachments'
    outside.mkdir(parents=True)
    secret = outside/'needle-private.txt'
    secret.write_text('never expose')
    (attachments/'needle-link.txt').symlink_to(secret)
    (attachments/'google_client_secret.json').write_text('{"synthetic":true}')
    (attachments/'auth_pool.json').write_text('{"synthetic":true}')
    safe = attachments/'needle-visible.txt'
    safe.write_text('safe')
    missing = attachments/'needle-missing.txt'
    db = SessionDB(home/'state.db')
    db.create_session('qa', source='desktop')
    refs = [safe,missing,secret,attachments/'needle-link.txt',attachments/'google_client_secret.json',attachments/'auth_pool.json']
    db.append_message('qa','user',[{'type':'text','text':'\n'.join(f'@file:{p}' for p in refs)}],timestamp=100)
    db.close()
    db = SessionDB(home/'state.db',read_only=True)
    try:
        search=lambda q:search_profile_files(db,q,limit=20,offset=0,include_sources=None,exclude_sources=[])
        rows=search('needle')['results']
        assert {r['path'] for r in rows}=={str(safe),str(missing)}
        assert next(r for r in rows if r['path']==str(missing))['exists'] is False
        assert search('google_client_secret')['results']==[]
        assert search('auth_pool')['results']==[]
        # Even an entire attachment root redirected to another profile is denied.
        renamed=home/'old-attachments'
        attachments.rename(renamed)
        attachments.symlink_to(outside, target_is_directory=True)
        assert search('needle')['results']==[]
        # Default HOME nests named profiles, so base containment alone is not
        # a scope boundary. Both nested profile and home-wide aliases deny.
        nested = home/'profiles'/'other'/'attachments'
        nested.mkdir(parents=True)
        (nested/'needle-private.txt').write_text('synthetic other profile')
        attachments.unlink()
        attachments.symlink_to(nested, target_is_directory=True)
        from hermes_cli.session_file_search import attachment_roots
        assert attachments.resolve() not in attachment_roots(home)
        assert search('needle')['results']==[]
        attachments.unlink()
        attachments.symlink_to(home, target_is_directory=True)
        assert home not in attachment_roots(home)
    finally:
        db.close()
