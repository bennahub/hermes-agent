"""Native corrections fence admission before they become actor-visible."""

from types import SimpleNamespace

import pytest

from agent import execution_scope, owner_amendment


def test_amendment_fences_before_accept_and_compiles_only_accepted(monkeypatch):
    events = []
    token = object()
    monkeypatch.setattr(execution_scope, "begin_amendment", lambda *args, **kwargs: events.append("fence") or token)
    monkeypatch.setattr(execution_scope, "cancel_amendment", lambda value: events.append(("cancel", value)))
    monkeypatch.setattr(owner_amendment, "start_owner_amendment", lambda *args: events.append(("compile", args[1], args[3])))

    def accept(text):
        events.append(("accept", text))
        return True

    assert owner_amendment.admit_owner_amendment(SimpleNamespace(), "inspect only", accept)
    assert events == ["fence", ("accept", "inspect only"), ("compile", "inspect only", token)]
    events.clear()
    assert not owner_amendment.admit_owner_amendment(SimpleNamespace(), "inspect only", lambda text: False)
    assert events == ["fence", ("cancel", token)]


@pytest.mark.parametrize("marker", ["HERMES_EXECUTION_SCOPE", "HERMES_OWNER_CONTINUATION_ID", "HERMES_KANBAN_TASK"])
def test_machine_correction_cannot_expand_owner_scope(monkeypatch, marker):
    monkeypatch.setenv(marker, "native-machine-assignment")
    monkeypatch.setattr(execution_scope, "begin_amendment", lambda *args, **kwargs: pytest.fail("must not mint owner amendment"))
    assert owner_amendment.admit_owner_amendment(SimpleNamespace(), "inspect only", lambda text: True)
