import json
from gateway.agent_computer.errors import ComputerCapacityError, NativeOperationError
from tools import agent_computer_tool as tool


def test_capacity_error_retains_outcome_not_private_inventory(monkeypatch):
    class Contract:
        def wake(self, *args):
            raise ComputerCapacityError("All slots occupied", details={
                "max_active_computers": 2, "active_profiles": ["private-a", "private-b"], "token": "SECRET"})
    monkeypatch.setattr(tool, "_profile_id", lambda _: "qa")
    monkeypatch.setattr(tool, "get_contract", Contract)
    data = json.loads(tool.registry.dispatch("computer_wake", {"computer_id": "qa-computer"}))
    assert data["error_code"] == "COMPUTER_CAPACITY_EXHAUSTED"
    assert data["outcome"] == "not_started"
    assert data["retryable"] is True
    assert "Report the blocker" not in data.get("recovery", "")
    assert (data["capacity"], data["active_count"]) == (2, 2)
    assert "SECRET" not in str(data) and "private-a" not in str(data)


def test_uncertain_operation_is_not_retryable_or_claimed_rolled_back():
    data = json.loads(tool._computer_error(NativeOperationError("Runtime stopped", details={"phase": "nav"})))
    assert data["outcome"] == "unknown"
    assert data["retryable"] is False
    assert data["phase"] == "nav"
    assert "verify" in data["recovery"]


def test_registry_capacity_error_preserves_other_live_computer(tmp_path, monkeypatch):
    from gateway.agent_computer.adapter import InMemoryRuntime
    from gateway.agent_computer.contract import AgentComputerContract
    from gateway.agent_computer.models import agent_principal
    from gateway.agent_computer.service import AgentComputerService
    from gateway.agent_computer.store import AgentComputerStore
    service = AgentComputerService(AgentComputerStore(tmp_path / "state.db"), InMemoryRuntime(),
                                   data_root=tmp_path, max_active_computers=1)
    other = service.ensure_computer("other")
    qa = service.ensure_computer("qa")
    _, original_lease = service.wake(other.id, agent_principal("other"))
    original = service.public_status(service.get_computer(other.id))
    monkeypatch.setattr(tool, "_profile_id", lambda _: "qa")
    monkeypatch.setattr(tool, "get_contract", lambda: AgentComputerContract(service))
    for _ in range(2):
        result = json.loads(tool.registry.dispatch("computer_wake", {"computer_id": qa.id}))
        assert result["error_code"] == "COMPUTER_CAPACITY_EXHAUSTED"
        assert result["outcome"] == "not_started"
        assert result["active_count"] == result["capacity"] == 1
    assert service.public_status(service.get_computer(other.id)) == original
    assert service.store.active_lease_for_computer(other.id).lease_id == original_lease.lease_id
    assert service.store.active_lease_for_computer(qa.id) is None
