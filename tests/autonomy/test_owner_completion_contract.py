"""Owner responsibility may not disappear in a prose wait or silent completion."""

import pytest

from agent.autonomy import store


def owner_work(home):
    return store.start_work(
        why="Owner asked for a delayed QA check",
        outcome="Check the QA artifact and report its verified result",
        done_contract="Artifact verified and result durably delivered to owner",
        idempotency_key="owner:qa-session:message-123",
        refs={
            "owner_request": {"session_id": "qa-session", "message_id": "message-123"}
        },
        hermes_home=home,
    )["work"]


def test_native_resume_timeout_rejects_nonfinite_values(monkeypatch):
    from agent.autonomy.owner_continuity import _native_resume_timeout

    for raw in ("nan", "inf", "-inf"):
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", raw)
        assert _native_resume_timeout() == 1800.0


def test_owner_wait_rejects_prose_without_durable_resume(autonomy_home):
    work = owner_work(autonomy_home)
    with pytest.raises(ValueError):
        store.update_work(
            work["id"],
            state="waiting",
            waiting_reason="I'll check in two minutes",
            hermes_home=autonomy_home,
        )
    assert store.get_work(work["id"], autonomy_home)["state"] == "working"


def test_owner_completion_requires_verification_and_delivery(autonomy_home):
    work = owner_work(autonomy_home)
    with pytest.raises(ValueError):
        store.complete_work(work["id"], "[SILENT]", hermes_home=autonomy_home)
    assert store.get_work(work["id"], autonomy_home)["state"] != "completed"


def native_owner(
    home,
    text="After two minutes check the QA artifact then report the result",
    session="owner-chat",
):
    from hermes_state import SessionDB
    from agent.autonomy.owner_continuity import register_owner_request

    db = SessionDB(home / "state.db")
    db.create_session(session, source="desktop")
    message_id = db.append_message(session, "user", text)
    work = register_owner_request(db, session, message_id, hermes_home=home)
    return db, work, message_id


def test_native_source_classifier_and_repeated_owner_identity(autonomy_home):
    from agent.autonomy.owner_continuity import register_owner_request
    from hermes_state import SessionDB

    db = SessionDB(autonomy_home / "state.db")
    db.create_session("owner", source="desktop")
    try:
        for text in [
            "Thanks",
            "كيف تعمل الذاكرة؟",
            "What is a compiler?",
            "Read the file",
        ]:
            mid = db.append_message("owner", "user", text)
            assert (
                register_owner_request(db, "owner", mid, hermes_home=autonomy_home)
                is None
            )
        text = "بعد دقيقتين تحقق من وجود ملف QA ثم نفذ خطوة QA التالية وارجع بالنتيجة."
        first = db.append_message("owner", "user", text)
        a = register_owner_request(db, "owner", first, hermes_home=autonomy_home)
        assert (
            register_owner_request(db, "owner", first, hermes_home=autonomy_home)["id"]
            == a["id"]
        )
        second = db.append_message("owner", "user", text)
        b = register_owner_request(db, "owner", second, hermes_home=autonomy_home)
        assert a["id"] != b["id"]
        foreign = db.append_message(
            "owner", "user", text, display_kind="async_delegation_complete"
        )
        with pytest.raises(ValueError):
            register_owner_request(db, "owner", foreign, hermes_home=autonomy_home)
    finally:
        db.close()


def test_native_file_verification_and_idempotent_final_delivery_after_disconnect(
    autonomy_home,
):
    from agent.autonomy.owner_continuity import (
        verify,
        request_finish,
        deliver_pending,
        reconcile,
    )

    db, work, mid = native_owner(autonomy_home)
    artifact = autonomy_home / "qa.txt"
    artifact.write_text("verified synthetic artifact")
    try:
        with pytest.raises(ValueError):
            request_finish(work["id"], "Done", hermes_home=autonomy_home)
        verify(work["id"], file=str(artifact), hermes_home=autonomy_home)
        request_finish(
            work["id"],
            "QA artifact verified; requested result complete.",
            hermes_home=autonomy_home,
        )
        assert store.get_work(work["id"], autonomy_home)["state"] == "working"
        # No client is connected. Native transcript commit is the durable delivery.
        assert deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        completed = store.get_work(work["id"], autonomy_home)
        assert completed["state"] == "completed"
        assert completed["refs"]["owner_delivery"]["session_id"] == "owner-chat"
        assert (
            len([r for r in db.get_messages("owner-chat") if r["role"] == "assistant"])
            == 1
        )
        reconcile(autonomy_home)
        assert (
            len([r for r in db.get_messages("owner-chat") if r["role"] == "assistant"])
            == 1
        )
        store.update_work(work["id"], state="working", hermes_home=autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "completed"
    finally:
        db.close()


def test_wait_timer_survives_store_reopen_and_native_cron_has_one_bound_job(
    autonomy_home,
):
    from datetime import datetime, timezone, timedelta
    from agent.autonomy.owner_continuity import wait, reconcile, resume_due
    from cron.jobs import list_jobs, use_cron_store

    db, work, _ = native_owner(autonomy_home)
    try:
        at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        wait(work["id"], until=at, hermes_home=autonomy_home)
        assert resume_due(store.get_work(work["id"], autonomy_home))
        assert reconcile(autonomy_home) == 1
        assert reconcile(autonomy_home) == 0
        with use_cron_store(autonomy_home):
            jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 1 and jobs[0]["no_agent"] is True
        assert jobs[0]["name"].startswith("owner-continuation:" + work["id"])
        assert (
            autonomy_home / "scripts" / ("owner-continuation-" + work["id"] + "-1.py")
        ).is_file()
    finally:
        db.close()


def test_end_of_turn_registers_followup_and_approval_never_auto_resumes(autonomy_home):
    from types import SimpleNamespace
    from agent.autonomy.owner_continuity import (
        finish_turn,
        request_finish,
        deliver_pending,
        reconcile,
        wait,
    )

    db, work, _ = native_owner(autonomy_home)
    try:
        agent = SimpleNamespace(_owner_continuity_work_id=work["id"], _session_db=db)
        finish_turn(
            agent, {"completed": True, "final_response": "I'll come back later"}
        )
        waiting = store.get_work(work["id"], autonomy_home)
        assert (
            waiting["state"] == "waiting"
            and waiting["refs"]["resume"]["kind"] == "until"
        )
        request_finish(
            work["id"],
            "I need Owner approval for the next action.",
            terminal="needs_owner",
            hermes_home=autonomy_home,
        )
        deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "needs_owner"
        assert reconcile(autonomy_home, now=10**12) == 0
        with pytest.raises(ValueError):
            wait(work["id"], until="2026-09-01T00:00:00Z", hermes_home=autonomy_home)
    finally:
        db.close()


def test_native_child_completion_wakes_only_matching_source_without_completing_parent(
    autonomy_home,
):
    import sqlite3
    from datetime import datetime, timezone, timedelta
    from agent.autonomy.owner_continuity import wait, reconcile, signal_event
    from tools.async_delegation import _initialize_schema

    db, work, _ = native_owner(autonomy_home)
    conn = sqlite3.connect(autonomy_home / "state.db")
    try:
        _initialize_schema(conn)
        conn.execute(
            "INSERT INTO async_delegations (delegation_id,origin_session,parent_session_id,state,dispatched_at,updated_at,completed_at) VALUES (?,?,?,?,?,?,?)",
            ("child-1", "owner-chat", "other-chat", "completed", 1, 2, 2),
        )
        conn.commit()
        wait(
            work["id"],
            child="child-1",
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            hermes_home=autonomy_home,
        )
        assert reconcile(autonomy_home) == 0
        assert (
            signal_event(work["id"], "foreign-child", hermes_home=autonomy_home)
            is False
        )
        conn.execute(
            "UPDATE async_delegations SET parent_session_id='owner-chat' WHERE delegation_id='child-1'"
        )
        conn.commit()
        assert reconcile(autonomy_home) == 1
        current = store.get_work(work["id"], autonomy_home)
        assert current["state"] == "waiting" and not current["refs"].get("verification")
        assert current["refs"]["resume_event"]["payload"]["native_state"] == "completed"
        assert reconcile(autonomy_home) == 0
    finally:
        conn.close()
        db.close()


def test_generation_fences_stale_events_and_native_resume_replay(autonomy_home, monkeypatch):
    from agent.autonomy.owner_continuity import wait, signal_event, run_resume
    from types import SimpleNamespace

    db, work, _ = native_owner(autonomy_home)
    try:
        wait(
            work["id"],
            event="first",
            deadline="2099-01-01T00:00:00Z",
            hermes_home=autonomy_home,
        )
        assert signal_event(work["id"], "first", hermes_home=autonomy_home)
        wait(
            work["id"],
            event="second",
            deadline="2099-01-01T00:00:00Z",
            hermes_home=autonomy_home,
        )
        assert not signal_event(work["id"], "first", hermes_home=autonomy_home)
        assert not store.get_work(work["id"], autonomy_home)["refs"].get("resume_event")
        called = []
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "420")

        def native_boundary(argv, **kwargs):
            called.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        assert (
            run_resume(work["id"], 1, hermes_home=autonomy_home, runner=native_boundary)
            == 0
        )
        assert called == []
        signal_event(work["id"], "second", hermes_home=autonomy_home)
        assert (
            run_resume(work["id"], 2, hermes_home=autonomy_home, runner=native_boundary)
            == 0
        )
        assert (
            run_resume(work["id"], 2, hermes_home=autonomy_home, runner=native_boundary)
            == 0
        )
        assert len(called) == 1
        argv, kwargs = called[0]
        assert argv[argv.index("--resume") + 1] == "owner-chat"
        assert kwargs["env"]["HERMES_BACKGROUND_DELIVERY"] == "1"
        assert kwargs["env"]["HERMES_OWNER_CONTINUATION_ID"] == work["id"]
        assert kwargs["timeout"] == 420.0
        assert "never replay" in kwargs["input"]
        assert store.get_work(work["id"], autonomy_home)["state"] == "waiting"
    finally:
        db.close()


def test_restart_unknown_effect_requires_owner_and_never_replays(
    autonomy_home, monkeypatch
):
    import time
    from agent.autonomy.owner_continuity import reconcile

    db, work, _ = native_owner(autonomy_home)
    try:
        store.update_work(
            work["id"],
            refs={
                "heartbeat_at": time.time() - 1000,
                "owner_process": {"pid": 999999999, "started_at": 1},
            },
            hermes_home=autonomy_home,
        )
        reconcile(autonomy_home)
        reconcile(autonomy_home)
        current = store.get_work(work["id"], autonomy_home)
        assert current["state"] == "needs_owner"
        assert current["refs"]["owner_delivery"]["message_id"]
        assert not current["refs"].get("resume_job")
        assert (
            len([r for r in db.get_messages("owner-chat") if r["role"] == "assistant"])
            == 1
        )
    finally:
        db.close()


def test_outbox_waits_native_turn_lease_and_recovers_postappend_crash(
    autonomy_home, monkeypatch
):
    from agent.autonomy.owner_continuity import request_finish, deliver_pending

    db, work, _ = native_owner(autonomy_home)
    try:
        request_finish(
            work["id"],
            "QA has failed; no consequential action was retried.",
            terminal="failed",
            hermes_home=autonomy_home,
        )
        pending = store.get_work(work["id"], autonomy_home)
        assert db.try_acquire_session_turn_lease(
            "owner-chat", "real-active-owner", ttl_seconds=30, patience_s=0
        )
        assert deliver_pending(pending, autonomy_home) is False
        assert len(db.get_messages("owner-chat")) == 1
        db.release_session_turn_lease("owner-chat", "real-active-owner")
        original = store.update_work

        def crash_after_append(*args, **kwargs):
            raise RuntimeError(
                "simulated process stopped after durable transcript commit"
            )

        monkeypatch.setattr(store, "update_work", crash_after_append)
        with pytest.raises(RuntimeError):
            deliver_pending(pending, autonomy_home)
        monkeypatch.setattr(store, "update_work", original)
        assert deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "failed"
        assert (
            len([r for r in db.get_messages("owner-chat") if r["role"] == "assistant"])
            == 1
        )
    finally:
        db.close()


def test_tool_verification_rejects_foreign_task_and_failed_observations(autonomy_home):
    from agent.autonomy.owner_continuity import verify

    db, work, _ = native_owner(autonomy_home)
    try:
        foreign = db.append_message(
            "owner-chat",
            "tool",
            '{"success":true}',
            tool_name="read_file",
            tool_call_id="t1",
        )
        with pytest.raises(ValueError):
            verify(work["id"], tool_message_id=str(foreign), hermes_home=autonomy_home)
        failed = db.append_message(
            "owner-chat",
            "tool",
            '{"exit_code":1,"output":"missing"}',
            tool_name="terminal",
            tool_call_id="t2",
            display_metadata={"owner_work_id": work["id"]},
        )
        with pytest.raises(ValueError):
            verify(work["id"], tool_message_id=str(failed), hermes_home=autonomy_home)
        valid = db.append_message(
            "owner-chat",
            "tool",
            '{"success":true,"output":"QA artifact present with expected content"}',
            tool_name="read_file",
            tool_call_id="t3",
            display_metadata={"owner_work_id": work["id"]},
        )
        assert verify(
            work["id"], tool_message_id=str(valid), hermes_home=autonomy_home
        )["refs"]["verification"]["message_id"] == str(valid)
    finally:
        db.close()


def test_trusted_owner_binding_and_actual_async_registration_only(autonomy_home):
    from types import SimpleNamespace
    from tools.owner_task_authority import (
        mint_pending,
        bind_turn as grant_turn,
        clear_turn,
    )
    from agent.autonomy.owner_continuity import (
        bind_turn,
        prepare_dispatch,
        observe_dispatch,
    )
    from hermes_state import SessionDB

    db = SessionDB(autonomy_home / "state.db")
    db.create_session("owner", source="desktop")
    mid = db.append_message("owner", "user", "Delegate the QA check")
    agent = SimpleNamespace(
        _session_db=db,
        session_id="owner",
        _current_turn_id="owner-turn",
        platform="desktop",
    )
    try:
        # Identical prose without trusted ingress cannot mint responsibility.
        assert bind_turn(agent, {"_row_id": mid}) == ""
        prepare_dispatch(agent, "delegate_task", {})
        assert store.list_work(autonomy_home) == []
        nonce = mint_pending("owner", profile_home=str(autonomy_home))
        grant_turn(["owner"], "owner-turn", nonce=nonce)
        bind_turn(agent, {"_row_id": mid})
        prepare_dispatch(agent, "delegate_task", {})
        work = store.get_work(agent._owner_continuity_work_id, autonomy_home)
        assert work["refs"]["owner_request"]["message_id"] == str(mid)
        result = observe_dispatch(
            agent,
            "delegate_task",
            {},
            '{"status":"dispatched","delegation_id":"actual-child"}',
        )
        assert "actual-child" in result
        assert (
            store.get_work(work["id"], autonomy_home)["refs"]["resume"]["id"]
            == "actual-child"
        )
    finally:
        clear_turn("owner-turn")
        db.close()


def test_actual_agent_persists_owner_work_before_provider_and_keeps_system_prefix(
    autonomy_home,
):
    from unittest.mock import patch, MagicMock
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from tools.owner_task_authority import mint_pending
    from tests.run_agent.test_run_agent import _mock_response

    db = SessionDB(autonomy_home / "state.db")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="synthetic-test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            session_db=db,
            session_id="native-owner",
            platform="api_server",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "BYTE STABLE SYSTEM"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._pending_owner_task_nonce = mint_pending(
        "native-owner", profile_home=str(autonomy_home)
    )
    stages = []
    agent._interactive_stage_callback = lambda stage, at: stages.append(stage)
    calls = []

    def provider(**kwargs):
        durable = store.list_work(autonomy_home)
        assert len(durable) == 1
        source = durable[0]["refs"]["owner_request"]
        assert source["session_id"] == "native-owner"
        rows = db.get_messages("native-owner")
        assert any(
            str(r["id"]) == source["message_id"] and r["role"] == "user" for r in rows
        )
        assert agent._cached_system_prompt == "BYTE STABLE SYSTEM"
        assert any(
            "Owner task continuity" in str(m.get("content", ""))
            for m in kwargs["messages"]
            if m["role"] == "user"
        )
        calls.append(kwargs)
        return _mock_response(
            content="The verification is scheduled; I will return with the outcome.",
            finish_reason="stop",
        )

    agent.client.chat.completions.create.side_effect = provider
    try:
        with (
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_save_trajectory"),
            patch(
                "agent.auxiliary_client.call_llm",
                return_value=_mock_response(
                    content='{"objective":"Verify the QA file after two minutes and report",'
                            '"permitted":["Verify the QA file after two minutes","Report the result"],'
                            '"excluded":["Unrelated work"]}',
                    finish_reason="stop",
                ),
            ),
        ):
            agent.run_conversation(
                "After two minutes verify the QA file then report the result"
            )
        assert len(calls) == 1
        assert store.list_work(autonomy_home)[0]["state"] == "waiting"
        assert stages[:2] == ["CONTEXT_BUILD_START", "CONTEXT_BUILD_DONE"]
        visible = [r for r in db.get_messages("native-owner") if r["role"] == "user"]
        assert (
            len(visible) == 1 and "Owner task continuity" not in visible[0]["content"]
        )
    finally:
        agent.close()
        db.close()


def test_explicit_owner_decision_rebinds_same_task_without_resetting_generation(
    autonomy_home,
):
    from types import SimpleNamespace
    from tools.owner_task_authority import mint_pending, bind_turn as grant, clear_turn
    from agent.autonomy.owner_continuity import (
        request_finish,
        deliver_pending,
        bind_turn,
        wait,
    )

    db, work, _ = native_owner(autonomy_home)
    try:
        wait(work["id"], until="2099-01-01T00:00:00Z", hermes_home=autonomy_home)
        request_finish(
            work["id"],
            "Owner approval is needed.",
            terminal="needs_owner",
            hermes_home=autonomy_home,
        )
        deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        with pytest.raises(ValueError):
            store.update_work(work["id"], state="working", hermes_home=autonomy_home)
        mid = db.append_message("owner-chat", "user", "كمل")
        nonce = mint_pending("owner-chat", profile_home=str(autonomy_home))
        grant("owner-chat", "decision-turn", nonce=nonce)
        agent = SimpleNamespace(
            _session_db=db,
            session_id="owner-chat",
            platform="api_server",
            _current_turn_id="decision-turn",
        )
        bind_turn(agent, {"_row_id": mid, "content": "كمل"})
        resumed = store.get_work(work["id"], autonomy_home)
        assert (
            agent._owner_continuity_work_id == work["id"]
            and resumed["state"] == "working"
        )
        assert resumed["refs"]["owner_decision"]["message_id"] == str(mid)
        assert resumed["refs"]["resume_generation"] > 1
        assert len(store.list_work(autonomy_home)) == 1
    finally:
        clear_turn("decision-turn")
        db.close()


def test_native_process_completion_event_is_bound_and_never_marks_success(
    autonomy_home, monkeypatch
):
    from tools.process_registry import ProcessRegistry, ProcessSession
    import tools.process_registry as module
    from agent.autonomy.owner_continuity import wait, resume_due

    monkeypatch.setattr(module, "CHECKPOINT_PATH", autonomy_home / "processes.json")
    db, work, _ = native_owner(autonomy_home)
    registry = ProcessRegistry()
    try:
        process = ProcessSession(
            id="qa-process",
            command="synthetic fixture",
            parent_session_id="owner-chat",
            owner_continuity_work_id=work["id"],
            owner_continuity_home=str(autonomy_home),
        )
        registry._running[process.id] = process
        wait(
            work["id"],
            external="process:" + process.id,
            deadline="2099-01-01T00:00:00Z",
            hermes_home=autonomy_home,
        )
        registry._finish_exited(process, 1)
        current = store.get_work(work["id"], autonomy_home)
        assert resume_due(current)
        assert current["refs"]["resume_event"]["payload"]["exit_code"] == 1
        assert current["state"] == "waiting" and not current["refs"].get("verification")
    finally:
        db.close()


def test_final_paraphrase_is_delivered_once_but_later_owner_turn_cannot_donate_result(
    autonomy_home,
):
    from agent.autonomy.owner_continuity import request_finish, deliver_pending

    db, work, _ = native_owner(autonomy_home)
    try:
        request_finish(
            work["id"],
            "QA failed; file missing.",
            terminal="failed",
            hermes_home=autonomy_home,
        )
        final = db.append_message(
            "owner-chat",
            "assistant",
            "The QA file was absent, so I could not verify the requested outcome.",
            display_metadata={"owner_work_id": work["id"],
                "owner_result_id": store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]["id"]},
        )
        assert deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["refs"]["owner_delivery"][
            "message_id"
        ] == str(final)
        assert (
            len([r for r in db.get_messages("owner-chat") if r["role"] == "assistant"])
            == 1
        )
        mid = db.append_message(
            "owner-chat",
            "user",
            "After two minutes verify the second QA file then report",
        )
        from agent.autonomy.owner_continuity import register_owner_request

        other = register_owner_request(db, "owner-chat", mid, hermes_home=autonomy_home)
        request_finish(
            other["id"],
            "Second QA failed.",
            terminal="failed",
            hermes_home=autonomy_home,
        )
        db.append_message("owner-chat", "user", "What time is it?")
        unrelated = db.append_message("owner-chat", "assistant", "It is 10:00.")
        assert deliver_pending(
            store.get_work(other["id"], autonomy_home), autonomy_home
        )
        assert store.get_work(other["id"], autonomy_home)["refs"]["owner_delivery"][
            "message_id"
        ] != str(unrelated)
        assert db.get_messages("owner-chat")[-1]["content"] == "Second QA failed."
    finally:
        db.close()


def test_one_bad_source_does_not_hide_another_owners_due_work(autonomy_home):
    from agent.autonomy.owner_continuity import request_finish, wait, reconcile

    db, good, _ = native_owner(autonomy_home)
    try:
        bad = store.start_work(
            why="Synthetic missing source",
            outcome="Must remain visible as pending",
            done_contract="deliver",
            idempotency_key="bad",
            refs={"owner_request": {"session_id": "missing", "message_id": "123"}},
            hermes_home=autonomy_home,
        )["work"]
        request_finish(
            bad["id"],
            "Missing source needs repair.",
            terminal="failed",
            hermes_home=autonomy_home,
        )
        wait(good["id"], until="2026-01-01T00:00:00Z", hermes_home=autonomy_home)
        assert reconcile(autonomy_home) == 1
        assert store.get_work(bad["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert store.get_work(good["id"], autonomy_home)["refs"]["resume_job"]
    finally:
        db.close()


def test_context_stage_failure_never_emits_done():
    from tests.agent.test_turn_context import _FakeAgent, _build

    agent = _FakeAgent()
    stages = []
    agent._interactive_stage_callback = lambda stage, at: stages.append(stage)

    def failing_stdio():
        raise RuntimeError("synthetic setup failure")

    with pytest.raises(RuntimeError):
        _build(agent, install_safe_stdio=failing_stdio)
    assert stages == ["CONTEXT_BUILD_START"]


def test_live_turn_lease_fences_recovery_but_released_lease_cannot_orphan_work(
    autonomy_home,
):
    import time
    from agent.autonomy.owner_continuity import reconcile

    db, work, _ = native_owner(autonomy_home)
    try:
        assert db.try_acquire_session_turn_lease(
            "owner-chat", "active-owner-turn", ttl_seconds=300, patience_s=0
        )
        store.update_work(
            work["id"],
            refs={
                "heartbeat_at": time.time() - 1000,
                "active_turn_lease": "active-owner-turn",
            },
            hermes_home=autonomy_home,
        )
        reconcile(autonomy_home)
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(
            "pending_owner_result"
        )
        db.release_session_turn_lease("owner-chat", "active-owner-turn")
        reconcile(autonomy_home)
        assert (
            store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"][
                "state"
            ]
            == "needs_owner"
        )
    finally:
        db.close()


def test_changed_file_cannot_reuse_stale_verification(autonomy_home):
    from agent.autonomy.owner_continuity import verify, request_finish

    db, work, _ = native_owner(autonomy_home)
    try:
        path = autonomy_home / "qa.txt"
        path.write_text("first")
        verify(work["id"], file=str(path), hermes_home=autonomy_home)
        path.write_text("changed")
        with pytest.raises(ValueError, match="changed"):
            request_finish(work["id"], "Done", hermes_home=autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "working"
    finally:
        db.close()


def test_native_cli_continuation_arguments_are_accepted():
    from hermes_cli.main import _light_chat_parser

    args = _light_chat_parser().parse_args([
        "chat",
        "--cli",
        "--resume",
        "owner-chat",
        "-Q",
        "--query-file",
        "-",
    ])
    assert args.resume == "owner-chat" and args.query_file == "-" and args.cli is True
