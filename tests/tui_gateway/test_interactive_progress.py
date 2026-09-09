from tui_gateway.interactive_progress import record, computer_notice


def test_timings_are_monotonic_first_occurrences_without_content(caplog):
    session = {}
    record(session, "message.start", {}, monotonic=100)
    record(session, "thinking.delta", {"text": "private"}, monotonic=102)
    record(session, "tool.start", {"tool_id": "a", "name": "read_file"}, monotonic=104)
    record(session, "tool.start", {"tool_id": "b", "name": "read_file"}, monotonic=107)
    record(session, "message.delta", {"text": "private"}, monotonic=108)
    with caplog.at_level("INFO"):
        record(session, "message.complete", {}, monotonic=110)
    assert session["_interactive_progress"]["stages"] == {
        "TURN_START": 0, "FIRST_REASONING_DELTA": 2, "FIRST_TOOL_CALL": 4,
        "FIRST_VISIBLE_TEXT": 8, "TURN_COMPLETE": 10}
    assert "private" not in caplog.text
    assert "read_file" not in caplog.text
    record(session, "tool.start", {"tool_id": "late", "name": "computer_wake"}, monotonic=111)
    assert not session["_interactive_progress"]["tools"]


def test_material_computer_failure_once_per_condition_per_turn():
    session = {}
    result = {"error": "private detail", "error_code": "COMPUTER_CAPACITY_EXHAUSTED"}
    record(session, "message.start", {})
    assert computer_notice(session, "unknown", "computer_wake", result) is None
    record(session, "tool.start", {"tool_id": "a", "name": "computer_wake"})
    notice = computer_notice(session, "a", "computer_wake", result)
    assert notice["level"] == "warning"
    assert "private" not in str(notice)
    assert computer_notice(session, "a", "computer_wake", result) is None
    record(session, "tool.start", {"tool_id": "b", "name": "computer_wake"})
    assert computer_notice(session, "b", "computer_wake", result) is None
    record(session, "message.complete", {})
    assert computer_notice(session, "b", "computer_wake", result) is None
    record(session, "message.start", {})
    record(session, "tool.start", {"tool_id": "c", "name": "computer_wake"})
    next_notice = computer_notice(session, "c", "computer_wake", result)
    assert next_notice is not None
    assert next_notice["key"] != notice["key"]


def test_unrelated_success_and_forged_conditions_do_not_emit():
    session = {}
    record(session, "message.start", {})
    for i, name, result in [
        ("a", "read_file", {"error": "x", "error_code": "COMPUTER_CAPACITY_EXHAUSTED"}),
        ("b", "computer_wake", {"error_code": "COMPUTER_CAPACITY_EXHAUSTED"}),
        ("c", "computer_wake", {"error": "x", "error_code": "unknown"}),
    ]:
        record(session, "tool.start", {"tool_id": i, "name": name})
        assert computer_notice(session, i, name, result) is None
    assert session["_interactive_progress"]["tools"] == {}


def test_real_completion_callback_surfaces_failure_with_tool_progress_off(monkeypatch, tmp_path):
    import json
    from tui_gateway import server
    frames = []
    session = {"running": True}
    monkeypatch.setattr(server, "_sessions", {"qa": session})
    monkeypatch.setattr(server, "write_json", frames.append)
    monkeypatch.setattr(server, "_consider_push", lambda *a: None)
    monkeypatch.setattr(server, "_session_live_status", lambda *a: "working")
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda *a: False)
    monkeypatch.setattr(server, "_session_verbose", lambda *a: False)
    monkeypatch.setattr(server, "_session_inline_diffs", lambda *a: False)
    server._emit("message.start", "qa", {})
    server._on_tool_start("qa", "call-a", "computer_wake", {})
    server._on_tool_complete("qa", "call-a", "computer_wake", {}, json.dumps({
        "error": "All active slots occupied", "error_code": "COMPUTER_CAPACITY_EXHAUSTED"}))
    notices = [f for f in frames if f.get("params", {}).get("type") == "notification.show"]
    assert len(notices) == 1, frames
    assert "Computer could not start" in notices[0]["params"]["payload"]["text"]
