"""Task-scoped authority for protected agent-instruction writes.

The gate in ``tools/file_tools.py`` asks the owner before an agent may write a
file that steers agent behaviour (``SOUL.md``, ``AGENTS.md``, ``CLAUDE.md``,
``.cursorrules``). It used to ask once per *write*, so an owner who had said
"edit Mishari's charter and remove Nasser" was asked again for every file the
task touched and again for every retry.

These tests pin the contract that replaced that: the owner's own answer mints
authority, and the authority is bound to the task, the session and the exact
files it was given for — nothing wider, nothing longer.

Written with ``unittest`` so it runs under this repo's pytest and under a bare
interpreter, since the protected gate is the one place where "it was too
awkward to test" would be the wrong answer.
"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools.approval_context as approval_context
import tools.approval as approval  # noqa: E402
import tools.file_tools_write_guards as file_tools  # noqa: E402


class _Recorder:
    """Stands in for the owner. Records every prompt and answers on script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def decide(self, session_key, notify_cb, approval_data, surface="gateway"):
        self.prompts.append(approval_data)
        choice = self.answers.pop(0) if self.answers else "deny"
        if choice == "timeout":
            return {"resolved": False, "choice": None}
        return {"resolved": True, "choice": choice}


@contextmanager
def _owner(answers, *, turn="turn-a", session="session-a", scope_allowed=True):
    """Runs the gate as if a human were on the other end of it."""
    recorder = _Recorder(answers)
    originals = {
        "decide": file_tools._await_gateway_decision,
        "session": file_tools.get_current_session_key,
        "turn": file_tools._current_task_authority_turn,
        "scope": file_tools._task_authority_scope_allowed,
        "cbs": dict(approval._gateway_notify_cbs),
    }
    file_tools._await_gateway_decision = recorder.decide
    file_tools.get_current_session_key = lambda *a, **k: session
    file_tools._current_task_authority_turn = lambda: turn
    file_tools._task_authority_scope_allowed = lambda: scope_allowed
    approval._gateway_notify_cbs[session] = lambda *a, **k: None
    try:
        yield recorder
    finally:
        file_tools._await_gateway_decision = originals["decide"]
        file_tools.get_current_session_key = originals["session"]
        file_tools._current_task_authority_turn = originals["turn"]
        file_tools._task_authority_scope_allowed = originals["scope"]
        approval._gateway_notify_cbs.clear()
        approval._gateway_notify_cbs.update(originals["cbs"])


class TaskScopedAuthorityTests(unittest.TestCase):

    def setUp(self):
        file_tools.clear_task_instruction_authority()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Two agents' charters, outside the Hermes home so the gate applies.
        self.mishari = root / "mishari" / "SOUL.md"
        self.mishari_agents = root / "mishari" / "AGENTS.md"
        self.nasser = root / "nasser" / "SOUL.md"
        for path in (self.mishari, self.mishari_agents, self.nasser):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("charter\n", encoding="utf-8")

    def tearDown(self):
        file_tools.clear_task_instruction_authority()
        self._tmp.cleanup()

    def _write(self, path):
        """The gate's verdict for a write: ``None`` means it may proceed."""
        return file_tools._check_protected_instruction_write([str(path)])

    # -- the owner's task ---------------------------------------------------

    def test_owner_grant_lets_the_task_proceed(self):
        """The write the owner asked for executes on their one answer."""
        with _owner(["session"]) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertEqual(len(owner.prompts), 1)
            # The wider scope was offered, and `always` never is.
            self.assertTrue(owner.prompts[0]["allow_session"])
            self.assertFalse(owner.prompts[0]["allow_permanent"])

    def test_two_writes_in_one_task_ask_once(self):
        """The second necessary write does not ask again."""
        with _owner(["session"]) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNone(self._write(self.mishari))
            self.assertEqual(len(owner.prompts), 1, "one task, one question")

    def test_a_second_file_in_the_same_task_is_still_asked(self):
        """Authority covers the files it was given for, not the task's reach."""
        with _owner(["session", "deny"]) as owner:
            self.assertIsNone(self._write(self.mishari))
            blocked = self._write(self.mishari_agents)
            self.assertIsNotNone(blocked)
            self.assertIn("BLOCKED", blocked)
            self.assertEqual(len(owner.prompts), 2)

    def test_an_unrelated_charter_is_gated(self):
        """A grant for Mishari's file cannot be spent on Nasser's."""
        with _owner(["session", "deny"]):
            self.assertIsNone(self._write(self.mishari))
            blocked = self._write(self.nasser)
            self.assertIsNotNone(blocked)
            self.assertIn("BLOCKED", blocked)

    # -- the boundaries -----------------------------------------------------

    def test_another_agent_cannot_reuse_the_authority(self):
        with _owner(["session"], session="abu-saud"):
            self.assertIsNone(self._write(self.mishari))
        with _owner(["deny"], session="majed") as other:
            self.assertIsNotNone(self._write(self.mishari))
            self.assertEqual(len(other.prompts), 1)

    def test_another_task_cannot_reuse_the_authority(self):
        with _owner(["session"], turn="turn-a"):
            self.assertIsNone(self._write(self.mishari))
        with _owner(["deny"], turn="turn-b") as later:
            self.assertIsNotNone(self._write(self.mishari))
            self.assertEqual(len(later.prompts), 1)

    def test_authority_expires_when_the_task_ends(self):
        with _owner(["session"], turn="turn-a"):
            self.assertIsNone(self._write(self.mishari))
            dropped = file_tools.clear_task_instruction_authority(
                session_key="session-a", turn_key="turn-a")
            self.assertEqual(dropped, 1)
        with _owner(["deny"], turn="turn-a") as after:
            self.assertIsNotNone(self._write(self.mishari))
            self.assertEqual(len(after.prompts), 1)

    def test_an_endpoint_switch_invalidates_it(self):
        """A grant belongs to one session on one server, and travels nowhere."""
        with _owner(["session"], session="server-a:abu-saud"):
            self.assertIsNone(self._write(self.mishari))
        with _owner(["deny"], session="server-b:abu-saud") as elsewhere:
            self.assertIsNotNone(self._write(self.mishari))
            self.assertEqual(len(elsewhere.prompts), 1)

    def test_a_turnless_context_can_never_hold_authority(self):
        """Without a task to belong to, a grant would be session-wide."""
        with _owner(["session", "session"], turn=None) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNone(self._write(self.mishari))
            self.assertEqual(len(owner.prompts), 2, "asked every time")
            self.assertFalse(owner.prompts[0]["allow_session"],
                             "the wider scope is not even offered")

    def test_an_automated_context_is_never_offered_the_scope(self):
        """Cron, webhooks and delegated children cannot mint a grant."""
        with _owner(["session", "session"], scope_allowed=False) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNone(self._write(self.mishari))
            self.assertEqual(len(owner.prompts), 2)
            self.assertFalse(owner.prompts[0]["allow_session"])

    # -- what did not change ------------------------------------------------

    def test_one_shot_approval_still_covers_exactly_one_write(self):
        with _owner(["once", "deny"]) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNotNone(self._write(self.mishari))
            self.assertEqual(len(owner.prompts), 2)

    def test_a_denial_still_blocks_and_says_so(self):
        with _owner(["deny"]):
            blocked = self._write(self.mishari)
            self.assertIn("BLOCKED", blocked)
            self.assertIn("denied by the user", blocked)

    def test_silence_is_still_not_consent(self):
        with _owner(["timeout"]):
            blocked = self._write(self.mishari)
            self.assertIn("BLOCKED", blocked)
            self.assertIn("Silence is not consent", blocked)

    def test_nothing_is_persisted_outside_the_process(self):
        """The grant lives in memory and touches no allowlist."""
        before = set(approval._permanent_approved)
        with _owner(["session"]):
            self.assertIsNone(self._write(self.mishari))
        self.assertEqual(set(approval._permanent_approved), before)
        session_grants = approval._session_approved.get("session-a", set())
        self.assertNotIn("protected_instruction_file", session_grants)

    def test_a_grant_cannot_be_spent_on_a_symlink_to_another_file(self):
        """Authority is recorded against the resolved target, not the spelling."""
        link = Path(self._tmp.name) / "mishari" / "CLAUDE.md"
        try:
            os.symlink(self.nasser, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with _owner(["session", "deny"]) as owner:
            self.assertIsNone(self._write(self.mishari))
            self.assertIsNotNone(self._write(link))
            self.assertEqual(len(owner.prompts), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
