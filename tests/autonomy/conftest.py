from pathlib import Path

import pytest


@pytest.fixture
def autonomy_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "SOUL.md").write_text("# Badr\n\nEngineering owner.\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
