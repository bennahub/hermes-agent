from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB
from agent import execution_scope
import tools.approval as approval_module

_COMMAND = '''python3 -c 'print(1)'; curl -I http://10.0.0.1/x'''

def _setup(monkeypatch, tmp_path: Path, *, enabled: bool):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr("tools.approval_context._get_single_query_approval_mode", lambda: "deny")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: %s\n" % ("true" if enabled else "false"))
    db = SessionDB(tmp_path / "state.db")
    record = db.create_or_get_scope("owner:test", {"message_id": 1},
        {"version": 1, "policy": {"objective": "inspect", "permitted": ["read"], "excluded": []},
         "original_instruction": "Inspect the assigned endpoint."})
    binding = execution_scope.Binding(db, record["scope_id"])
    token = execution_scope._CURRENT.set(execution_scope._Receipt(binding, "terminal", "digest", "call"))
    return db, token, record["scope_id"]

def test_active_opted_scope_uses_real_guard_with_one_mocked_guardian(monkeypatch, tmp_path):
    db, token, _ = _setup(monkeypatch, tmp_path, enabled=True)
    try:
        tirith = {"action": "block", "findings": [{"rule_id": "raw_ip_url", "title": "raw IP", "description": "private address"}]}
        with (patch("tools.tirith_security.check_command_security", return_value=tirith),
              patch("tools.approval._smart_verdict", return_value="approve") as guardian):
            result = approval_module.check_all_command_guards(_COMMAND, "local")
        assert result["approved"] is True
        guardian.assert_called_once()
        assert guardian.call_args.args[2] == "script execution via -e/-c flag"
        assert any(key.startswith("tirith:") for key in guardian.call_args.args[3])
    finally:
        execution_scope._CURRENT.reset(token)
        db.close()

def test_scope_opt_in_and_liveness_are_required_and_denials_fail_closed(monkeypatch, tmp_path):
    for enabled, close_scope, verdict in ((False, False, "approve"), (True, True, "approve"),
                                          (True, False, "deny"), (True, False, "escalate")):
        db, token, scope_id = _setup(monkeypatch, tmp_path / ("case-%s-%s" % (enabled, close_scope)), enabled=enabled)
        try:
            if close_scope:
                db.close_scope(scope_id)
            with patch("tools.approval._smart_verdict", return_value=verdict) as guardian:
                result = approval_module.check_all_command_guards(_COMMAND, "local")
            assert result["approved"] is False
            if enabled and not close_scope:
                guardian.assert_called_once()
            else:
                guardian.assert_not_called()
        finally:
            execution_scope._CURRENT.reset(token)
            db.close()
