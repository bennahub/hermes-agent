"""Parsed native CLI callbacks retain admitted scope for downstream scheduling."""
import json
from types import SimpleNamespace
import pytest
from hermes_cli.native_execution_scope import dispatch_native_command


def test_inherited_cli_callback_derives_job_scope_without_owner_mint(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from agent import execution_scope
    from cron.execution_scope import derive_job_scope
    db = SessionDB(get_hermes_home() / "state.db")
    parent = db.create_or_get_scope("native-parent", {"kind": "owner", "instruction": "schedule a report"}, {"objective": "schedule a report"})
    parent_binding = execution_scope.Binding(db, parent["scope_id"])
    locator = execution_scope.derive_child(parent_binding, "native-assignment", "hermes cron add report")
    monkeypatch.setenv("HERMES_EXECUTION_SCOPE", json.dumps(locator))
    monkeypatch.setattr(execution_scope, "judge_action", lambda *args, **kwargs: (True, "within scope"))
    seen = []
    job = {"id": "scheduled-report", "prompt": "produce the report"}
    def handler(args):
        seen.append(execution_scope.capture_binding().scope_id)
        derive_job_scope(job)
        return 0
    assert dispatch_native_command(SimpleNamespace(command="cron", func=handler), argv=["cron", "add", "report"]) == 0
    assert seen == [locator["scope_id"]]
    assert db.get_scope(job["execution_scope"]["scope_id"])["source"]["assignment_kind"] == "cron"
    assert execution_scope.capture_binding() is None
    db.close()


@pytest.mark.parametrize("marker", ["", "not-json", "{}"])
def test_invalid_inherited_cli_cannot_invoke_callback(monkeypatch, marker):
    monkeypatch.setenv("HERMES_EXECUTION_SCOPE", marker)
    args = SimpleNamespace(command="autonomy", func=lambda _: pytest.fail("blocked source invoked callback"))
    assert dispatch_native_command(args, argv=["autonomy", "status"]) == 1


def test_direct_human_admin_dispatch_keeps_native_behavior(monkeypatch):
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE", raising=False)
    seen = []
    args = SimpleNamespace(command="cron", func=lambda value: seen.append(value) or 0)
    assert dispatch_native_command(args) == 0
    assert seen == [args]


def test_human_admin_cron_revision_derives_fresh_scope_only_inside_dispatch(tmp_path, monkeypatch):
    from cron.jobs import create_job, update_job, get_job, use_cron_store
    from cron.execution_scope import assignment
    from hermes_state import SessionDB
    from agent import execution_scope_policy
    from hermes_cli.native_execution_scope import current_admin_ingress
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE", raising=False)
    compiled = []
    def compile_scope(instruction, **kwargs):
        compiled.append(instruction)
        return {"original_instruction": instruction, "policy": {"objective": "native assignment"}}
    monkeypatch.setattr(execution_scope_policy, "derive_scope", compile_scope)
    with use_cron_store(tmp_path), SessionDB(tmp_path / "state.db") as db:
        result = []
        dispatch_native_command(SimpleNamespace(command="cron", cron_command="create", prompt="inspect report", schedule="every 1h", func=lambda _: result.append(create_job("inspect report", "every 1h"))))
        initial = result.pop()
        old_scope = initial["execution_scope"]["scope_id"]
        dispatch_native_command(SimpleNamespace(command="cron", cron_command="edit", job_id=initial["id"], prompt="inspect another report", func=lambda _: result.append(update_job(initial["id"], {"prompt": "inspect another report"}))))
        updated = result.pop()
        new_scope = updated["execution_scope"]["scope_id"]
        assert new_scope != old_scope
        assert db.get_scope(old_scope)["state"] == "closed"
        assert compiled[-1] == {"command":"cron", "action":"edit", "arguments":{"job_id":initial["id"], "prompt":"inspect another report"}}
        assert db.get_scope(new_scope)["source"]["instruction"] == assignment(updated)
        assert current_admin_ingress() is None
        with pytest.raises(ValueError, match="requires current execution authority"):
            update_job(initial["id"], {"prompt": "programmatic untrusted replacement"})
        assert get_job(initial["id"])["execution_scope"]["scope_id"] == new_scope


@pytest.mark.parametrize("command,action", [("serve",None),("gateway",None),("chat",None),("autonomy","work-wait")])
def test_service_or_continuation_callback_cannot_mint_admin_cron_scope(monkeypatch, command, action):
    from cron.execution_scope import derive_job_scope
    from hermes_cli.native_execution_scope import current_admin_ingress
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE",raising=False)
    job={"id":"not-owner-cron","prompt":"unrelated future command"}
    def callback(args):
        assert current_admin_ingress() is None
        derive_job_scope(job)
    dispatch_native_command(SimpleNamespace(command=command,autonomy_command=action,func=callback))
    assert "execution_scope" not in job


def test_autonomy_delegate_admin_cannot_authorize_cron(monkeypatch):
    from cron.execution_scope import derive_job_scope
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE",raising=False)
    job={"id":"wrong-admin","prompt":"new future command"}
    dispatch_native_command(SimpleNamespace(command="autonomy",autonomy_command="delegate",goal="ask peer",func=lambda _:derive_job_scope(job)))
    assert "execution_scope" not in job
