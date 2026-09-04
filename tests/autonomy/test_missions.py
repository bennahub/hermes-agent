from agent.autonomy.missions import (
    PILOT_PROFILES,
    all_owner_facing_slugs,
    display_name,
    mission_for,
)


def test_pilot_missions_are_role_specific():
    abu = mission_for("abu-saud")
    badr = mission_for("badr")
    sami = mission_for("sami")
    nasser = mission_for("nasser")
    assert "coordination" in abu["domains"]
    assert "engineering" in badr["domains"]
    assert "operations" in sami["domains"]
    assert "erp" in nasser["domains"]
    assert "marketing" not in str(sami["body"]).lower()
    joud = mission_for("joud")
    assert "growth" in joud["domains"]
    assert "operations" not in joud["domains"]


def test_unknown_profile_gets_soul_derived_fallback():
    spec = mission_for("new-specialist")
    assert "SOUL" in spec["body"]
    assert display_name("badr") == "Badr"
    assert display_name("new-specialist") == "new-specialist"


def test_self_start_does_not_wait_for_abda():
    nasser = mission_for("nasser")
    body = str(nasser["body"])
    assert "create or bind" in body.lower()
    assert "Do not start a Jira key without" not in body


def test_soul_block_authorizes_self_started_tracking(tmp_path, monkeypatch):
    from agent.autonomy.missions import install_mission

    home = tmp_path / "profiles" / "badr"
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text(
        "Execute a Jira key only after the owner says ابدأ on that key.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    result = install_mission(home)
    soul = (home / "SOUL.md").read_text(encoding="utf-8")
    assert result["soul_changed"] is True
    assert "does NOT wait for Owner" in soul
    assert "create or bind" in soul


def test_roster_catalog_covers_current_known_agents():
    slugs = set(all_owner_facing_slugs())
    for name in PILOT_PROFILES:
        assert name in slugs
    for name in ("fahad", "faisal", "fares", "hamad", "joud", "majed", "mishari", "nawaf", "rashid", "turki"):
        assert name in slugs
