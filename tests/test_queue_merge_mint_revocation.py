"""A merged-away submission must leave no claimable owner-task mint.

`_enqueue_prompt` merges a second text-only submission into the existing queue
envelope instead of creating a new one. Each accepted busy submission mints
owner-task authority, so without a signal the merged submission's mint would sit
in the pending queue for a later same-session turn to claim. `_enqueue_prompt`
now reports whether it created a NEW dispatchable envelope; the busy-submit path
revokes the mint when it did not. These tests pin that signal (the security
property it drives — that the revoked mint cannot be bound — is covered by
test_owner_task_authority's revocation tests).
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tui_gateway import server as S  # noqa: E402


class EnqueuePromptEnvelopeSignalTests(unittest.TestCase):

    def _session(self, **kw):
        s = {"history_lock": threading.RLock()}
        s.update(kw)
        return s

    def test_a_fresh_submission_creates_a_new_envelope(self):
        s = self._session()
        self.assertTrue(S._enqueue_prompt(s, "first task", None))
        self.assertEqual(s["queued_prompt"]["text"], "first task")

    def test_a_second_text_submission_merges_and_reports_no_new_envelope(self):
        s = self._session(queued_prompt={"text": "first task", "transport": None})
        # Merged, not newly enqueued -> False, and the text is glued losslessly.
        self.assertFalse(S._enqueue_prompt(s, "second task", None))
        self.assertEqual(s["queued_prompt"]["text"], "first task\n\nsecond task")
        self.assertNotIn("queued_prompts", s)

    def test_a_live_turn_self_duplicate_is_dropped_as_no_new_envelope(self):
        s = self._session(inflight_turn={"user": "same text"})
        # Text equal to the live turn's own prompt is never queued.
        self.assertFalse(S._enqueue_prompt(s, "same text", None))
        self.assertIsNone(s.get("queued_prompt"))

    def test_a_second_genuine_envelope_behind_an_image_turn_is_new(self):
        # An image-bearing existing envelope cannot absorb a text merge, so the
        # follow-up becomes its own dispatchable envelope (queued_prompts).
        s = self._session(queued_prompt={"text": "look", "image_paths": ["/x.png"]})
        self.assertTrue(S._enqueue_prompt(s, "and summarize", None))
        self.assertEqual([e["text"] for e in s["queued_prompts"]], ["and summarize"])

    def test_the_nonce_is_attached_to_the_exact_created_envelope(self):
        # Atomic with creation, under the same lock — no separate "stamp the
        # newest one" pass a concurrent submit could race.
        s = self._session()
        self.assertTrue(S._enqueue_prompt(s, "first", None, owner_task_nonce="N1"))
        self.assertEqual(s["queued_prompt"]["owner_task_nonce"], "N1")
        # A second genuine envelope (behind an image envelope) carries its OWN
        # nonce, not the first one's.
        s2 = self._session(queued_prompt={"text": "look", "image_paths": ["/x.png"]})
        self.assertTrue(S._enqueue_prompt(s2, "summarize", None, owner_task_nonce="N2"))
        self.assertEqual(s2["queued_prompts"][-1]["owner_task_nonce"], "N2")

    def test_a_merge_does_not_carry_the_absorbed_submissions_nonce(self):
        # The merged submission starts no turn of its own, so its nonce must not
        # ride the existing envelope (its mint is revoked by the caller instead).
        s = self._session(queued_prompt={"text": "first", "transport": None})
        self.assertFalse(S._enqueue_prompt(s, "second", None, owner_task_nonce="N2"))
        self.assertNotIn("owner_task_nonce", s["queued_prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
