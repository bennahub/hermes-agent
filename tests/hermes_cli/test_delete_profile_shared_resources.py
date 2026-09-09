"""What ``delete_profile`` must reach that ``rmtree`` cannot.

Everything an agent owns lives under ``profiles/<name>`` except two things, both rooted at
the OWNER's home: the hosted-room peer reservations that admit new A2A work at it, and its
exclusive logical Computer. Left behind, a deleted agent stays routable and keeps a machine
in the owner's computer list. Left over-eager, a deletion would take the shared room log or
another agent's machine with it. These tests pin both edges.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway import hosted_rooms as rooms
from gateway.agent_computer.errors import ForbiddenError
from gateway.agent_computer.models import OWNER_PRINCIPAL, ControlAuthority
from gateway.agent_computer.adapter import InMemoryRuntime
from gateway.agent_computer.service import AgentComputerService
from gateway.agent_computer.store import AgentComputerStore
from hermes_cli.profiles import create_profile, delete_profile, profile_exists
from hermes_constants import named_profile_is_deleted

USER = {"kind": "user", "id": "owner", "display_name": "Owner"}


def _clock():
    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _svc(tmp_path: Path) -> AgentComputerService:
    return AgentComputerService(
        AgentComputerStore(tmp_path / "state.db"), InMemoryRuntime(),
        data_root=tmp_path, clock=_clock, takeover_ttl_s=60)


def _claims(target: str, room: str = "room-1", member: str = "member-1") -> dict:
    return {"room_id": room, "member_id": member, "target_profile": target,
            "authority_gateway_id": "gateway-a", "authority_epoch": 1}


# ── the A2A fence ────────────────────────────────────────────────────────────────────

def test_revoking_a_targets_reservations_stops_new_peer_work_reaching_it(tmp_path):
    db = tmp_path / "state.db"
    rooms.reserve_peer_room(db, claims=_claims("faisal"), expires_at=900, now=100)
    rooms.reserve_peer_room(
        db, claims=_claims("faisal", room="room-2", member="member-2"), expires_at=900, now=100)

    revoked = rooms.revoke_peer_reservations_for_profile(db, target_profile="faisal", now=200)

    assert revoked == 2
    assert not rooms.peer_room_is_reserved(db, room_id="room-1", target_profile="faisal", now=300)
    assert not rooms.peer_room_grant_is_current(db, claims=_claims("faisal"), now=300)


def test_revoking_one_target_leaves_every_other_agents_routing_intact(tmp_path):
    db = tmp_path / "state.db"
    rooms.reserve_peer_room(db, claims=_claims("faisal"), expires_at=900, now=100)
    rooms.reserve_peer_room(
        db, claims=_claims("majed", member="member-2"), expires_at=900, now=100)

    rooms.revoke_peer_reservations_for_profile(db, target_profile="faisal", now=200)

    assert rooms.peer_room_is_reserved(db, room_id="room-1", target_profile="majed", now=300)


def test_revoking_a_target_preserves_the_rooms_membership_and_event_log(tmp_path):
    db = tmp_path / "state.db"
    rooms.create_room(
        db, room_id="room-1", name="Build",
        members=[{"kind": "agent", "id": "faisal"}, {"kind": "agent", "id": "majed"}],
        authority_gateway_id="gateway-a", now=100)
    rooms.append_event(
        db, room_id="room-1", event_id="e1", kind="message.user", actor=USER,
        payload={"text": "shipped"}, authority_gateway_id="gateway-a", authority_epoch=1, now=110)
    rooms.reserve_peer_room(db, claims=_claims("faisal"), expires_at=900, now=100)

    rooms.revoke_peer_reservations_for_profile(db, target_profile="faisal", now=200)

    state = rooms.room_state(db, room_id="room-1")
    assert [m["id"] for m in state["members"]] == ["faisal", "majed"]
    assert [e["event_id"] for e in rooms.read_events(db, room_id="room-1")["events"]] == ["e1"]


def test_revoking_a_target_with_nothing_reserved_is_a_no_op(tmp_path):
    db = tmp_path / "state.db"
    rooms.create_room(
        db, room_id="room-1", name="Build", members=[], authority_gateway_id="gateway-a", now=100)

    assert rooms.revoke_peer_reservations_for_profile(db, target_profile="faisal", now=200) == 0


# ── the logical Computer ─────────────────────────────────────────────────────────────

def test_retiring_a_profile_drops_its_computer_but_keeps_the_audit_trail(tmp_path):
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    svc.wake(computer.id, OWNER_PRINCIPAL)

    result = svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert result == {"retired": True, "profile_id": "faisal", "computer_id": computer.id}
    assert svc.store.get_computer_by_profile("faisal") is None
    assert [c.agent_profile_id for c in svc.list_computers()] == []
    assert svc.store.active_lease_for_computer(computer.id) is None
    events = [e.event_type for e in svc.list_audit(computer.id)]
    assert "computer_provision" in events and "computer_retired" in events


def test_retiring_one_agents_computer_leaves_the_others_running(tmp_path):
    svc = _svc(tmp_path)
    svc.ensure_computer("faisal")
    keeper = svc.ensure_computer("majed")
    svc.wake(keeper.id, OWNER_PRINCIPAL)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert [c.agent_profile_id for c in svc.list_computers()] == ["majed"]
    assert svc.store.active_lease_for_computer(keeper.id) is not None


def test_retiring_a_profile_that_never_had_a_computer_is_idempotent(tmp_path):
    svc = _svc(tmp_path)

    assert svc.retire_profile("faisal", OWNER_PRINCIPAL) == {
        "retired": False, "profile_id": "faisal"}
    assert svc.retire_profile("faisal", OWNER_PRINCIPAL)["retired"] is False


def test_retiring_a_profile_outranks_a_live_owner_takeover(tmp_path):
    """Nobody can hand the machine back to an agent that no longer exists."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    svc.wake(computer.id, OWNER_PRINCIPAL)
    held = svc.store.get_computer(computer.id)
    held.control_authority = ControlAuthority.OWNER_CONTROLLED
    svc.store.upsert_computer(held)

    assert svc.retire_profile("faisal", OWNER_PRINCIPAL)["retired"] is True
    assert svc.store.get_computer_by_profile("faisal") is None


def test_retiring_a_profile_releases_its_browser_identity_for_reuse(tmp_path):
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal", "majed"], metadata={"label": "work"})
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    released = svc.get_identity(identity.id)
    assert released.lock_computer_id is None
    assert released.revoked is False  # a shared identity is not the deleted agent's to destroy
    # ...and the deleted agent stops owning it, or a recreated slug re-inherits the profile.
    assert released.ownership == ["majed"]
    other = svc.ensure_computer("majed")
    svc.attach_identity(other.id, identity.id, OWNER_PRINCIPAL)


# ── the canonical lifecycle, end to end ──────────────────────────────────────────────

@pytest.fixture
def owner_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_AGENT_COMPUTER_ROOT", str(tmp_path / "agent-computers"))
    from gateway import agent_computer

    agent_computer.reset_service_for_tests()
    yield hermes_home
    agent_computer.reset_service_for_tests()


def test_deleting_an_agent_closes_its_a2a_fence_and_retires_its_computer(owner_home):
    from gateway import agent_computer

    db = owner_home / "state.db"
    rooms.reserve_peer_room(db, claims=_claims("faisal"), expires_at=1e12, now=100)
    computer = agent_computer.get_service().ensure_computer("faisal")
    create_profile("faisal", no_alias=True, no_skills=True)

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    assert not rooms.peer_room_grant_is_current(db, claims=_claims("faisal"))
    assert agent_computer.get_service().store.get_computer_by_profile("faisal") is None
    assert "computer_retired" in [
        e.event_type for e in agent_computer.get_service().list_audit(computer.id)]


# ── what the deleted agent must stop owning (P2-2) ───────────────────────────────────

def test_retiring_a_profile_strikes_it_from_every_identitys_ownership(tmp_path):
    """``allows()`` is a plain membership test and a profile name is a slug the Owner can mint
    again. Leaving the name behind is the whole leak."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal", "majed"], metadata={"label": "work"})
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    released = svc.get_identity(identity.id)
    assert released.ownership == ["majed"]
    assert released.allows("faisal") is False
    assert released.revoked is False  # still the co-owner's to use


def test_a_recreated_slug_does_not_inherit_the_deleted_agents_browser_profile(tmp_path):
    """The live case: delete 'faisal', create 'faisal' again, and it must NOT mount the
    previous agent's Chromium profile — its cookies and logged-in sessions."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal", "majed"])
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)
    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    reborn = svc.ensure_computer("faisal")  # same slug, new agent
    with pytest.raises(ForbiddenError):
        svc.attach_identity(reborn.id, identity.id, OWNER_PRINCIPAL)
    assert svc.store.get_computer(reborn.id).active_browser_identity_id is None


def test_retiring_the_last_owner_of_an_identity_revokes_it_rather_than_orphaning_it(tmp_path):
    """A sole-owned identity has nobody left to attach it. Revoked is the honest terminal
    state; listed-as-usable while refusing every attach is not."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal"])
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    dead = svc.get_identity(identity.id)
    assert dead.ownership == []
    assert dead.revoked is True
    assert dead.lock_computer_id is None
    assert [i.id for i in svc.list_identities()] == [identity.id]  # enumerable, not orphaned


def test_retiring_a_profile_with_no_computer_still_strikes_its_identity_ownership(tmp_path):
    """Ownership is granted by slug, not by attachment: an agent that never provisioned a
    computer can still be named in one."""
    svc = _svc(tmp_path)
    identity = svc.create_identity(ownership=["faisal", "majed"])

    assert svc.retire_profile("faisal", OWNER_PRINCIPAL) == {
        "retired": False, "profile_id": "faisal"}
    assert svc.get_identity(identity.id).ownership == ["majed"]


def test_retiring_one_agent_leaves_another_agents_identity_ownership_alone(tmp_path):
    svc = _svc(tmp_path)
    svc.ensure_computer("faisal")
    keeper = svc.create_identity(ownership=["majed"])

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert svc.get_identity(keeper.id).ownership == ["majed"]
    assert svc.get_identity(keeper.id).revoked is False


# ── the workspace the record used to name (P2-3) ─────────────────────────────────────

def test_retiring_a_profile_removes_its_computers_workspace(tmp_path):
    """``delete_computer`` drops the only row naming this tree. Keeping it orphans browser
    state and downloads behind a dialog that says the deletion cannot be undone."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    workspace = Path(computer.persistence_ref)
    (workspace / "Cookies").write_text("session=secret", encoding="utf-8")

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert not workspace.exists()
    assert not (tmp_path / "computers" / "faisal").exists()  # empty per-profile parent goes too
    assert (tmp_path / "computers").is_dir()  # the root itself never does


def test_retiring_one_agents_computer_leaves_the_others_workspace_on_disk(tmp_path):
    svc = _svc(tmp_path)
    svc.ensure_computer("faisal")
    keeper = Path(svc.ensure_computer("majed").persistence_ref)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert keeper.is_dir()


def test_retirement_never_removes_a_workspace_outside_the_agent_computers_root(tmp_path):
    """Containment comes from the shared path primitive, not from trusting a stored string: a
    row carrying a path from another data root is reported and left alone."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    outsider = tmp_path / "not-agent-computers"
    outsider.mkdir()
    (outsider / "keep.txt").write_text("mine", encoding="utf-8")
    computer.persistence_ref = str(outsider)
    svc.store.upsert_computer(computer)

    assert svc.retire_profile("faisal", OWNER_PRINCIPAL)["retired"] is True

    assert (outsider / "keep.txt").exists()
    detail = [e.detail for e in svc.list_audit(computer.id) if e.event_type == "computer_retired"][0]
    assert detail["workspace_removed"] is False
    assert detail["workspace_reason"] == "outside_root"


def test_retirement_records_what_it_took_in_the_audit_row(tmp_path):
    """Not silent: the surviving audit row names the workspace and the identities touched."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal", "majed"])
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    detail = [e.detail for e in svc.list_audit(computer.id) if e.event_type == "computer_retired"][0]
    assert detail["identities_pruned"] == [identity.id]
    assert detail["identities_revoked"] == []
    assert detail["workspace_removed"] is True


# ── stopping the work this process is doing for the profile (P2-4) ───────────────────

def test_deleting_a_profile_releases_the_state_db_handles_this_process_holds(owner_home):
    """The gateway servicing ``profiles.delete`` can be running the deleted profile's OWN turn
    (``compute_host`` binds its ``HERMES_HOME`` and acquires its ``state.db``). The PID sweeps
    skip this process on purpose, so nothing else can find that holder."""
    import hermes_state_registry as registry

    create_profile("faisal", no_alias=True, no_skills=True)
    profile_dir = owner_home / "profiles" / "faisal"
    held = registry.acquire(profile_dir / "state.db")
    assert held in registry.live_shared_session_dbs()

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    assert held not in registry.live_shared_session_dbs()
    assert not profile_dir.exists()


def test_deleting_a_profile_leaves_another_profiles_state_db_handle_open(owner_home):
    import hermes_state_registry as registry

    create_profile("faisal", no_alias=True, no_skills=True)
    create_profile("majed", no_alias=True, no_skills=True)
    keeper = registry.acquire(owner_home / "profiles" / "majed" / "state.db")

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    assert keeper in registry.live_shared_session_dbs()
    registry.release_or_close(keeper)


# ── the retirement step must not fail quietly (P3-1) ─────────────────────────────────

def test_a_locked_root_state_db_is_retried_before_the_fence_is_given_up_on(owner_home):
    import sqlite3

    from gateway import hosted_rooms
    from hermes_cli import profiles as profiles_mod

    attempts = []

    def _flaky(db_path, *, target_profile, now=None):
        attempts.append(target_profile)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return 2

    with patch.object(hosted_rooms, "revoke_peer_reservations_for_profile", _flaky):
        revoked = profiles_mod._revoke_peer_reservations_with_retry(
            owner_home / "state.db", "faisal")

    assert (revoked, len(attempts)) == (2, 3)


def test_a_failed_fence_revoke_is_reported_with_its_consequence(owner_home, capsys):
    """Success printed and failure printed nothing, so a fence left OPEN — the one thing the
    step exists to close — was indistinguishable from a clean delete."""
    import sqlite3

    from gateway import hosted_rooms
    from hermes_cli import profiles as profiles_mod

    def _locked(db_path, *, target_profile, now=None):
        raise sqlite3.OperationalError("database is locked")

    with patch.object(hosted_rooms, "revoke_peer_reservations_for_profile", _locked):
        profiles_mod._retire_shared_agent_resources("faisal", owner_home / "profiles" / "faisal")

    out = capsys.readouterr().out
    assert "Could not revoke peer-room reservations" in out
    assert "A2A/RoomLink target" in out


def test_a_checkout_without_the_gateway_stays_silent(owner_home, capsys):
    """Best-effort is not the same as noisy: an absent gateway package is the documented
    no-op and must not print a scary warning at every delete."""
    from hermes_cli import profiles as profiles_mod

    def _missing():
        raise ModuleNotFoundError("No module named 'gateway'")

    assert profiles_mod._best_effort_retirement_step("do the thing", "bad things", _missing) is None
    assert capsys.readouterr().out == ""


# ── the summary has to name what it takes (P3-2) ─────────────────────────────────────

def test_the_delete_summary_names_the_computer_and_the_peer_reservations(owner_home, capsys):
    from hermes_cli import profiles as profiles_mod

    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    capsys.readouterr()

    profiles_mod._print_delete_summary("faisal", profile_dir, False, None)

    out = capsys.readouterr().out
    assert "agent computer" in out
    assert "peer-room reservations" in out
    assert "KEPT" in out and "audit trail" in out


# ── delete racing create of the same name (P3-5) ─────────────────────────────────────

def test_a_create_racing_a_delete_of_the_same_name_is_not_silently_erased(owner_home):
    """A create landing between the tombstone and the ``rmtree`` had its tombstone cleared and
    its fresh files removed by the delete — and BOTH calls returned ok, so the Owner was told a
    profile exists that does not. The two halves now take the same per-name lock."""
    import threading

    from hermes_cli import profiles as profiles_mod

    create_profile("faisal", no_alias=True, no_skills=True)
    profile_dir = owner_home / "profiles" / "faisal"
    real_rmtree = profiles_mod._rmtree_with_retry
    created: list = []

    def _recreate():
        try:
            created.append(create_profile("faisal", no_alias=True, no_skills=True))
        except Exception as exc:  # recorded, so a refusal is visible rather than swallowed
            created.append(exc)

    racers: list = []

    def _rmtree_with_a_racing_create(target, onexc):
        # Started INSIDE the tombstone→rmtree window the reviewer named. The join happens
        # after delete_profile returns: joining here would hold the lock the racer waits for.
        racer = threading.Thread(target=_recreate)
        racers.append(racer)
        racer.start()
        # Long enough that, unserialised, the whole create completes inside this window —
        # which is exactly how the delete used to erase it.
        time.sleep(1.0)
        real_rmtree(target, onexc)

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ), patch.object(profiles_mod, "_rmtree_with_retry", _rmtree_with_a_racing_create):
        delete_profile("faisal", yes=True)
    for racer in racers:
        racer.join(30)

    assert created and not isinstance(created[0], Exception), created
    assert profile_dir.is_dir()
    assert (profile_dir / ".env").exists()  # a bootstrapped profile, not a half-erased shell
    assert (profile_dir / "memories").is_dir() and (profile_dir / "sessions").is_dir()
    assert not named_profile_is_deleted(profile_dir)
    assert profile_exists("faisal")


# ── the computerless retirement leaves evidence (P3-1) ───────────────────────────────

def test_striking_a_computerless_agents_grants_is_recorded_in_the_audit_trail(tmp_path):
    """There is no computer to hang ``computer_retired`` on, so the pruned grants used to be
    computed and thrown away: an identity silently lost an owner with nothing saying who or
    why. Audit rows are the only durable record a delete leaves behind."""
    svc = _svc(tmp_path)
    shared = svc.create_identity(ownership=["faisal", "majed"])
    sole = svc.create_identity(ownership=["faisal"])

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    released = [e for e in svc.list_audit() if e.event_type == "identity_ownership_released"]
    assert len(released) == 1
    assert released[0].computer_id is None  # nullable column; unfiltered list_audit finds it
    assert released[0].actor == OWNER_PRINCIPAL
    assert released[0].detail["profile_id"] == "faisal"
    assert sorted(released[0].detail["identities_pruned"]) == sorted([shared.id, sole.id])
    assert released[0].detail["identities_revoked"] == [sole.id]


def test_a_computerless_agent_that_owned_nothing_writes_no_audit_noise(tmp_path):
    """Only a real strike is evidence; every other delete must not grow the trail."""
    svc = _svc(tmp_path)
    svc.create_identity(ownership=["majed"])

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert [e for e in svc.list_audit() if e.event_type == "identity_ownership_released"] == []


# ── a lock timeout must not leave a half-torn-down profile (P3-2) ────────────────────

def test_a_name_lock_timeout_aborts_before_anything_is_torn_down(owner_home):
    """The lock used to be taken at the tombstone — after the service was disabled, the s6
    slot dropped, the gateway stopped and the backends swept. A 30 s timeout there left the
    profile alive with its service disabled and its gateway down, reported as a bare
    ``TimeoutError``. Taken first, the timeout costs nothing."""
    import contextlib

    from hermes_cli import profiles as profiles_mod

    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)

    @contextlib.contextmanager
    def _already_held(canon, **_kw):
        raise TimeoutError(
            f"another create or delete of profile '{canon}' is already running")
        yield  # pragma: no cover - unreachable, keeps this a generator

    with patch.object(profiles_mod, "_profile_name_lock", _already_held), patch.object(
        profiles_mod, "_retire_shared_agent_resources"
    ) as retire, patch.object(
        profiles_mod, "_cleanup_gateway_service"
    ) as cleanup, patch.object(
        profiles_mod, "_maybe_unregister_gateway_service"
    ) as unregister, patch.object(
        profiles_mod, "_stop_profile_backends"
    ) as stop_backends, pytest.raises(TimeoutError) as excinfo:
        delete_profile("faisal", yes=True)

    for step in (retire, cleanup, unregister, stop_backends):
        assert step.call_count == 0, f"{step} ran before the lock was taken"
    assert profile_dir.is_dir() and profile_exists("faisal")
    assert not named_profile_is_deleted(profile_dir)
    # ...and the message names the state and the remedy, like the rmtree failure path does.
    message = str(excinfo.value)
    assert "already running" in message
    assert "Nothing about 'faisal' was changed" in message
    assert "run the delete again" in message


# ── the summary must not overstate what a delete removes (P3-3) ──────────────────────

def test_the_summary_does_not_claim_the_browser_profile_is_deleted(owner_home, capsys):
    """A sole-owned identity is revoked, but its Chromium profile at
    ``<agent-computers>/identities/<id>/`` — cookies included — is deliberately kept. The
    summary said the delete took "the browser profile" with it."""
    from hermes_cli import profiles as profiles_mod

    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    capsys.readouterr()

    profiles_mod._print_delete_summary("faisal", profile_dir, False, None)

    deleted, _, kept = capsys.readouterr().out.partition("This will be KEPT:")
    assert "browser profile" not in deleted
    assert "private workspace and downloads" in deleted
    assert "stays on disk" in kept and "revoked" in kept


def test_a_sole_owned_identitys_browser_profile_survives_the_delete(tmp_path):
    """The retention the copy now describes, proved rather than asserted: identity profiles
    live at ``<data_root>/identities/<id>``, outside the ``computers/`` root
    ``_remove_persistence_dir`` is scoped to, so the revoke takes nothing off disk."""
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("faisal")
    identity = svc.create_identity(ownership=["faisal"])
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)
    profile_ref = Path(svc.get_identity(identity.id).profile_ref)
    (profile_ref / "Cookies").write_text("session=kept", encoding="utf-8")

    svc.retire_profile("faisal", OWNER_PRINCIPAL)

    assert svc.get_identity(identity.id).revoked is True
    assert (profile_ref / "Cookies").read_text(encoding="utf-8") == "session=kept"


# ── the two owner-home stores the delete used to walk past ───────────────────────────
#
# Composition defects, both: each lane was right alone and wrong once the others shipped. The A2A
# ledger and the connection-health store live at the OWNER's root, so ``rmtree`` never reaches them
# — the same reason the peer-room fence and the logical Computer are handled above, and the same
# reason ``_release_identity_ownership`` strikes a deleted name from every BrowserIdentity.


def test_deleting_an_agent_retires_its_a2a_history_so_a_reused_slug_starts_empty(owner_home):
    """The delete confirmation says "Their conversations … go with them." Before this, a slug the
    Owner minted again resolved the deleted agent's thread and read it."""
    from gateway import a2a_threads

    a2a_threads.record_send(owner_home, sender="faisal", recipients=["joud"],
                            body="here is the client list", send_id="s-1", sent_at=100.0)
    create_profile("faisal", no_alias=True, no_skills=True)
    assert a2a_threads.resolve_thread(owner_home, profile="faisal", counterpart="joud", run="")

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    create_profile("faisal", no_alias=True, no_skills=True)  # the Owner reuses the name
    assert a2a_threads.resolve_thread(owner_home, profile="faisal", counterpart="joud", run="") == ""
    assert a2a_threads.runs_for(owner_home, profile="faisal")["runs"] == []
    # Kept, not destroyed: the counterpart's own history, and the row itself.
    assert [e.body for e in a2a_threads.read_thread(
        owner_home, thread=a2a_threads.thread_id("faisal", "joud"))] == ["here is the client list"]


def test_deleting_an_agent_clears_the_connection_faults_recorded_against_it(owner_home):
    """An MCP fault is keyed to the serving profile and only that profile's own successful call
    clears it, so one left behind is a fault nobody can ever act on — and the respawn guard holds
    autonomous work on it."""
    import json

    from agent import connection_health as ch

    create_profile("faisal", no_alias=True, no_skills=True)
    block = ch.build_connection_block(provider="acme-mcp",
                                      reason_code=ch.REASON_REVOKED).to_dict()
    block["connection_id"] = "acme-mcp"
    ch.record_fault(block, scope="faisal")
    store = owner_home / "state" / "connection_health.json"
    assert list(json.loads(store.read_text(encoding="utf-8"))["faults"]) == ["faisal/acme-mcp"]

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    assert json.loads(store.read_text(encoding="utf-8"))["faults"] == {}


def test_deleting_one_agent_keeps_anothers_history_and_faults(owner_home):
    """Narrow on both stores: one agent's retirement is not the roster's."""
    import json

    from agent import connection_health as ch
    from gateway import a2a_threads

    a2a_threads.record_send(owner_home, sender="majed", recipients=["joud"], body="mine",
                            send_id="s-9", sent_at=100.0)
    create_profile("faisal", no_alias=True, no_skills=True)
    create_profile("majed", no_alias=True, no_skills=True)
    for scope in ("faisal", "majed"):
        block = ch.build_connection_block(provider="acme-mcp",
                                          reason_code=ch.REASON_REVOKED).to_dict()
        block["connection_id"] = "acme-mcp"
        ch.record_fault(block, scope=scope)

    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile("faisal", yes=True)

    assert a2a_threads.resolve_thread(owner_home, profile="majed", counterpart="joud", run="")
    faults = json.loads((owner_home / "state" / "connection_health.json").read_text(
        encoding="utf-8"))["faults"]
    assert list(faults) == ["majed/acme-mcp"]


def test_the_summary_says_the_messages_are_kept_but_not_inheritable(owner_home, capsys):
    """Kept AND unreachable is a third state. "Deleted" would be false about an append-only
    ledger; a bare "kept" reads as "a new agent with this name picks them back up", which is the
    defect the retirement closed."""
    from hermes_cli import profiles as profiles_mod

    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    capsys.readouterr()

    profiles_mod._print_delete_summary("faisal", profile_dir, False, None)

    _, _, kept = capsys.readouterr().out.partition("This will be KEPT:")
    assert "messages to and from other agents" in kept
    assert "cannot open or continue them" in kept
