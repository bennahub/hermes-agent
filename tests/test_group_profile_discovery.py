"""Native room discovery must not admit internal directories or deleted profiles."""
from types import SimpleNamespace
import pytest
import hermes_constants
from gateway import hosted_rooms, hosted_room_discussion
from tui_gateway.hosted_room_service import HostedRoomService

@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = hermes_constants.set_hermes_home_override(tmp_path)
    for name in ("n0-closure-qa", "fares", ".deleted", "bad name", "retired"):
        (tmp_path / "profiles" / name).mkdir(parents=True)
    hermes_constants.mark_named_profile_deleted(tmp_path / "profiles" / "retired")
    svc = HostedRoomService(SimpleNamespace(), db_path=tmp_path / "state.db")
    yield svc
    hermes_constants.reset_hermes_home_override(token)

def members(*profiles):
    return [{"member_id": p, "profile": p, "handle": p} for p in profiles]

def test_native_create_replay_ignores_internal_directories(service):
    roster = members("n0-closure-qa", "fares")
    first = service.create_room(room_id="qa", name="QA", members=roster)
    again = service.create_room(room_id="qa", name="QA", members=roster)
    assert first["room_id"] == again["room_id"] == "qa"
    stored = hosted_rooms.room_state(service.db_path, room_id="qa")
    assert {m["profile"] for m in stored["members"]} == {"n0-closure-qa", "fares"}
    assert len(hosted_rooms.list_rooms(service.db_path)) == 1
    assert len(hosted_room_discussion.validate_room(stored, local_profiles=service.local_profiles()).active_members) == 2

@pytest.mark.parametrize("target", ["retired", "missing", ".deleted", "bad name"])
def test_unavailable_or_invalid_members_cannot_create_room(service, target):
    with pytest.raises(hosted_room_discussion.DiscussionValidationError):
        service.create_room(room_id="bad", name="Bad", members=members("fares", target))
    assert hosted_rooms.list_rooms(service.db_path) == []

def test_default_and_minimum_members_contract(service):
    with pytest.raises(hosted_room_discussion.DiscussionValidationError):
        service.create_room(room_id="short", name="Short", members=members("fares"))
    room = service.create_room(room_id="default-room", name="Default", members=members("default", "fares"))
    assert {m["profile"] for m in room["members"]} == {"default", "fares"}
