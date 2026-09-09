"""Canonical Hermes truth offered to every owner-facing adapter."""
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent.message_projection import owner_safe_metadata, owner_semantics
from gateway.push_registry import is_notifiable
from hermes_state import SessionDB
from hermes_cli.web_routers.sessions import _project_for_display
from tui_gateway.server import _canonical_owner_read_state, _history_to_messages


CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "cross_client_parity_v1.json").read_text(encoding="utf-8")
)


def assert_no_private_location(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert "path" not in key.lower() and "backend" not in key.lower()
            assert_no_private_location(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_private_location(item)
    elif isinstance(value, str):
        assert not value.startswith(("/", "~/", "file://", "@file:", "@image:"))


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["name"])
def test_canonical_cross_client_semantics(case):
    row, expected = case["row"], case["expected"]
    semantics = owner_semantics(row)
    for key in ("semantic_kind", "owner_authored", "owner_notification_eligible", "owner_unread_eligible"):
        assert semantics[key] == expected[key]

    rows = [json.loads(json.dumps(row)) for _ in range(case.get("copies", 1))]
    history = _history_to_messages(rows)
    assert bool(history) == expected["history_visible"]
    projected_rows = _project_for_display(rows)
    projected = projected_rows[0]
    assert (projected.get("display_kind") != "hidden") == expected["history_visible"]
    if row["role"] == "assistant":
        assert is_notifiable(
            kind="decision_required" if case["name"] == "needs_you" else "task_complete",
            role=row["role"], content=row["content"],
            display_kind=row.get("display_kind"), display_metadata=row.get("display_metadata"),
        ) == expected["owner_notification_eligible"]

    if history:
        for key in ("semantic_kind", "owner_authored", "owner_notification_eligible", "owner_unread_eligible"):
            assert history[0][key] == expected[key]
        assert_no_private_location(history[0])
    assert_no_private_location(projected)
    if "attachment_count" in expected:
        assert len(semantics["attachments"]) == expected["attachment_count"]
    if expected.get("path_free"):
        assert all("path" not in key.lower() for item in semantics["attachments"] for key in item)
        if "backend" in row["display_metadata"]:
            # Projection sanitizing is non-mutating; private storage/export data remains.
            assert row["display_metadata"]["backend"]["nested"]["serverPath"].startswith("/")
    if "durable_row_id" in expected:
        identity = semantics["message_identity"]
        assert identity["durable_row_id"] == expected["durable_row_id"]
        assert len(identity["client_message_ids"]) == expected["client_message_id_count"]
        assert identity["reply_to"] == {"message_id": expected["reply_to_message_id"]}
        assert len(history) == len(projected_rows) == 1
    if "collaboration_key" in expected:
        assert len(history) == len(projected_rows) == expected["canonical_card_count"]
        assert history[0]["text"] == projected_rows[0]["content"] == ""
        assert history[0]["collaboration_summary"]["key"] == expected["collaboration_key"]
        assert "Internal exchange body" not in json.dumps(history, ensure_ascii=False)
        assert "Internal exchange body" not in json.dumps(projected_rows, ensure_ascii=False)
        assert "Internal A2A memo" not in json.dumps(history, ensure_ascii=False)
        assert "Internal A2A memo" not in json.dumps(projected_rows, ensure_ascii=False)
    if "transcript_text" in expected:
        assert semantics["transcript"]["text"] == expected["transcript_text"]
        assert semantics["transcript"]["audio_artifact_id"] == expected["audio_artifact_id"]


def test_unbound_transcript_is_not_projected():
    case = next(case for case in CORPUS["cases"] if case["name"] == "voice_with_canonical_transcript")
    row = json.loads(json.dumps(case["row"]))
    row["display_metadata"]["transcript"]["audio_artifact_id"] = "different-artifact"
    assert "transcript" not in owner_semantics(row)


def test_unresolved_legacy_authorship_stays_optional():
    semantics = owner_semantics({"id": 9, "role": "user", "content": "legacy row"})
    assert semantics["semantic_kind"] == "legacy_message"
    assert semantics["owner_authored"] is None


def test_runtime_reply_producer_projects_product_message_id(tmp_path, monkeypatch):
    from tui_gateway import server
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", source="desktop")
    target = db.append_message("chat", "assistant", "Answer from /private/backend")
    monkeypatch.setattr(server, "_session_db", lambda session: nullcontext(db))
    try:
        reference, quote = server._resolve_reply_reference(
            {"session_key": "chat"}, {"message_id": target, "excerpt": "forged"})
        assert reference["message_id"] == target and reference["excerpt"] != "forged"
        metadata = owner_safe_metadata({"reply_to": reference})
        assert metadata == {"reply_to": {"message_id": target}}
        semantics = owner_semantics({
            "id": target + 1, "session_id": "chat", "role": "user", "content": "Thanks",
            "display_metadata": {"reply_to": reference, "projection": {
                "version": 1, "origin": "owner", "audience": "owner", "purpose": "message"}},
        })
        assert semantics["message_identity"]["reply_to"] == {"message_id": target}
        assert str(target) in quote
    finally:
        db.close()


@pytest.mark.parametrize("content,eligible", [("Direct result", True), ("", False), ("   ", False), ("[SILENT]", False)])
def test_notification_and_unread_use_the_same_real_content(content, eligible):
    metadata = {"projection": {
        "version": 1, "origin": "agent", "audience": "owner", "purpose": "result"}}
    row = {"role": "assistant", "content": content, "display_metadata": metadata}
    semantics = owner_semantics(row)
    assert semantics["owner_notification_eligible"] == eligible
    assert semantics["owner_unread_eligible"] == eligible
    assert is_notifiable(
        kind="task_complete", role="assistant", content=content, display_metadata=metadata) == eligible


def test_fixture_covers_every_addendum_case_once():
    names = {case["name"] for case in CORPUS["cases"]}
    assert names == {
        "real_owner_message", "direct_agent_to_owner", "a2a_child_acknowledgement",
        "compact_collaboration_run", "runtime_user_shaped_row", "continuation_recovery_row",
        "duplicate_optimistic_and_durable_identity", "image_attachment", "pdf_attachment",
        "multi_attachment_message", "voice_with_canonical_transcript", "needs_you",
    }


def test_corpus_events_share_one_cross_device_read_watermark(tmp_path):
    by_name = {case["name"]: case["row"] for case in CORPUS["cases"]}
    db = SessionDB(tmp_path / "state.db")
    db.create_session("chat", source="desktop")
    try:
        def append(name, timestamp):
            row = by_name[name]
            db.append_message(
                "chat", row["role"], row["content"], timestamp=timestamp,
                display_kind=row.get("display_kind"), display_metadata=row.get("display_metadata"),
            )

        append("direct_agent_to_owner", 1000)
        db.set_session_read("chat", through=1000)
        append("a2a_child_acknowledgement", 1100)
        append("runtime_user_shaped_row", 1200)
        state = _canonical_owner_read_state(db, "chat")
        assert state == {"version": 1, "last_read_at": 1000, "latest_reply_at": 1000}

        append("needs_you", 1300)
        state = _canonical_owner_read_state(db, "chat")
        assert state["last_read_at"] == 1000 and state["latest_reply_at"] == 1300
        db.set_session_read("chat", through=1300)
        assert _canonical_owner_read_state(db, "chat")["last_read_at"] == 1300
    finally:
        db.close()
