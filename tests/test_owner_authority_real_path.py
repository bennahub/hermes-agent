"""The owner-authority contract over the path Mobile actually uses.

The unit tests around this feature passed while the real thing failed on the
owner's phone, twice, for the same reason: they supplied the identities
themselves. Production does not. The tui path names a conversation three ways,
sets none of the contextvars the platforms gateway sets, and runs the turn in a
thread where `get_current_session_key()` falls back to ``"default"`` — so a
grant filed under the session key was never found, and every owner task was
asked about anyway.

These tests wire the real components together at the seams that failed in
production — they mint and bind under the identities the tui path actually
resolves (session key falling back to "default"), publish the turn id through
the contextvar the tool dispatcher uses, and call the real gate. They do stub
two profile-identity helpers and call mint/bind directly rather than driving the
registered `prompt.submit` handler and a real early-return through
`build_turn_context`; a full handler->turn->gate integration test is worthwhile
future coverage. The only thing faked is the human at the other end of a prompt,
and most of these tests turn on never reaching one.
"""

import os
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_constants  # noqa: E402
import tools.approval_context as approval_context
import tools.approval as approval  # noqa: E402
import tools.file_tools_write_guards as file_tools  # noqa: E402
import tools.owner_task_authority as owner_authority  # noqa: E402


def _turn_id(agent_session: str, task_id: str = "task-1") -> str:
    """A turn id shaped exactly as `build_turn_context` builds one."""
    return f"{agent_session}:{task_id}:{uuid.uuid4().hex[:8]}"


class _Owner:
    """The human at the other end of an approval prompt."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def decide(self, session_key, notify_cb, approval_data, surface="gateway"):
        self.prompts.append(approval_data)
        choice = self.answers.pop(0) if self.answers else "deny"
        return {"resolved": True, "choice": choice}


class OwnerAuthorityOverTheRealPathTests(unittest.TestCase):

    def setUp(self):
        owner_authority.clear_all()
        file_tools.clear_task_instruction_authority()
        file_tools.reset_real_hermes_home_cache()

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "hermes"
        self.installation = root
        self.abu_saud_home = root / "profiles" / "abu-saud"
        self.mishari_home = root / "profiles" / "mishari"
        for home in (self.abu_saud_home, self.mishari_home):
            home.mkdir(parents=True, exist_ok=True)
        self.own_charter = self.abu_saud_home / "SOUL.md"
        self.mishari_charter = self.mishari_home / "SOUL.md"
        self.mishari_agents = self.mishari_home / "AGENTS.md"
        self.outside = Path(self._tmp.name) / "elsewhere" / "SOUL.md"
        self.outside.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.own_charter, self.mishari_charter,
                     self.mishari_agents, self.outside):
            path.write_text("charter\n", encoding="utf-8")

        # The tui path runs a turn under the INSTALLATION ROOT, not the profile
        # directory — the profile override is only applied on the platforms
        # gateway's inbound path. Reproducing that is the point: it is what made
        # the first implementation's profile check reject every real grant.
        self._home_token = hermes_constants.set_hermes_home_override(str(root))

        self._real_owning = file_tools._profile_owning_path
        real_root = os.path.realpath(str(root))

        def owning(target):
            target = os.path.realpath(target)
            if not (target == real_root or target.startswith(real_root + os.sep)):
                return None
            rel = os.path.relpath(target, real_root).split(os.sep)
            if rel and rel[0] == "profiles" and len(rel) >= 2:
                return rel[1]
            return "default"

        file_tools._profile_owning_path = owning
        self._real_acting = file_tools._acting_profile_name
        file_tools._acting_profile_name = lambda: "abu-saud"

    def tearDown(self):
        file_tools._profile_owning_path = self._real_owning
        file_tools._acting_profile_name = self._real_acting
        try:
            hermes_constants.reset_hermes_home_override(self._home_token)
        except Exception:
            hermes_constants.set_hermes_home_override(None)
        owner_authority.clear_all()
        file_tools.clear_task_instruction_authority()
        file_tools.reset_real_hermes_home_cache()
        self._tmp.cleanup()

    # -- the real path, step by step ----------------------------------------

    def _session(self, agent_session="20260831_211630_020ed9", ui_session="f5c4d711"):
        """A session dictionary shaped like the tui gateway's own."""
        return {
            "session_key": agent_session,
            "profile_home": str(self.abu_saud_home),
            "agent": type("Agent", (), {"session_id": agent_session})(),
            "ui_session": ui_session,
        }

    def _owner_submits(self, session, ui_session="f5c4d711"):
        """Exactly what the `prompt.submit` handler now does."""
        return owner_authority.mint_pending(
            [session.get("session_key"),
             getattr(session.get("agent"), "session_id", None),
             ui_session],
            profile_home=str(session.get("profile_home") or "") or None,
            source="gateway.prompt.submit",
        )

    def _turn_starts(self, session, turn_id, nonce=None):
        """Exactly what `build_turn_context` now does: bind the mint the dispatch
        chokepoint carried to this turn (agent._pending_owner_task_nonce). A turn
        the owner did not start carries none and binds nothing."""
        agent_session = getattr(session.get("agent"), "session_id", None)
        # Deliberately passing what the real call passes, including the
        # session key as this path resolves it — which is "default".
        return owner_authority.bind_turn(
            [agent_session, file_tools.get_current_session_key()], turn_id, nonce=nonce)

    @contextmanager
    def _tool_runs_in(self, turn_id, answers=("deny",)):
        """The tool dispatcher publishes the turn id; a human waits behind it."""
        owner = _Owner(answers)
        original_decide = file_tools._await_gateway_decision
        original_cbs = dict(approval._gateway_notify_cbs)
        file_tools._await_gateway_decision = owner.decide
        approval._gateway_notify_cbs[file_tools.get_current_session_key()] = lambda *a, **k: None
        token = approval_context._approval_turn_id.set(turn_id)
        try:
            yield owner
        finally:
            approval_context._approval_turn_id.reset(token)
            file_tools._await_gateway_decision = original_decide
            approval._gateway_notify_cbs.clear()
            approval._gateway_notify_cbs.update(original_cbs)

    def _write(self, *paths):
        return file_tools._check_protected_instruction_write([str(p) for p in paths])

    # -- the two failures the owner reported --------------------------------

    def test_owner_task_to_edit_the_agents_own_charter_asks_nothing(self):
        session = self._session()
        nonce = self._owner_submits(session)
        self.assertIsNotNone(nonce)
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce),
                        "the turn must find the mint without the session-key contextvar")
        with self._tool_runs_in(turn) as owner:
            self.assertIsNone(self._write(self.own_charter))
            self.assertEqual(owner.prompts, [])

    def test_owner_task_to_edit_another_agents_charter_asks_nothing(self):
        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce))
        with self._tool_runs_in(turn) as owner:
            self.assertIsNone(self._write(self.mishari_charter))
            self.assertIsNone(self._write(self.mishari_agents))
            # And a multi-file patch of the same task, in one call.
            self.assertIsNone(self._write(self.mishari_charter, self.mishari_agents))
            self.assertEqual(owner.prompts, [])

    def test_the_session_key_this_path_resolves_is_not_usable_as_a_binding(self):
        """The defect itself, pinned so it cannot come back.

        On this path `get_current_session_key()` is "default" — the fallback —
        which is why binding on it silently matched nothing.
        """
        self.assertEqual(file_tools.get_current_session_key(), "default")
        session = self._session()
        nonce = self._owner_submits(session)
        self.assertFalse(owner_authority.bind_turn(["default"], _turn_id("x"), nonce=nonce))

    # -- and the boundaries, over the same path ------------------------------

    def test_a_turn_with_no_owner_submit_behind_it_still_asks(self):
        session = self._session()
        turn = _turn_id(session["session_key"])
        self.assertFalse(self._turn_starts(session, turn), "nothing was minted")
        with self._tool_runs_in(turn) as owner:
            self.assertIsNotNone(self._write(self.mishari_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_a_second_turn_of_the_same_conversation_does_not_inherit_it(self):
        session = self._session()
        nonce = self._owner_submits(session)
        first = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, first, nonce=nonce))
        second = _turn_id(session["session_key"])
        self.assertFalse(self._turn_starts(session, second, nonce=nonce),
                         "one owner message authorises one task")
        with self._tool_runs_in(second) as owner:
            self.assertIsNotNone(self._write(self.mishari_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_a_file_outside_the_installation_still_asks(self):
        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self._turn_starts(session, turn, nonce=nonce)
        with self._tool_runs_in(turn) as owner:
            self.assertIsNotNone(self._write(self.outside))
            self.assertEqual(len(owner.prompts), 1)

    def test_the_grant_is_dropped_when_the_turn_ends(self):
        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self._turn_starts(session, turn, nonce=nonce)
        self.assertEqual(owner_authority.clear_turn(turn), 1)
        with self._tool_runs_in(turn) as owner:
            self.assertIsNotNone(self._write(self.mishari_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_the_grant_survives_the_persist_funnel_running_mid_turn(self):
        """The failure the owner hit twice, pinned.

        `_persist_session` is Hermes' "save on any exit path" funnel and runs
        dozens of times inside one turn. Clearing the grant from the hook it
        calls destroyed the owner's authority seconds after the turn claimed
        it, so the second and every later protected write in the task asked
        again — while every unit test passed, because none of them persisted.
        """
        from agent import agent_runtime_helpers

        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce))

        agent = type("Agent", (), {
            "_current_turn_id": turn,
            "_inflight_turn_id": turn,
            "_persist_disabled": True,
            "session_id": session["session_key"],
        })()

        with self._tool_runs_in(turn) as owner:
            self.assertIsNone(self._write(self.mishari_charter))
            # The agent saves state, repeatedly, as it works.
            for _ in range(5):
                agent_runtime_helpers.note_turn_persisted(agent)
            self.assertIsNone(self._write(self.mishari_agents),
                              "the owner's authority must outlive a mid-turn save")
            self.assertIsNone(self._write(self.own_charter))
            self.assertEqual(owner.prompts, [])

    def test_an_owner_authorised_permission_transfer_runs_without_asking(self):
        """The owner's actual task: move one agent's tools to another, then remove it.

        Executing it means editing several charters — the agent losing the
        work, the agent gaining it, and the acting agent's own file, which
        still names the agent being removed. All of that is one owner
        instruction, so none of it is asked about a second time; a charter
        belonging to nobody in this installation still is.
        """
        nasser_home = self.installation / "profiles" / "nasser"
        nasser_home.mkdir(parents=True, exist_ok=True)
        nasser_charter = nasser_home / "SOUL.md"
        nasser_charter.write_text("charter\n", encoding="utf-8")

        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce))

        with self._tool_runs_in(turn) as owner:
            # Take the tools off Nasser, give them to Mishari, and correct the
            # acting agent's own charter, which still refers to Nasser.
            self.assertIsNone(self._write(nasser_charter))
            self.assertIsNone(self._write(self.mishari_charter, self.mishari_agents))
            self.assertIsNone(self._write(self.own_charter))
            self.assertEqual(owner.prompts, [], "one instruction, no second asking")

            # And the boundary still holds inside the same task.
            self.assertIsNotNone(self._write(self.outside))
            self.assertEqual(len(owner.prompts), 1)

    def test_a_queued_prompt_still_carries_the_owners_authority(self):
        """A task sent while the agent is still working is still the owner's.

        `prompt.submit` returns early when the session is busy — the prompt is
        queued and runs as the next turn. Minting after that branch meant the
        owner's own task began with no authority, which is the second half of
        what the phone kept hitting.
        """
        session = self._session()
        # The ingress mints when the message arrives, not when a turn starts.
        nonce = self._owner_submits(session)
        self.assertIsNotNone(nonce)
        # ... the agent finishes what it was doing, and the queued prompt runs.
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce))
        with self._tool_runs_in(turn) as owner:
            self.assertIsNone(self._write(self.mishari_charter))
            self.assertEqual(owner.prompts, [])

    def test_a_hidden_continuation_is_not_an_owner_task(self):
        """Hermes injects its own continuations as hidden submits.

        Those are the runtime talking to itself, so they mint nothing — which
        is what keeps "the owner asked for this" meaning what it says.
        """
        session = self._session()
        # What the ingress does for a hidden submit: nothing.
        turn = _turn_id(session["session_key"])
        self.assertFalse(self._turn_starts(session, turn))
        with self._tool_runs_in(turn) as owner:
            self.assertIsNotNone(self._write(self.mishari_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_two_queued_tasks_keep_their_own_grants_in_order(self):
        """Two messages sent while the agent is busy are two tasks.

        A single pending slot meant the second message overwrote the first, so
        the first queued turn claimed the second task's grant and the second
        turn ran with none.
        """
        session = self._session()
        first_nonce = self._owner_submits(session)
        second_nonce = self._owner_submits(session)
        self.assertNotEqual(first_nonce, second_nonce)

        first_turn = _turn_id(session["session_key"], task_id="one")
        second_turn = _turn_id(session["session_key"], task_id="two")
        self.assertTrue(self._turn_starts(session, first_turn, nonce=first_nonce))
        self.assertTrue(self._turn_starts(session, second_turn, nonce=second_nonce))
        self.assertEqual(owner_authority.turn_authority(first_turn)["nonce"], first_nonce)
        self.assertEqual(owner_authority.turn_authority(second_turn)["nonce"], second_nonce)

    def test_authority_is_cleared_on_an_early_turn_exit(self):
        """A turn that exits early (preflight timeout, codex handoff) must still
        drop its authority — those paths bypass finalize_turn.
        """
        from agent import agent_runtime_helpers

        session = self._session()
        nonce = self._owner_submits(session)
        turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, turn, nonce=nonce))
        self.assertIsNotNone(owner_authority.turn_authority(turn))

        agent = type("Agent", (), {"_current_turn_id": turn})()
        agent_runtime_helpers.clear_turn_authority(agent)
        self.assertIsNone(owner_authority.turn_authority(turn),
                          "the early-exit clear must drop the owner grant")

    def test_the_session_grant_table_is_cleared_at_turn_exit(self):
        """The explicit "Approve for this task" grants had no production caller;
        the turn-exit clear is now that caller.
        """
        from agent import agent_runtime_helpers

        turn = _turn_id("s")
        file_tools._grant_task_authority("session-x", turn, ["/home/hermes/.hermes/profiles/mishari/SOUL.md"])
        self.assertTrue(file_tools._task_authority_covers(
            "session-x", turn, ["/home/hermes/.hermes/profiles/mishari/SOUL.md"]))

        agent = type("Agent", (), {"_current_turn_id": turn})()
        agent_runtime_helpers.clear_turn_authority(agent)
        self.assertFalse(file_tools._task_authority_covers(
            "session-x", turn, ["/home/hermes/.hermes/profiles/mishari/SOUL.md"]),
            "the task-scoped grant must not survive the turn")

    def test_a_background_fork_cannot_claim_the_owners_grant(self):
        """A fork copies the parent's session id; it must not inherit authority.

        Background review and delegated children run on the same conversation,
        so filing a mint under session names alone would let whichever starts
        first take it. The claim is refused for those contexts, and the mint
        stays available for the turn the owner's message actually starts.
        """
        from agent import delegation_context

        session = self._session()
        nonce = self._owner_submits(session)

        original = delegation_context.is_delegated_child_context
        delegation_context.is_delegated_child_context = lambda: True
        try:
            # The fork is a separate dispatch: it carries the owner's nonce here
            # to prove that even so the delegated context refuses the claim and
            # leaves the mint intact.
            fork_turn = _turn_id(session["session_key"], task_id="review")
            self.assertFalse(self._turn_starts(session, fork_turn, nonce=nonce))
            self.assertIsNone(owner_authority.turn_authority(fork_turn))
        finally:
            delegation_context.is_delegated_child_context = original

        # The owner's own turn still finds it.
        owners_turn = _turn_id(session["session_key"])
        self.assertTrue(self._turn_starts(session, owners_turn, nonce=nonce))

    def test_scope_never_follows_the_terminal_working_directory(self):
        """Authorisation may not be derived from something the model controls.

        The rule used to cover "the task's working tree", which resolves to the
        live terminal CWD — so `cd` into any checkout widened the agent's own
        authority to that checkout's instruction files.
        """
        foreign = Path(self._tmp.name) / "some-repo" / "AGENTS.md"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("instructions\n", encoding="utf-8")
        covered, why = file_tools._owner_task_covers(str(foreign), "default")
        self.assertFalse(covered)
        self.assertIn("outside this installation", why)

    def test_two_conversations_at_once_do_not_borrow_each_others_authority(self):
        """Fourteen agents share one process; grants must not.

        Both turns run at the same moment against one shared approval channel,
        which is why the recorder is installed once for both rather than being
        swapped per thread — two context managers racing on the same module
        attribute would test the test, not the product.
        """
        authorised = self._session(agent_session="20260831_211630_020ed9")
        nonce = self._owner_submits(authorised)
        authorised_turn = _turn_id(authorised["session_key"])
        self.assertTrue(self._turn_starts(authorised, authorised_turn, nonce=nonce))

        # A second conversation nobody authorised, running at the same time.
        other = self._session(agent_session="20260831_211846_e63ebd", ui_session="bcf15f6a")
        other_turn = _turn_id(other["session_key"])
        self.assertFalse(self._turn_starts(other, other_turn))

        prompts: dict[str, int] = {authorised_turn: 0, other_turn: 0}
        prompt_lock = threading.Lock()

        def decide(session_key, notify_cb, approval_data, surface="gateway"):
            with prompt_lock:
                prompts[approval_context._approval_turn_id.get() or ""] += 1
            return {"resolved": True, "choice": "deny"}

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)
        original_decide = file_tools._await_gateway_decision
        original_cbs = dict(approval._gateway_notify_cbs)
        file_tools._await_gateway_decision = decide
        approval._gateway_notify_cbs[file_tools.get_current_session_key()] = lambda *a, **k: None
        try:
            def run(name, turn):
                token = approval_context._approval_turn_id.set(turn)
                try:
                    barrier.wait(timeout=10)
                    results[name] = self._write(self.mishari_charter)
                finally:
                    approval_context._approval_turn_id.reset(token)

            threads = [
                threading.Thread(target=run, args=("authorised", authorised_turn)),
                threading.Thread(target=run, args=("other", other_turn)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            file_tools._await_gateway_decision = original_decide
            approval._gateway_notify_cbs.clear()
            approval._gateway_notify_cbs.update(original_cbs)

        self.assertIsNone(results["authorised"], "the owner authorised this one")
        self.assertEqual(prompts[authorised_turn], 0)
        self.assertIsNotNone(results["other"], "nobody authorised this one")
        self.assertEqual(prompts[other_turn], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
