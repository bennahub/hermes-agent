"""Durable duplicate delivery cannot retain an idle session's turn ownership."""

from contextlib import nullcontext
import sqlite3
import threading
import time

import pytest

from tools import async_delegation as delegation
from tui_gateway import server


@pytest.mark.parametrize("delivery_state", ["delivered", "claimed_elsewhere", "store_unavailable"])
def test_rejected_durable_completion_releases_session_turn(tmp_path, monkeypatch, delivery_state):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    event = {"type": "async_delegation", "delegation_id": "qa-completed", "session_key": "qa-owner"}
    if delivery_state == "store_unavailable":
        (tmp_path / "state.db").mkdir()
    else:
        with delegation._transaction() as conn:
            conn.execute(
                "INSERT INTO async_delegations (delegation_id,origin_session,state,dispatched_at,updated_at,delivery_state) VALUES (?,?,?,?,?,?)",
                ("qa-completed", "qa-owner", "completed", time.time(), time.time(),
                 "delivered" if delivery_state == "delivered" else "pending"),
            )
    if delivery_state == "claimed_elsewhere":
        assert delegation.claim_completion_delivery("qa-completed", "other-consumer")
    submitted = []
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: submitted.append(a))
    session = {"running": False, "history_lock": threading.RLock()}
    assert server._notif_claim_turn(session)
    with pytest.raises(sqlite3.OperationalError) if delivery_state == "store_unavailable" else nullcontext():
        server._notif_dispatch_event("qa-ui", session, event, "QA complete")
    assert submitted == []
    assert session["running"] is False
    # A subsequent owner request can now acquire the same session.
    assert server._notif_claim_turn(session)
    server._notif_release_turn(session)
