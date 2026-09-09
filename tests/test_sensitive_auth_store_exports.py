"""Native export surfaces refuse credential stores before copying or serving bytes."""
import base64
import pytest

from gateway import published_artifacts


def sensitive_names():
    for name in ('google_client_secret.json', 'auth_pool.json'):
        yield from (name, name.upper(), name + '.bak', name.upper() + '.BAK-copy', name + '~')
    yield from ('auth.json', 'google_token.json')


def test_native_publication_refuses_auth_stores_and_keeps_ordinary_artifacts(tmp_path):
    profile_home = tmp_path / 'profile'
    profile_home.mkdir()
    for name in sensitive_names():
        source = profile_home / name
        source.write_bytes(b'{"synthetic_test_fixture":true}')
        with pytest.raises(published_artifacts.PublishRefused) as failure:
            published_artifacts.publish(profile_home, source_path=str(source), profile='fixture',
                                        filename='innocent-report.json', session_id='fixture-session')
        assert failure.value.reason == 'refused', name
    assert not published_artifacts.store_dir(profile_home).exists(), 'refusal must precede copying/indexing'
    ordinary = profile_home / 'quarterly-report.json'
    ordinary.write_bytes(b'{"revenue":42}')
    record = published_artifacts.publish(profile_home, source_path=str(ordinary), profile='fixture',
                                        session_id='fixture-session')
    assert record.filename == ordinary.name
    blob, stored = published_artifacts.resolve_blob(profile_home, record.artifact_id)
    assert blob.read_bytes() == ordinary.read_bytes()
    assert stored.artifact_id == record.artifact_id


def test_native_managed_http_read_and_download_refuse_auth_stores(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_DASHBOARD_FILES_ROOT', str(tmp_path))
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    fixture_bytes = b'{"synthetic_test_fixture":true}'
    for name in sensitive_names():
        source = tmp_path / name
        source.write_bytes(fixture_bytes)
        for endpoint in ('read', 'download', 'stream'):
            response = client.get(f'/api/files/{endpoint}', params={'path': str(source)})
            assert response.status_code == 403, (name, endpoint, response.status_code)
            assert fixture_bytes not in response.content
    ordinary = tmp_path / 'quarterly-report.json'
    ordinary.write_bytes(b'{"revenue":42}')
    response = client.get('/api/files/read', params={'path': str(ordinary)})
    assert response.status_code == 200
    assert base64.b64decode(response.json()['data_url'].split(',', 1)[1]) == ordinary.read_bytes()
    response = client.get('/api/files/download', params={'path': str(ordinary)})
    assert response.status_code == 200
    assert response.content == ordinary.read_bytes()
    client.close()
