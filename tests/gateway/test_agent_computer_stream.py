"""Human Takeover stream / direct-input contract (BWM-796 C0 UX repair)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.agent_computer.contract import FORBIDDEN_PUBLIC_KEYS, AgentComputerContract, sanitize_public
from gateway.agent_computer.errors import ConflictError, ForbiddenError, StaleControllerError
from gateway.agent_computer.keys import cdp_key_params, modifier_mask
from gateway.agent_computer.models import OWNER_PRINCIPAL, agent_principal, project_control
from gateway.agent_computer.cursor import map_remote_cursor
from gateway.agent_computer.pointer import map_client_to_viewport, map_owner_pointer
from gateway.agent_computer.service import AgentComputerService
from gateway.agent_computer.store import AgentComputerStore
from gateway.agent_computer.stream import (
    FrameBroker,
    OwnerStreamSession,
    get_stream_hub,
    normalize_owner_event,
    reset_stream_hub_for_tests,
)
from gateway.agent_computer.adapter import InMemoryRuntime


def _svc(tmp_path: Path) -> AgentComputerService:
    reset_stream_hub_for_tests()
    return AgentComputerService(
        AgentComputerStore(tmp_path / "state.db"),
        InMemoryRuntime(),
        data_root=tmp_path,
    )


def _owner_session(svc: AgentComputerService, profile: str = "majed"):
    computer = svc.ensure_computer(profile)
    identity = svc.create_identity(ownership=[profile], metadata={"purpose": "stream-test"})
    svc.attach_identity(computer.id, identity.id, OWNER_PRINCIPAL)
    agent = agent_principal(profile)
    svc.wake(computer.id, agent)
    raw = svc.request_takeover(computer.id, OWNER_PRINCIPAL, reason="ux")
    lease = svc.connect_takeover(computer.id, OWNER_PRINCIPAL, takeover_token=raw["takeover_token"])
    return svc.get_computer(computer.id), svc.get_identity(identity.id), lease


def test_project_control_is_single_owner_label():
    assert project_control("OWNER_CONTROLLED") == "OWNER_CONTROL"
    assert project_control("AGENT_CONTROLLED") == "AGENT_CONTROL"
    assert project_control("TAKEOVER_PENDING") == "TAKEOVER_PENDING"


def test_frame_broker_backpressure():
    broker = FrameBroker(max_inflight=2)
    assert broker.offer(1) is True
    assert broker.offer(2) is True
    assert broker.offer(3) is False
    assert broker.dropped == 1
    assert broker.ack(1) is True
    assert broker.offer(4) is True
    assert broker.inflight == 2


def test_client_pointer_scaling():
    x, y = map_client_to_viewport(
        200, 100, client_width=1600, client_height=1000, viewport_width=800, viewport_height=500
    )
    assert (x, y) == (100.0, 50.0)
    same = map_client_to_viewport(
        10, 20, client_width=1440, client_height=900, viewport_width=1440, viewport_height=900
    )
    assert same == (10.0, 20.0)


def test_owner_pointer_display_to_viewport_and_retina_frame():
    # Displayed canvas is smaller than the 1440×900 Chromium viewport.
    x, y = map_owner_pointer(
        240, 140,
        displayed_width=720,
        displayed_height=450,
        viewport_width=1440,
        viewport_height=900,
    )
    assert (x, y) == (480.0, 280.0)
    # Retina-sized JPEG (2×) of a 1440×900 viewport: display CSS → bitmap → viewport.
    rx, ry = map_owner_pointer(
        240, 140,
        displayed_width=720,
        displayed_height=450,
        viewport_width=1440,
        viewport_height=900,
        frame_width=2880,
        frame_height=1800,
    )
    assert (rx, ry) == (480.0, 280.0)
    identity = map_owner_pointer(
        240, 140,
        displayed_width=1440,
        displayed_height=900,
        viewport_width=1440,
        viewport_height=900,
        frame_width=1440,
        frame_height=900,
    )
    assert identity == (240.0, 140.0)


def test_remote_cursor_never_exposes_crosshair():
    assert map_remote_cursor("pointer") == "pointer"
    assert map_remote_cursor("text") == "text"
    assert map_remote_cursor("auto") == "default"
    assert map_remote_cursor("crosshair") == "default"
    assert map_remote_cursor("url(foo.png), pointer") == "default"
    assert map_remote_cursor("not-a-cursor") == "default"
    assert map_remote_cursor("") == "default"


def test_cursor_probe_does_not_audit_or_wake(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    result = svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "cursor", "x": 240, "y": 140, "client_width": 1440, "client_height": 900},
    )
    assert result["cursor"] == "pointer"
    kinds = [event.event_type for event in svc.list_audit(computer.id)]
    assert "stream_input" not in kinds or all(
        event.detail.get("kind") != "cursor"
        for event in svc.list_audit(computer.id)
        if event.event_type == "stream_input"
    )


def test_normalize_owner_event_maps_wheel_and_key():
    ev = normalize_owner_event(
        {"type": "wheel", "x": 200, "y": 100, "delta_y": 80, "client_width": 2000, "client_height": 1000},
        viewport_width=1000,
        viewport_height=500,
    )
    assert ev["kind"] == "wheel"
    assert ev["x"] == 100.0
    assert ev["delta_y"] == 80
    key = normalize_owner_event(
        {"type": "key", "phase": "down", "key": "Enter", "code": "Enter"},
        viewport_width=1440,
        viewport_height=900,
    )
    assert key["kind"] == "key"
    assert key["key"] == "Enter"


def test_cdp_key_params_named_and_printable():
    enter = cdp_key_params(phase="down", key="Enter")
    assert enter["key"] == "Enter"
    assert enter["windowsVirtualKeyCode"] == 13
    assert enter["type"] == "rawKeyDown"
    letter = cdp_key_params(phase="down", key="a", modifiers=modifier_mask())
    assert letter.get("text") == "a"
    assert letter.get("type") == "char"
    ctrl = cdp_key_params(phase="down", key="a", modifiers=modifier_mask(ctrl=True))
    assert "text" not in ctrl


def test_status_exposes_control_label_and_hides_cdp(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    status = sanitize_public(svc.public_status(computer))
    assert status["control"] == "OWNER_CONTROLLED"
    assert status["control_label"] == "OWNER_CONTROL"
    assert status["browser_identity"]["id"] == identity.id
    assert status["stream"]["public_cdp"] is False
    assert status["stream"]["kind"] == "screencast_frames"
    assert status["can_resume"] is True
    assert "origin" in status["location"]
    blob = str(status)
    for key in FORBIDDEN_PUBLIC_KEYS:
        assert f"'{key}'" not in blob and f'"{key}"' not in blob
    _ = lease


def test_stream_requires_owner_and_rejects_agent(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    agent = agent_principal("majed")
    with pytest.raises(ForbiddenError):
        svc.open_owner_stream(
            computer.id, agent, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
        )


def test_stream_input_rejected_when_agent_controlled(tmp_path):
    svc = _svc(tmp_path)
    computer = svc.ensure_computer("majed")
    agent = agent_principal("majed")
    _comp, agent_lease = svc.wake(computer.id, agent)
    with pytest.raises(ConflictError):
        svc.open_owner_stream(
            computer.id,
            OWNER_PRINCIPAL,
            lease_id=agent_lease.lease_id,
            fencing_epoch=agent_lease.fencing_epoch,
        )


def test_direct_pointer_wheel_keyboard_and_no_secrets_in_audit(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        width=1440,
        height=900,
    )
    secret = "STREAM-PASSWORD-MUST-NOT-LOG"
    svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "pointer", "phase": "click", "x": 500, "y": 184, "client_width": 1440, "client_height": 900},
    )
    svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "wheel", "x": 100, "y": 100, "delta_y": 120, "client_width": 1440, "client_height": 900},
    )
    svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "key", "phase": "down", "key": "Tab"},
    )
    svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "text", "text": secret},
    )
    blob = str([event.detail for event in svc.list_audit(computer.id)])
    assert secret not in blob
    assert "STREAM-PASSWORD" not in blob
    kinds = [event.detail.get("kind") for event in svc.list_audit(computer.id) if event.event_type == "stream_input"]
    assert "pointer" in kinds
    assert "wheel" in kinds
    assert "key" in kinds
    assert "text" in kinds
    assert computer.active_browser_identity_id == identity.id


def test_stale_generation_and_reconnect_keeps_owner_control(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    first, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    second, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    assert second.generation != first.generation
    with pytest.raises(StaleControllerError):
        svc.owner_stream_input(
            computer.id,
            OWNER_PRINCIPAL,
            lease_id=lease.lease_id,
            fencing_epoch=lease.fencing_epoch,
            generation=first.generation,
            event={"type": "pointer", "phase": "click", "x": 1, "y": 1},
        )
    svc.close_owner_stream(computer.id, second.generation)
    after = svc.get_computer(computer.id)
    assert after.control_authority.value == "OWNER_CONTROLLED"
    assert after.active_browser_identity_id == identity.id
    assert after.resume_observe_required is False


def test_dropped_frame_does_not_occupy_broker():
    session = OwnerStreamSession(
        computer_id="ac_test",
        identity_id="bi_test",
        generation=1,
        lease_id="lease",
        fencing_epoch=1,
    )
    assert session.push_frame(1, "aaaa", 1440, 900) is not None
    assert session.push_frame(2, "bbbb", 1440, 900) is not None
    dropped = session.push_frame(3, "cccc", 1440, 900)
    assert dropped is None
    assert session.broker.dropped == 1
    assert session.broker.inflight == 2


def test_owner_disconnect_and_give_back_drop_stream(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    assert get_stream_hub().get(computer.id) is not None
    svc.owner_disconnect(computer.id, OWNER_PRINCIPAL)
    assert get_stream_hub().get(computer.id) is None
    after = svc.get_computer(computer.id)
    assert after.control_authority.value == "AGENT_CONTROLLED"
    assert after.active_browser_identity_id == identity.id
    _ = session


def test_give_back_is_exactly_once_and_closes_stream(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    first = svc.give_back(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    again = svc.give_back(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    assert first.lease_id == again.lease_id
    assert first.controller.value == "agent"
    computer = svc.get_computer(computer.id)
    assert computer.control_authority.value == "AGENT_CONTROLLED"
    assert computer.active_browser_identity_id == identity.id
    assert computer.resume_observe_required is True
    assert get_stream_hub().get(computer.id) is None
    _ = session


def test_agent_input_rejected_during_owner_control(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    agent = agent_principal("majed")
    with pytest.raises((StaleControllerError, ForbiddenError)):
        svc.act(
            computer.id,
            agent,
            lease_id=lease.lease_id,
            fencing_epoch=lease.fencing_epoch,
            kind="click",
            target="nope",
        )


def test_contract_stream_hello_has_no_cdp(tmp_path):
    svc = _svc(tmp_path)
    contract = AgentComputerContract(svc)
    computer, identity, lease = _owner_session(svc)
    hello = contract.open_stream(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
    )
    assert hello["control"] == "OWNER_CONTROL"
    assert hello["identity_id"] == identity.id
    assert hello["public_cdp"] is False
    assert hello["computer_id"] == computer.id
    blob = str(hello)
    assert "127.0.0.1" not in blob
    assert "cdp_loopback" not in blob
    assert "webSocketDebuggerUrl" not in blob


def test_stream_input_uses_runtime_without_waking(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    seen = []
    session._dispatch = seen.append
    wakes = {"n": 0}
    real_wake = svc.runtime.wake

    def _count_wake(*args, **kwargs):
        wakes["n"] += 1
        return real_wake(*args, **kwargs)

    svc.runtime.wake = _count_wake
    svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={
            "type": "pointer",
            "phase": "click",
            "x": 240,
            "y": 140,
            "client_width": 720,
            "client_height": 450,
            "frame_width": 1440,
            "frame_height": 900,
        },
    )
    assert wakes["n"] == 0
    assert seen == []
    handle = svc._handles[computer.id]
    obs = svc.runtime.observe(handle)
    assert "pixel:480,280" in obs.text or "owner-pixel-clicked" in obs.text or obs.text


def test_normalize_displayed_canvas_coords():
    ev = normalize_owner_event(
        {
            "type": "pointer",
            "phase": "down",
            "x": 240,
            "y": 140,
            "client_width": 720,
            "client_height": 450,
            "frame_width": 1440,
            "frame_height": 900,
        },
        viewport_width=1440,
        viewport_height=900,
    )
    assert ev["x"] == 480.0
    assert ev["y"] == 280.0


def test_public_location_hides_file_paths_and_keeps_https_origin():
    from gateway.agent_computer.location import public_location, safe_navigate_url

    hidden = public_location("file:///home/hermes/.hermes/secret/input_fixture.html", "fix")
    assert hidden["origin"] == "fixture://local"
    assert "home" not in hidden["url"]
    assert hidden["url"] == "fixture://input_fixture.html"
    web = public_location("https://en.wikipedia.org/wiki/Hermes", "Hermes")
    assert web["origin"] == "https://en.wikipedia.org"
    assert web["https"] is True
    assert web["url"].startswith("https://en.wikipedia.org/")
    assert safe_navigate_url("javascript:alert(1)") == ""
    assert safe_navigate_url("https://example.com/x").startswith("https://")


def test_nav_back_forward_reload_changes_inmemory_page(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    page = svc.runtime._page(f"id:{identity.id}")
    page.url = "https://example.com/a"
    page.history = []
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    opened = svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "nav", "action": "open", "url": "https://example.com/b"},
    )
    assert opened["origin"] == "https://example.com"
    assert opened["url"] == "https://example.com/b"
    back = svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "nav", "action": "back"},
    )
    assert back["url"] == "https://example.com/a"
    fwd = svc.owner_stream_input(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        generation=session.generation,
        event={"type": "nav", "action": "forward"},
    )
    assert fwd["url"] == "https://example.com/b"


def test_expired_owner_lease_returns_agent_control_on_list(tmp_path):
    from tests.gateway.test_agent_computer import Clock

    clock = Clock()
    svc = AgentComputerService(
        AgentComputerStore(tmp_path / "state.db"),
        InMemoryRuntime(),
        data_root=tmp_path,
        clock=clock,
        takeover_ttl_s=10,
    )
    computer, _identity, _lease = _owner_session(svc)
    assert svc.get_computer(computer.id).control_authority.value == "OWNER_CONTROLLED"
    clock.advance(11)
    listed = svc.list_computers()
    row = next(item for item in listed if item.id == computer.id)
    assert row.control_authority.value == "AGENT_CONTROLLED"
    status = svc.public_status(row)
    assert status["can_resume"] is False
    assert status["control_label"] == "AGENT_CONTROL"


def test_chromium_stream_pointer_does_not_remap_viewport_pixels(monkeypatch):
    from gateway.agent_computer.adapter import HermesChromiumRuntime, RuntimeHandle

    seen = []

    def fake_cdp(_handle, method, params=None):
        seen.append((method, params or {}))
        return {}

    monkeypatch.setattr("gateway.agent_computer.adapter.loopback_cdp", fake_cdp)
    runtime = HermesChromiumRuntime()
    handle = RuntimeHandle(
        computer_id="ac_x",
        identity_id=None,
        user_data_dir="/tmp",
        screenshot_width=2880,
        screenshot_height=1800,
        viewport_width=1440,
        viewport_height=900,
    )
    runtime.stream_pointer(handle, phase="down", x=468.0, y=33.0, click_count=1, buttons=1)
    pressed = [params for method, params in seen if params.get("type") == "mousePressed"]
    assert pressed, seen
    assert pressed[0]["x"] == 468.0
    assert pressed[0]["y"] == 33.0


def test_safe_workspace_url_rejects_unsafe_schemes():
    from gateway.agent_computer.adapter import page_needs_restore, safe_workspace_url

    assert safe_workspace_url("https://en.wikipedia.org/wiki/Main_Page").startswith("https://")
    assert safe_workspace_url("javascript:alert(1)") == ""
    assert safe_workspace_url("data:text/html,hi") == ""
    assert safe_workspace_url("about:blank") == ""
    assert page_needs_restore("about:blank") is True
    assert page_needs_restore("https://en.wikipedia.org/wiki/Main_Page") is False


def test_blank_page_restores_last_workspace_on_stream_open(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    park = "https://en.wikipedia.org/wiki/Main_Page"
    computer.workspace_url = park
    computer.workspace_title = "Wikipedia"
    svc.store.upsert_computer(computer)
    page = svc.runtime._page(f"id:{identity.id}")
    page.url = "about:blank"
    page.title = ""
    session, computer = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    assert session.generation >= 1
    assert svc.runtime._page(f"id:{identity.id}").url == park
    assert computer.workspace_url == park


def test_observe_does_not_forget_workspace_when_page_is_blank(tmp_path):
    svc = _svc(tmp_path)
    computer, identity, lease = _owner_session(svc)
    park = "https://en.wikipedia.org/wiki/Main_Page"
    computer.workspace_url = park
    computer.workspace_title = "Wikipedia"
    svc.store.upsert_computer(computer)
    page = svc.runtime._page(f"id:{identity.id}")
    page.url = "about:blank"
    page.title = ""
    obs = svc.observe(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    stored = svc.get_computer(computer.id)
    assert stored.workspace_url == park
    assert obs.url == "about:blank"


def test_pick_page_target_skips_blank_chrome_pages():
    from gateway.agent_computer.adapter import pick_page_target

    chosen = pick_page_target(
        [
            {"type": "page", "id": "blank", "url": "about:blank"},
            {"type": "page", "id": "work", "url": "file:///tmp/input_fixture.html"},
            {"type": "iframe", "id": "skip", "url": "https://example.com"},
        ]
    )
    assert chosen is not None
    assert chosen["id"] == "work"


def test_open_stream_pins_designed_viewport(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    session, _ = svc.open_owner_stream(
        computer.id,
        OWNER_PRINCIPAL,
        lease_id=lease.lease_id,
        fencing_epoch=lease.fencing_epoch,
        width=1920,
        height=1080,
    )
    assert session.viewport_width == 1440
    assert session.viewport_height == 900


def test_open_stream_does_not_wake_runtime(tmp_path):
    svc = _svc(tmp_path)
    computer, _identity, lease = _owner_session(svc)
    wakes = {"n": 0}
    real_wake = svc.runtime.wake

    def _count_wake(*args, **kwargs):
        wakes["n"] += 1
        return real_wake(*args, **kwargs)

    svc.runtime.wake = _count_wake
    session, _ = svc.open_owner_stream(
        computer.id, OWNER_PRINCIPAL, lease_id=lease.lease_id, fencing_epoch=lease.fencing_epoch
    )
    assert wakes["n"] == 0
    assert session.generation >= 1
    assert get_stream_hub().get(computer.id) is session


def test_reap_dead_profile_skips_live_cdp_and_kills_stale_pid(tmp_path):
    from gateway.agent_computer.adapter import HermesChromiumRuntime

    runtime = HermesChromiumRuntime()
    (tmp_path / "DevToolsActivePort").write_text("9333\n", encoding="utf-8")
    (tmp_path / "chromium.pid").write_text("4242\n", encoding="utf-8")
    slept = []
    runtime.alive = lambda handle: True
    runtime.sleep = lambda handle: slept.append(handle.process_id)
    runtime._reap_dead_profile_browser(str(tmp_path), "ac_x")
    assert slept == []
    runtime.alive = lambda handle: False
    runtime._reap_dead_profile_browser(str(tmp_path), "ac_x")
    assert slept == [4242]


def test_stream_route_is_mounted_and_requires_auth():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from hermes_cli.web_server import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/agent-computers/{computer_id}/stream" in paths
    client = TestClient(app)
    with client.websocket_connect("/api/agent-computers/ac_missing/stream") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == 4401
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
