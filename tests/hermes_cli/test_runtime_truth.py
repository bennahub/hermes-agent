"""Canonical runtime status must come from the hosted release, not cwd git."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.runtime_truth import collect_canonical_runtime


def _reset_root_memo(monkeypatch, root: Path) -> None:
    import hermes_constants

    monkeypatch.setenv("HERMES_HOME", str(root))
    hermes_constants._default_hermes_root_memo = None


def test_hosted_release_sha_is_not_cwd_git(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    release = root / "releases" / "20260909-aaaaaaaa"
    release.mkdir(parents=True)
    sha = "a" * 40
    (release / "RUNTIME_IDENTITY").write_text(f"sha={sha}\n", encoding="utf-8")
    (root / "releases" / "current").symlink_to(release)
    (root / "config.yaml").write_text("_config_version: 40\nmodel:\n  default: x\n", encoding="utf-8")
    (root / "profiles" / "faisal").mkdir(parents=True)
    (root / "profiles" / "majed").mkdir(parents=True)
    (root / "profiles" / "faisal" / "config.yaml").write_text("_config_version: 40\n", encoding="utf-8")
    (root / "profiles" / "majed" / "config.yaml").write_text("_config_version: 40\n", encoding="utf-8")
    (root / "cron").mkdir()
    (root / "cron" / "ticker_heartbeat").write_text("9999999999\n", encoding="utf-8")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-test\n", encoding="utf-8")
    _reset_root_memo(monkeypatch, root)

    payload = collect_canonical_runtime(persist=False, root=root)

    assert payload["canonical"] is True
    assert payload["source"] == "hosted-release"
    assert payload["sha"] == sha
    assert payload["release_path"] == str(release.resolve())
    assert payload["profile_schema"]["default"] == 40
    assert payload["profile_schema"]["faisal"] == 40
    assert payload["profile_schema"]["majed"] == 40
    assert payload["profiles_consistent"] is True
    assert payload["providers_ready"] is True
    assert payload["scheduler"] == "healthy"


def test_missing_hosted_release_is_not_local_truth(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("_config_version: 39\n", encoding="utf-8")
    _reset_root_memo(monkeypatch, root)

    payload = collect_canonical_runtime(persist=False, root=root)

    assert payload["canonical"] is False
    assert payload["source"] == "not_canonical"
    assert payload["sha"] is None
    assert payload["gateway"] == "unknown"
    assert payload["serve"] == "unknown"


def test_runtime_identity_beats_git(tmp_path, monkeypatch):
    from hermes_cli import build_info

    stamp = tmp_path / "RUNTIME_IDENTITY"
    stamp.write_text("b" * 40, encoding="utf-8")
    monkeypatch.setattr(build_info, "_RUNTIME_IDENTITY_FILE", stamp)
    monkeypatch.setattr(build_info, "_BUILD_SHA_FILE", tmp_path / "missing")
    try:
        identity = build_info.get_code_identity(refresh=True)
        assert identity["sha"] == "b" * 40
        assert identity["source"] == "runtime-identity"
    finally:
        build_info._code_identity_cache = None
