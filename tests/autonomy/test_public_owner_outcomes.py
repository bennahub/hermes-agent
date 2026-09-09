"""Public CLI outcomes and native process evidence, with real isolated stores."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent.autonomy import owner_continuity as oc, store


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_state import SessionDB
    from tools import process_registry as module

    monkeypatch.setattr(module, "CHECKPOINT_PATH", home / "processes.json")
    registry = module.ProcessRegistry()
    monkeypatch.setattr(module, "process_registry", registry)
    db = SessionDB(home / "state.db")
    db.create_session("owner", source="desktop")
    mid = db.append_message(
        "owner", "user", "Run the harmless QA operation then report its final result"
    )
    work = oc.register_owner_request(db, "owner", mid, force=True, hermes_home=home)
    agent = SimpleNamespace(
        _session_db=db,
        session_id="owner",
        _owner_continuity_source=("owner", mid),
        _owner_continuity_work_id=work["id"],
    )
    yield home, db, work, agent, registry
    db.close()


def cli(*args):
    from hermes_cli.subcommands.autonomy import build_autonomy_parser

    parser = argparse.ArgumentParser()
    build_autonomy_parser(parser.add_subparsers())
    from hermes_cli.autonomy_cmd import cmd_autonomy

    with pytest.raises(SystemExit) as result:
        cmd_autonomy(parser.parse_args(["autonomy", *args]))
    return result.value.code


@pytest.mark.parametrize("state", ["failed", "cancelled", "needs_owner"])
def test_public_cli_terminal_outcome_delivers_once(native, state, capsys):
    home, db, work, _, _ = native
    assert (
        cli(
            "work-update",
            work["id"],
            "--state",
            state,
            "--waiting-reason",
            "Synthetic explicit outcome",
        )
        == 0
    )
    pending = store.get_work(work["id"], home)
    assert pending["refs"]["pending_owner_result"]["state"] == state
    assert oc.deliver_pending(pending, home)
    oc.reconcile(home)
    final = store.get_work(work["id"], home)
    assert final["state"] == state and final["refs"]["owner_delivery"]["message_id"]
    assert len([r for r in db.get_messages("owner") if r["role"] == "assistant"]) == 1


def bind_process(native, exit_code=None, early=False):
    home, db, work, agent, registry = native
    from tools.process_registry import ProcessSession

    process = ProcessSession(
        id="proc_native_qa",
        command="synthetic",
        parent_session_id="owner",
        started_at=10.5,
        host_start_time=123,
        output_buffer="synthetic native reply",
    )
    registry._running[process.id] = process
    if early:
        registry._finish_exited(process, exit_code)
    oc.observe_dispatch(
        agent,
        "message_agent",
        {},
        json.dumps({"status": "sent", "process_id": process.id}),
    )
    if exit_code is not None and not early:
        registry._finish_exited(process, exit_code)
    return process


@pytest.mark.parametrize("early", [False, True])
def test_exact_native_completion_public_verification_and_fresh_rewait(
    native, early, capsys
):
    home, db, work, _, registry = native
    p = bind_process(native, exit_code=0, early=early)
    assert cli("work-verify", work["id"], "--process-id", p.id) == 0
    old = store.get_work(work["id"], home)
    proof = old["refs"]["verification"]
    assert (
        proof["kind"] == "native_process_completion"
        and proof["identity"]["owner_request"] == work["refs"]["owner_request"]
    )
    assert proof["output_tail_sha256"] and proof["exit_code"] == 0
    oc.wait(
        work["id"],
        external="process:" + p.id,
        deadline="2099-01-01T00:00:00Z",
        hermes_home=home,
    )
    fresh = store.get_work(work["id"], home)
    assert fresh["refs"]["verification"] is None
    assert cli("work-verify", work["id"], "--process-id", p.id) == 0
    assert (
        store.get_work(work["id"], home)["refs"]["verification"]["resume_generation"]
        > proof["resume_generation"]
    )
    assert cli("work-complete", work["id"], "--result", "Verified native reply") == 0
    assert oc.deliver_pending(store.get_work(work["id"], home), home)
    assert store.get_work(work["id"], home)["state"] == "completed"


@pytest.mark.parametrize(
    "case",
    [
        "pending",
        "failed",
        "killed",
        "wrong_id",
        "wrong_start",
        "foreign_source",
        "different_wait",
        "stopped",
        "ack",
        "user_prose",
    ],
)
def test_nonproof_cannot_become_external_success(native, case):
    home, db, work, _, registry = native
    p = bind_process(
        native, exit_code=None if case == "pending" else 1 if case == "failed" else 0
    )
    if case == "killed":
        current = store.get_work(work["id"], home)
        receipt = dict(current["refs"]["native_process_completion"], reason="killed")
        store.update_work(
            work["id"], refs={"native_process_completion": receipt}, hermes_home=home
        )
    elif case in {"wrong_start", "foreign_source"}:
        current = store.get_work(work["id"], home)
        identity = dict(current["refs"]["native_process"])
        identity["started_at" if case == "wrong_start" else "owner_request"] = (
            99 if case == "wrong_start" else {"session_id": "other", "message_id": "1"}
        )
        store.update_work(
            work["id"], refs={"native_process": identity}, hermes_home=home
        )
    elif case == "different_wait":
        oc.wait(
            work["id"],
            external="process:proc_other",
            deadline="2099-01-01T00:00:00Z",
            hermes_home=home,
        )
    elif case == "stopped":
        oc.cancel_owner_session("owner", hermes_home=home)
    if case in {"ack", "user_prose"}:
        row = db.append_message(
            "owner",
            "tool" if case == "ack" else "user",
            json.dumps({"status": "sent", "process_id": p.id}),
            tool_name="message_agent" if case == "ack" else None,
            display_metadata={"owner_work_id": work["id"]},
        )
        with pytest.raises(ValueError):
            oc.verify(work["id"], tool_message_id=str(row), hermes_home=home)
    else:
        with pytest.raises(ValueError):
            oc.verify(
                work["id"],
                process_id="proc_other" if case == "wrong_id" else p.id,
                hermes_home=home,
            )
    assert not store.get_work(work["id"], home)["refs"].get("verification")


def test_completion_after_stop_or_replaced_operation_cannot_write_receipt(native):
    home, db, work, _, registry = native
    p = bind_process(native)
    oc.wait(
        work["id"],
        external="process:proc_other",
        deadline="2099-01-01T00:00:00Z",
        hermes_home=home,
    )
    registry._finish_exited(p, 0)
    current = store.get_work(work["id"], home)
    assert not current["refs"].get("native_process_completion") and not current[
        "refs"
    ].get("resume_event")
    oc.cancel_owner_session("owner", hermes_home=home)
    assert not oc.signal_process_completion(p)
    assert store.get_work(work["id"], home)["refs"]["owner_stop"]


@pytest.mark.parametrize("winner", ["rewait", "stop", "failed", "different"])
def test_completion_receipt_cas_respects_native_winner(native, monkeypatch, winner):
    home, db, work, _, registry = native
    p = bind_process(native)
    original = store.update_work
    ran = False

    def racing(*args, **kwargs):
        nonlocal ran
        if not ran and kwargs.get("refs", {}).get("native_process_completion"):
            ran = True
            if winner in {"rewait", "different"}:
                oc.wait(
                    work["id"],
                    external="process:"
                    + (p.id if winner == "rewait" else "proc_other"),
                    deadline="2099-01-01T00:00:00Z",
                    hermes_home=home,
                )
            elif winner == "stop":
                oc.cancel_owner_session("owner", hermes_home=home)
            else:
                oc.request_finish(
                    work["id"],
                    "Actual failure already recorded",
                    terminal="failed",
                    hermes_home=home,
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "update_work", racing)
    registry._finish_exited(p, 0)
    current = store.get_work(work["id"], home)
    assert ran
    if winner == "rewait":
        assert current["refs"]["native_process_completion"]["identity"]["id"] == p.id
        assert oc.verify(work["id"], process_id=p.id, hermes_home=home)["refs"][
            "verification"
        ]
    else:
        assert not current["refs"].get("native_process_completion")
        if winner == "stop":
            assert current["refs"]["owner_stop"]
        elif winner == "failed":
            assert current["refs"]["pending_owner_result"]["state"] == "failed"
        else:
            assert current["refs"]["resume"]["id"] == "process:proc_other"


def test_process_proof_cannot_race_a_new_wait_during_verification(native, monkeypatch):
    home, db, work, _, registry = native
    p = bind_process(native, exit_code=0)
    original = store.update_work
    ran = False

    def racing(*args, **kwargs):
        nonlocal ran
        if not ran and kwargs.get("refs", {}).get("verification"):
            ran = True
            oc.wait(
                work["id"],
                external="process:proc_other",
                deadline="2099-01-01T00:00:00Z",
                hermes_home=home,
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "update_work", racing)
    assert oc.verify(work["id"], process_id=p.id, hermes_home=home) is None
    current = store.get_work(work["id"], home)
    assert (
        current["refs"]["resume"]["id"] == "process:proc_other"
        and not current["refs"]["verification"]
    )


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize(
    "notify,async_supported,platform",
    [(True, True, ""), (False, True, ""), (True, False, ""), (True, True, "telegram")],
)
def test_real_tui_background_process_binding_and_completion(
    native, monkeypatch, exit_code, notify, async_supported, platform
):
    import shlex, sys, time
    from gateway.session_context import get_session_env, set_session_vars
    from tui_gateway import server
    import tools.terminal_tool  # Register the real public handler.
    from tools.terminal_tool_lifecycle import cleanup_vm
    from tools.registry import registry as tools_registry

    home, db, work, agent, processes = native
    # Only this temporary fixture authorizes its harmless synthetic subprocess.
    (home / "config.yaml").write_text("approvals:\n  mode: 'off'\n")
    task_key = "owned-tui-terminal-test"
    monkeypatch.setattr(
        server,
        "_sessions",
        {
            "owned-ui": {
                "session_key": task_key,
                "agent": agent,
                "source": "desktop",
                "cwd": str(home),
            }
        },
    )
    monkeypatch.setenv("TERMINAL_ENV", "local")
    tokens = server._set_session_context(task_key, ui_session_id="owned-ui")
    assert get_session_env("HERMES_SESSION_ID") == "owner"
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    if not async_supported or platform:
        tokens = set_session_vars(
            source="desktop",
            session_id="owner",
            session_key=task_key,
            cwd=str(home),
            async_delivery=async_supported,
            platform=platform,
            chat_id="owned-chat",
            thread_id="owned-topic",
        )
    release = home / "release-owned-process"
    code = (
        "import pathlib,time; p=pathlib.Path("
        + repr(str(release))
        + "); end=time.monotonic()+12; "
        "\nwhile not p.exists() and time.monotonic()<end: time.sleep(.01)"
        '\nassert p.exists(), "bounded fixture was not released"'
        '\nprint("NATIVE_PROCESS_PROOF_OK", flush=True)'
        + "\nraise SystemExit("
        + str(exit_code)
        + ")"
    )
    process = None
    try:
        args = {
            "command": shlex.join([sys.executable, "-c", code]),
            "background": True,
            "notify": notify,
            "workdir": str(home),
        }
        ack = tools_registry.get_entry("terminal").handler(args, task_id=task_key)
        payload = json.loads(ack)
        assert payload.get("session_id"), payload
        process = processes.get(payload["session_id"])
        assert process.parent_session_id == "owner"
        assert process.watcher_platform == platform
        if platform:
            assert len(processes.pending_watchers) == 1
            watcher = processes.pending_watchers[0]
            assert (
                watcher["parent_session_id"],
                watcher["chat_id"],
                watcher["thread_id"],
            ) == ("owner", "owned-chat", "owned-topic")
        else:
            assert not processes.pending_watchers
        assert process.notify_on_complete == (notify and async_supported)
        oc.observe_dispatch(agent, "terminal", args, ack)
        bound = store.get_work(work["id"], home)["refs"]["native_process"]
        assert bound["parent_session_id"] == "owner" and bound["id"] == process.id
        observed = processes.wait(process.id, timeout=1)
        assert observed["status"] == "timeout" and observed["process_running"]
        with pytest.raises(ValueError):
            oc.verify(work["id"], process_id=process.id, hermes_home=home)
        release.write_text("release harmless fixture")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if store.get_work(work["id"], home)["refs"].get(
                "native_process_completion"
            ):
                break
            time.sleep(0.01)
        completion = store.get_work(work["id"], home)["refs"][
            "native_process_completion"
        ]
        assert completion["exit_code"] == exit_code
        assert completion["identity"] == bound
        assert (
            completion["output_tail_sha256"]
            == __import__("hashlib").sha256(process.output_buffer.encode()).hexdigest()
        )
        if exit_code == 0:
            assert cli("work-verify", work["id"], "--process-id", process.id) == 0
            assert (
                cli("work-complete", work["id"], "--result", "Verified native success")
                == 0
            )
            expected = "completed"
        else:
            with pytest.raises(ValueError):
                oc.verify(work["id"], process_id=process.id, hermes_home=home)
            assert (
                cli(
                    "work-update",
                    work["id"],
                    "--state",
                    "failed",
                    "--waiting-reason",
                    "Native process exited 7",
                )
                == 0
            )
            expected = "failed"
        assert oc.deliver_pending(store.get_work(work["id"], home), home)
        oc.reconcile(home)
        assert store.get_work(work["id"], home)["state"] == expected
        assert (
            len([r for r in db.get_messages("owner") if r["role"] == "assistant"]) == 1
        )
    finally:
        if process is not None:
            if not process.exited:
                processes.kill_process(process.id)
            if process.process:
                process.process.wait(timeout=5)
        cleanup_vm(task_key)
        server._clear_session_context(tokens)


def test_finished_process_old_ack_proof_cannot_finish_external_work(native):
    home, db, work, _, _ = native
    p = bind_process(native, exit_code=0)
    store.update_work(
        work["id"],
        refs={"verification": {"kind": "tool_observation", "message_id": "123"}},
        hermes_home=home,
    )
    with pytest.raises(ValueError):
        oc.request_finish(work["id"], "Pretend old ACK was enough", hermes_home=home)
    assert not store.get_work(work["id"], home)["refs"].get("pending_owner_result")


def test_new_wait_wins_race_after_native_proof_before_final_result(native, monkeypatch):
    home, db, work, _, _ = native
    p = bind_process(native, exit_code=0)
    oc.verify(work["id"], process_id=p.id, hermes_home=home)
    original = store.update_work
    ran = False

    def racing(*args, **kwargs):
        nonlocal ran
        if not ran and kwargs.get("refs", {}).get("pending_owner_result"):
            ran = True
            oc.wait(
                work["id"],
                external="process:proc_other",
                deadline="2099-01-01T00:00:00Z",
                hermes_home=home,
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "update_work", racing)
    assert oc.request_finish(work["id"], "Stale success", hermes_home=home) is None
    current = store.get_work(work["id"], home)
    assert current["refs"]["resume"]["id"] == "process:proc_other" and not current[
        "refs"
    ].get("pending_owner_result")


def test_same_process_rewait_cannot_erase_concurrent_native_completion(
    native, monkeypatch
):
    home, db, work, agent, registry = native
    p = bind_process(native)
    orig = store.update_work
    ran = [False]

    def race(*args, **kwargs):
        if (
            not ran[0]
            and kwargs.get("state") == "waiting"
            and "native_process_completion" in kwargs.get("refs", {})
        ):
            ran[0] = True
            registry._finish_exited(p, 0)
            assert store.get_work(work["id"], home)["refs"]["native_process_completion"]
        return orig(*args, **kwargs)

    monkeypatch.setattr(store, "update_work", race)
    oc.wait(
        work["id"],
        external="process:" + p.id,
        deadline="2099-01-01T00:00:00Z",
        hermes_home=home,
    )
    assert ran[0]
    assert store.get_work(work["id"], home)["refs"]["native_process_completion"]
    assert oc.verify(work["id"], process_id=p.id, hermes_home=home)
    assert oc.resume_due(store.get_work(work["id"], home))


def test_process_started_on_intermediate_compression_tip_remains_provable(native):
    home, db, work, agent, registry = native
    db.end_session("owner", "compression")
    db.create_session("tip1", source="desktop", parent_session_id="owner")
    agent.session_id = "tip1"
    from tools.process_registry import ProcessSession

    p = ProcessSession(
        id="proc_compressed",
        command="synthetic",
        parent_session_id="tip1",
        started_at=10,
        host_start_time=123,
        output_buffer="safe result",
    )
    registry._running[p.id] = p
    oc.observe_dispatch(
        agent, "message_agent", {}, json.dumps({"status": "sent", "process_id": p.id})
    )
    assert store.get_work(work["id"], home)["refs"]["native_process"]
    db.end_session("tip1", "compression")
    db.create_session("tip2", source="desktop", parent_session_id="tip1")
    registry._finish_exited(p, 0)
    assert store.get_work(work["id"], home)["refs"]["native_process_completion"]
    assert oc.verify(work["id"], process_id=p.id, hermes_home=home)
    assert oc.resume_due(store.get_work(work["id"], home))


def test_public_bare_returned_process_id_preserves_same_native_operation(native):
    home, db, work, agent, registry = native
    p = bind_process(native, exit_code=0)
    oc.wait(
        work["id"], external=p.id, deadline="2099-01-01T00:00:00Z", hermes_home=home
    )
    assert oc.verify(work["id"], process_id=p.id, hermes_home=home)


def test_existing_process_cannot_be_rebound_to_new_owner_work(native):
    home, db, work, agent, registry = native
    p = bind_process(native)
    mid = db.append_message(
        "owner", "user", "Do a separate harmless task and return a verified result"
    )
    other = oc.register_owner_request(db, "owner", mid, force=True, hermes_home=home)
    from types import SimpleNamespace

    other_agent = SimpleNamespace(
        _session_db=db,
        session_id="owner",
        _owner_continuity_source=("owner", mid),
        _owner_continuity_work_id=other["id"],
    )
    try:
        oc.observe_dispatch(
            other_agent,
            "message_agent",
            {},
            json.dumps({"status": "sent", "process_id": p.id}),
        )
    except ValueError:
        pass
    assert p.owner_continuity_work_id == work["id"]
    assert not store.get_work(other["id"], home)["refs"].get("native_process")


def test_preowned_process_cannot_cross_profile_home_even_same_work_id(
    native, monkeypatch
):
    home, db, work, agent, registry = native
    p = bind_process(native)
    original_home = p.owner_continuity_home
    p.owner_continuity_home = str(home.parent / "foreign-profile")
    try:
        with pytest.raises(ValueError):
            oc.observe_dispatch(
                agent,
                "message_agent",
                {},
                json.dumps({"status": "sent", "process_id": p.id}),
            )
        assert (
            p.owner_continuity_home != original_home
            and p.owner_continuity_work_id == work["id"]
        )
    finally:
        p.owner_continuity_home = original_home


def test_duplicate_same_process_ack_preserves_completion_proof(native):
    home, db, work, agent, registry = native
    p = bind_process(native, exit_code=0)
    original = store.get_work(work["id"], home)["refs"]["native_process_completion"]
    oc.observe_dispatch(
        agent, "message_agent", {}, json.dumps({"status": "sent", "process_id": p.id})
    )
    assert (
        store.get_work(work["id"], home)["refs"]["native_process_completion"]
        == original
    )
    assert oc.verify(work["id"], process_id=p.id, hermes_home=home)


@pytest.mark.parametrize("parent", ["", "foreign-conversation"])
def test_real_terminal_does_not_adopt_ambient_or_foreign_parent(
    native, monkeypatch, parent
):
    import shlex, sys
    import tools.terminal_tool  # Register the real public handler.
    from tools.terminal_tool_lifecycle import cleanup_vm
    from tools.registry import registry as tools_registry
    from gateway.session_context import set_session_vars, clear_session_vars

    home, db, work, agent, processes = native
    # A task-local empty identity must not fall back to stale process environment.
    monkeypatch.setenv("HERMES_SESSION_ID", "owner")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    task_key = "owned-negative-terminal-test"
    tokens = set_session_vars(
        source="desktop", session_id=parent, session_key=task_key, cwd=str(home)
    )
    process = None
    try:
        args = {
            "command": shlex.join([sys.executable, "-c", "print('harmless')"]),
            "background": True,
            "notify": True,
            "workdir": str(home),
        }
        ack = tools_registry.get_entry("terminal").handler(args, task_id=task_key)
        process = processes.get(json.loads(ack)["session_id"])
        assert process.parent_session_id == parent
        oc.observe_dispatch(agent, "terminal", args, ack)
        current = store.get_work(work["id"], home)
        assert current["state"] == "waiting"
        assert not current["refs"].get("native_process")
        assert not current["refs"].get("native_process_completion")
        assert not process.owner_continuity_work_id
        with pytest.raises(ValueError):
            oc.verify(work["id"], process_id=process.id, hermes_home=home)
    finally:
        if process is not None:
            if not process.exited:
                processes.kill_process(process.id)
            if process.process:
                process.process.wait(timeout=5)
        cleanup_vm(task_key)
        clear_session_vars(tokens)
