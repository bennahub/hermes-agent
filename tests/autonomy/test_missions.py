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


def test_roster_catalog_covers_current_known_agents():
    slugs = set(all_owner_facing_slugs())
    for name in PILOT_PROFILES:
        assert name in slugs
    for name in ("fahad", "faisal", "fares", "hamad", "joud", "majed", "mishari", "nawaf", "rashid", "turki"):
        assert name in slugs
