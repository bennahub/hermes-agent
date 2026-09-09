"""Native regressions independently reproduced against initial candidate 6fe54."""

from types import SimpleNamespace
from pathlib import Path
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def needs_owner(home):
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store

    db = SessionDB(home / "state.db")
    db.create_session("qa", source="desktop")
    mid = db.append_message(
        "qa", "user", "After two minutes verify the file then report"
    )
    work = oc.register_owner_request(db, "qa", mid, hermes_home=home)
    oc.request_finish(
        work["id"], "Owner decision needed", terminal="needs_owner", hermes_home=home
    )
    pending = store.get_work(work["id"], home)
    oc.deliver_pending(pending, home)
    return db, work, pending


def test_negated_explicit_id_does_not_resume(home):
    from agent.autonomy import owner_continuity as oc, store

    db, work, _ = needs_owner(home)
    try:
        mid = db.append_message("qa", "user", "Do not continue " + work["id"])
        assert oc._bind_owner_decision(db, "qa", mid) is None
        assert store.get_work(work["id"], home)["state"] == "needs_owner"
    finally:
        db.close()


def test_stale_outbox_cannot_undo_new_owner_decision(home):
    from agent.autonomy import owner_continuity as oc, store

    db, work, stale = needs_owner(home)
    try:
        mid = db.append_message("qa", "user", "continue")
        oc._bind_owner_decision(db, "qa", mid)
        assert store.get_work(work["id"], home)["state"] == "working"
        oc.deliver_pending(stale, home)
        assert store.get_work(work["id"], home)["state"] == "working"
    finally:
        db.close()


def test_posteffect_hook_failure_does_not_discard_result(home, monkeypatch):
    from agent.autonomy import owner_continuity as oc
    from agent import tool_executor as te

    def unavailable(*a, **kw):
        raise OSError("synthetic ledger failure")

    monkeypatch.setattr(oc, "observe_dispatch", unavailable)
    calls = []
    monkeypatch.setattr(
        te,
        "_flush_session_db_after_tool_progress",
        lambda *a, **kw: calls.append("flush") or True,
    )
    monkeypatch.setattr(te, "maybe_persist_tool_result", lambda **kw: kw["content"])
    monkeypatch.setattr(te, "get_active_env", lambda _: None)
    monkeypatch.setattr(te, "_record_persisted_path_for_stub", lambda *a: None)
    agent = SimpleNamespace(
        _current_tool="terminal",
        _touch_activity=lambda _: None,
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda *a: None),
        _tool_result_content_for_active_model=lambda name, result: result,
        tool_progress_callback=None,
    )
    ref = SimpleNamespace(
        name="terminal", args={"background": True}, call_id="qa-tool", task_id="qa"
    )
    messages = []
    te._commit_tool_result(
        agent,
        messages,
        ref,
        '{"session_id":"qa-process"}',
        budget=None,
        tool_duration=0.1,
        is_error=False,
        blocked=False,
        effect_disposition="applied",
    )
    assert calls == ["flush"] and messages[-1]["role"] == "tool"


def test_finish_hook_failure_cannot_convert_success_to_failed_turn(home, monkeypatch):
    from agent import conversation_loop as loop, agent_runtime_helpers as helpers
    from agent.autonomy import owner_continuity as oc

    result = {"final_response": "QA success", "completed": True}
    observed = []
    monkeypatch.setattr(
        loop, "_run_conversation_with_authority", lambda *a, **kw: result
    )
    monkeypatch.setattr(helpers, "clear_turn_authority", lambda *a: None)

    def unavailable(agent, value):
        observed.append(value)
        raise OSError("synthetic continuity failure")

    monkeypatch.setattr(oc, "finish_turn", unavailable)
    assert loop.run_conversation(SimpleNamespace()) is result
    assert observed == [result]


@pytest.mark.parametrize(
    "text",
    [
        "Do not continue {id}",
        "Should I continue {id}?",
        "لا تتابع {id}",
        "continue {id} if approval arrives",
    ],
)
def test_conditional_or_negated_work_ids_never_resume(home, text):
    from agent.autonomy import owner_continuity as oc, store

    db, work, _ = needs_owner(home)
    try:
        mid = db.append_message("qa", "user", text.format(id=work["id"]))
        assert oc._bind_owner_decision(db, "qa", mid) is None
        assert store.get_work(work["id"], home)["state"] == "needs_owner"
    finally:
        db.close()


def test_automatic_terminal_promotion_registers_before_owner_promise(home):
    from agent.autonomy import owner_continuity as oc, store
    from hermes_state import SessionDB

    db = SessionDB(home / "state.db")
    db.create_session("qa", source="desktop")
    mid = db.append_message("qa", "user", "Run the QA command")
    agent = SimpleNamespace(
        _session_db=db,
        session_id="qa",
        _owner_continuity_source=("qa", mid),
        _owner_continuity_work_id=None,
    )
    try:
        assert store.list_work(home) == []
        result = oc.observe_dispatch(
            agent,
            "terminal",
            {"command": "synthetic QA command"},
            '{"session_id":"auto-background-qa","output":"Command still running"}',
        )
        work = store.get_work(agent._owner_continuity_work_id, home)
        assert work["state"] == "waiting"
        assert work["refs"]["resume"]["id"] == "process:auto-background-qa"
        assert "owner_continuity" in result
    finally:
        db.close()


@pytest.mark.parametrize(
    "text",
    [
        "Fix the broken UI",
        "Build the app",
        "Deploy the approved candidate",
        "أصلح مشكلة الكيبورد",
    ],
)
def test_direct_substantial_owner_work_does_not_need_future_tense(text):
    from agent.autonomy.owner_continuity import requires_continuation

    assert requires_continuation(text)


def test_explicit_stop_fences_queued_native_resume_and_only_originating_session(home):
    from agent.autonomy import owner_continuity as oc, store
    from hermes_state import SessionDB

    db = SessionDB(home / "state.db")
    try:
        works = []
        for sid in ("owner-a", "owner-b"):
            db.create_session(sid, source="desktop")
            mid = db.append_message(
                sid, "user", "After two minutes verify the file then report"
            )
            work = oc.register_owner_request(db, sid, mid, hermes_home=home)
            oc.wait(work["id"], until="2026-01-01T00:00:00Z", hermes_home=home)
            works.append(work)
        assert oc.cancel_owner_session("owner-a", hermes_home=home) == 1
        assert not oc.resume_due(store.get_work(works[0]["id"], home))
        assert oc.resume_due(store.get_work(works[1]["id"], home))
        calls = []
        oc.run_resume(
            works[0]["id"], 1, hermes_home=home, runner=lambda *a, **kw: calls.append(1)
        )
        assert not calls
        oc.deliver_pending(store.get_work(works[0]["id"], home), home)
        assert store.get_work(works[0]["id"], home)["state"] == "cancelled"
        assert store.get_work(works[1]["id"], home)["state"] == "waiting"
    finally:
        db.close()


def test_native_stop_rpc_cancels_durable_work_but_shared_disconnect_helper_does_not(
    home, monkeypatch
):
    import threading
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store
    from tui_gateway import server

    db = SessionDB(home / "state.db")
    db.create_session("qa-stop", source="desktop")
    mid = db.append_message(
        "qa-stop", "user", "After two minutes verify the file then report"
    )
    work = oc.register_owner_request(db, "qa-stop", mid, hermes_home=home)
    oc.wait(work["id"], until="2026-01-01T00:00:00Z", hermes_home=home)
    calls = []
    session = {
        "agent": SimpleNamespace(
            session_id="qa-stop", interrupt=lambda: calls.append("stop")
        ),
        "profile_home": str(home),
        "session_key": "qa-stop",
        "history_lock": threading.RLock(),
        "running": True,
        "_run_thread": None,
    }
    monkeypatch.setattr(server, "_tts_stream_stop", lambda: None)
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_sess", lambda *a: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _: False)
    monkeypatch.setattr(server, "_clear_pending", lambda _: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a: None)
    try:
        server._interrupt_session_turn("ui-qa", session)
        assert oc.resume_due(store.get_work(work["id"], home))
        session["running"] = True
        response = server._methods["session.interrupt"]("stop", {"session_id": "ui-qa"})
        assert response["result"]["owner_continuity_status"] == "stop_recorded"
        assert calls == ["stop", "stop"]
        assert not oc.resume_due(store.get_work(work["id"], home))
    finally:
        db.close()


@pytest.mark.parametrize(
    "dispatch,expected",
    [
        ({"pid": 9000001, "process_started_at": "same"}, [9000001]),
        ({"pid": 9000001, "process_started_at": "old"}, []),
        ({}, []),
    ],
)
def test_stop_only_terminates_exact_owned_continuation_process(
    home, monkeypatch, dispatch, expected
):
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store
    from gateway import status
    from agent import deadline

    db = SessionDB(home / "state.db")
    db.create_session("qa-stop", source="desktop")
    mid = db.append_message(
        "qa-stop", "user", "After two minutes verify the file then report"
    )
    work = oc.register_owner_request(db, "qa-stop", mid, hermes_home=home)
    store.update_work(
        work["id"],
        refs={
            "dispatch": dispatch,
            "owner_process": {"pid": 9000002, "started_at": "same"},
        },
        hermes_home=home,
    )
    killed = []
    monkeypatch.setattr(status, "get_process_start_time", lambda _: "same")
    monkeypatch.setattr(
        deadline, "kill_process_tree", lambda pid: killed.append(pid) or True
    )
    try:
        assert oc.cancel_owner_session("qa-stop", hermes_home=home) == 1
        assert killed == expected
        assert (
            store.get_work(work["id"], home)["refs"]["pending_owner_result"]["state"]
            == "cancelled"
        )
    finally:
        db.close()


def test_explicit_stop_cannot_be_overwritten_by_late_completion(home):
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store

    db = SessionDB(home / "state.db")
    db.create_session("qa-stop", source="desktop")
    try:
        mid = db.append_message("qa-stop", "user", "Build the QA file then report")
        work = oc.register_owner_request(db, "qa-stop", mid, hermes_home=home)
        artifact = home / "qa.txt"
        artifact.write_text("qa")
        oc.verify(work["id"], file=str(artifact), hermes_home=home)
        assert oc.cancel_owner_session("qa-stop", hermes_home=home) == 1
        assert (
            store.get_work(work["id"], home)["refs"]["pending_owner_result"]["state"]
            == "cancelled"
        )
        try:
            oc.request_finish(
                work["id"], "Late completion from interrupted worker", hermes_home=home
            )
        except ValueError:
            pass
        oc.deliver_pending(store.get_work(work["id"], home), home)
        assert store.get_work(work["id"], home)["state"] == "cancelled"
    finally:
        db.close()


def test_stop_before_auto_background_result_cannot_create_new_wait(home):
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store

    db = SessionDB(home / "state.db")
    db.create_session("qa-late", source="desktop")
    try:
        mid = db.append_message("qa-late", "user", "Run the QA command")
        assert oc.register_owner_request(db, "qa-late", mid, hermes_home=home) is None
        oc.cancel_owner_session("qa-late", hermes_home=home)
        agent = SimpleNamespace(
            _session_db=db,
            session_id="qa-late",
            _owner_continuity_work_id=None,
            _owner_continuity_source=("qa-late", mid),
        )
        oc.observe_dispatch(
            agent,
            "terminal",
            {"command": "sleep 60", "background": False},
            {"session_id": "qa-late-process"},
        )
        rows = store.list_work(home)
        assert all(
            w["state"] == "cancelled"
            or (w["refs"].get("pending_owner_result") or {}).get("state") == "cancelled"
            for w in rows
        )
    finally:
        db.close()


def test_stop_fence_does_not_block_later_legitimate_owner_source(home):
    from hermes_state import SessionDB
    from agent.autonomy import owner_continuity as oc, store

    db = SessionDB(home / "state.db")
    db.create_session("qa", source="desktop")
    try:
        old = db.append_message("qa", "user", "Run QA")
        oc.cancel_owner_session("qa", hermes_home=home)
        stopped = oc.register_owner_request(db, "qa", old, hermes_home=home, force=True)
        assert stopped["refs"]["owner_stop"]
        with pytest.raises(ValueError):
            oc._work_hint(stopped)
        with pytest.raises(ValueError):
            oc.prepare_dispatch(
                SimpleNamespace(_owner_continuity_work_id=stopped["id"]),
                "terminal",
                {"command": "effect must not run"},
            )
        with pytest.raises(ValueError):
            oc.wait(stopped["id"], until="2026-01-01T00:00:00Z", hermes_home=home)
        for change in (
            {"state": "waiting", "refs": {"owner_stop": None}},
            {"refs": {"pending_owner_result": None}},
            {"state": "completed", "completion_result": "late"},
        ):
            with pytest.raises(ValueError):
                store.update_work(stopped["id"], hermes_home=home, **change)
        new = db.append_message("qa", "user", "Build a new QA artifact")
        fresh = oc.register_owner_request(db, "qa", new, hermes_home=home)
        assert fresh["id"] != stopped["id"] and not fresh["refs"].get("owner_stop")
        oc.wait(fresh["id"], until="2026-01-01T00:00:00Z", hermes_home=home)
        assert oc.resume_due(store.get_work(fresh["id"], home))
    finally:
        db.close()
