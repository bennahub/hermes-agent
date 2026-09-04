"""Profile-local autonomy paths. Always HERMES_HOME-anchored."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from hermes_constants import get_hermes_home

HomeLike = Optional[Union[str, Path]]


def resolve_home(hermes_home: HomeLike = None) -> Path:
    if hermes_home is None:
        return get_hermes_home().resolve()
    return Path(hermes_home).expanduser().resolve()


def autonomy_dir(hermes_home: HomeLike = None) -> Path:
    path = resolve_home(hermes_home) / "autonomy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_db_path(hermes_home: HomeLike = None) -> Path:
    return autonomy_dir(hermes_home) / "work.db"


def mission_path(hermes_home: HomeLike = None) -> Path:
    return autonomy_dir(hermes_home) / "mission.md"


def state_path(hermes_home: HomeLike = None) -> Path:
    return autonomy_dir(hermes_home) / "state.json"


def scripts_dir(hermes_home: HomeLike = None) -> Path:
    path = resolve_home(hermes_home) / "scripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def soul_path(hermes_home: HomeLike = None) -> Path:
    return resolve_home(hermes_home) / "SOUL.md"


def profile_slug(hermes_home: HomeLike = None) -> str:
    home = resolve_home(hermes_home)
    if home.name == "profiles":
        return "default"
    if home.parent.name == "profiles":
        return home.name
    return "default"


def sibling_profile_home(slug: str, hermes_home: HomeLike = None) -> Path:
    """Resolve another profile's HERMES_HOME from this one (multiplex-safe)."""
    home = resolve_home(hermes_home)
    name = (slug or "").strip().lower()
    if home.parent.name == "profiles":
        return home.parent / name
    return Path.home() / ".hermes" / "profiles" / name
