"""Shared Gmail setup never requests protected master-store sandbox mounts."""
from pathlib import Path


def test_native_skill_readiness_does_not_require_or_export_protected_google_files(tmp_path,monkeypatch):
    from agent.skill_utils import parse_frontmatter
    from tools.credential_files import clear_credential_files,register_credential_files,get_credential_file_mounts
    source=Path(__file__).resolve().parents[2]/'skills/productivity/google-workspace/SKILL.md'
    fm,_=parse_frontmatter(source.read_text())
    root=tmp_path/'.hermes';root.mkdir()
    monkeypatch.setenv('HERMES_HOME',str(root))
    for name in ['google_token.json','google_client_secret.json']:(root/name).write_text('{}')
    clear_credential_files()
    try:
        # Readiness succeeds without declaring forbidden copies into a sandbox.
        assert register_credential_files(fm.get('required_credential_files',[]))==[]
        assert get_credential_file_mounts()==[]
    finally:clear_credential_files()
