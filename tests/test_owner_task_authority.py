"""The owner's own task as the authority for the writes it implies.

An owner who tells an agent "edit Mishari's charter and remove Nasser" has
authorised those writes. These tests pin the two halves of making that safe:
the signal is minted where the owner was authenticated and nowhere else, and it
covers ordinary task material and nothing else.

They also pin the fix to the home resolution the gate depends on — which used
to be latched process-globally, so on a gateway serving fourteen agents one
profile's home decided which files were protected for all of them.
"""

import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_constants  # noqa: E402
import tools.approval_context as approval_context
import tools.approval as approval  # noqa: E402
import tools.file_tools_write_guards as file_tools  # noqa: E402
import tools.owner_task_authority as owner_authority  # noqa: E402


class _Recorder:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def decide(self, session_key, notify_cb, approval_data, surface="gateway"):
        self.prompts.append(approval_data)
        choice = self.answers.pop(0) if self.answers else "deny"
        return {"resolved": True, "choice": choice}


@contextmanager
def _gateway(answers, *, turn="turn-1", session="session-1"):
    recorder = _Recorder(answers)
    originals = (file_tools._await_gateway_decision, file_tools.get_current_session_key,
                 file_tools._current_task_authority_turn, dict(approval._gateway_notify_cbs))
    file_tools._await_gateway_decision = recorder.decide
    file_tools.get_current_session_key = lambda *a, **k: session
    file_tools._current_task_authority_turn = lambda: turn
    approval._gateway_notify_cbs[session] = lambda *a, **k: None
    try:
        yield recorder
    finally:
        (file_tools._await_gateway_decision, file_tools.get_current_session_key,
         file_tools._current_task_authority_turn, cbs) = originals
        approval._gateway_notify_cbs.clear()
        approval._gateway_notify_cbs.update(cbs)


class OwnerTaskAuthorityTests(unittest.TestCase):
    """One authenticated owner message, and what it does and does not license."""

    def setUp(self):
        owner_authority.clear_all()
        file_tools.clear_task_instruction_authority()
        file_tools.reset_real_hermes_home_cache()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)

        # A Hermes installation with two agents, and a directory that is not
        # part of it at all.
        self.root = root / "hermes"
        self.acting_home = self.root / "profiles" / "abu-saud"
        self.other_home = self.root / "profiles" / "mishari"
        self.elsewhere = root / "elsewhere"
        for path in (self.acting_home, self.other_home, self.elsewhere):
            path.mkdir(parents=True, exist_ok=True)
        self.own_charter = self.acting_home / "SOUL.md"
        self.other_charter = self.other_home / "SOUL.md"
        self.other_agents_file = self.other_home / "AGENTS.md"
        self.unrelated = self.elsewhere / "SOUL.md"
        for path in (self.own_charter, self.other_charter,
                     self.other_agents_file, self.unrelated):
            path.write_text("charter\n", encoding="utf-8")

        self._home_override = hermes_constants.set_hermes_home_override(str(self.acting_home))
        self._patched = {
            "root": file_tools._profile_owning_path,
            "acting": file_tools._acting_profile_name,
        }
        file_tools._acting_profile_name = lambda: "abu-saud"
        real_root = os.path.realpath(str(self.root))

        def owning(target):
            target = os.path.realpath(target)
            if not (target == real_root or target.startswith(real_root + os.sep)):
                return None
            rel = os.path.relpath(target, real_root).split(os.sep)
            if rel and rel[0] == "profiles" and len(rel) >= 2:
                return rel[1]
            return "default"

        file_tools._profile_owning_path = owning

    def tearDown(self):
        file_tools._profile_owning_path = self._patched["root"]
        file_tools._acting_profile_name = self._patched["acting"]
        try:
            hermes_constants.reset_hermes_home_override(self._home_override)
        except Exception:
            hermes_constants.set_hermes_home_override(None)
        owner_authority.clear_all()
        file_tools.clear_task_instruction_authority()
        file_tools.reset_real_hermes_home_cache()
        self._tmp.cleanup()

    def _owner_sends_a_task(self, session="session-1", turn="turn-1"):
        """What the authenticated ingress does, then what the turn does."""
        nonce = owner_authority.mint_pending([session], profile_home=str(self.acting_home),
                                             source="gateway.prompt.submit")
        return owner_authority.bind_turn([session], turn, nonce=nonce)

    def _write(self, path):
        return file_tools._check_protected_instruction_write([str(path)])

    # -- the owner's task is the authority ----------------------------------

    def test_owner_task_needs_no_approval_for_the_write_it_implies(self):
        self.assertTrue(self._owner_sends_a_task())
        with _gateway(["deny"]) as owner:
            self.assertIsNone(self._write(self.other_charter))
            self.assertEqual(owner.prompts, [], "the owner already said so")

    def test_every_necessary_write_in_that_task_is_covered(self):
        self.assertTrue(self._owner_sends_a_task())
        with _gateway(["deny"]) as owner:
            self.assertIsNone(self._write(self.other_charter))
            self.assertIsNone(self._write(self.other_agents_file))
            self.assertIsNone(self._write(self.other_charter))
            self.assertEqual(owner.prompts, [])

    # -- what it cannot be ---------------------------------------------------

    def test_an_agent_task_cannot_mint_owner_authority(self):
        """Nothing minted: a hosted-room or internal turn never reaches ingress."""
        with _gateway(["deny"]) as owner:
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_quoting_the_owner_cannot_mint_authority(self):
        """The signal is not text, so no text can produce it.

        The only way to hold authority is for the ingress to have minted it —
        there is no argument, header or phrase a model can emit that reaches
        `mint_pending`, and a turn that claims one finds nothing to claim.
        """
        self.assertFalse(owner_authority.bind_turn(["session-1"], "turn-1", nonce="nope"))
        with _gateway(["deny"]) as owner:
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(owner.prompts), 1)

    def test_the_agents_own_charter_is_gated_on_its_own_initiative(self):
        """"It lives under my own home" is not authority over your own charter.

        This is the file an agent would rewrite to widen its own boundaries, so
        living inside the agent's own home does not exempt it. Without an
        owner-sent task there is no authority here at all and the gate asks.
        """
        self.assertEqual(
            file_tools._protected_instruction_reason(str(self.own_charter)), "abu-saud/SOUL.md")
        with _gateway(["deny"]) as owner:
            blocked = self._write(self.own_charter)
            self.assertIsNotNone(blocked)
            self.assertIn("BLOCKED", blocked)
            self.assertEqual(len(owner.prompts), 1)

    def test_the_owner_may_task_an_agent_to_edit_its_own_charter(self):
        """And when they do, it is not asked about twice."""
        self.assertTrue(self._owner_sends_a_task())
        with _gateway(["deny"]) as owner:
            self.assertIsNone(self._write(self.own_charter))
            self.assertEqual(owner.prompts, [])

    def test_ordinary_files_under_the_agents_home_stay_ordinary(self):
        """The home is not turned into a protected filesystem.

        Only the files that define what the agent is are protected there; its
        notes, its caches and its work stay as writable as they were.
        """
        for name in ("notes.md", "README.md", "scratch.txt", "report.pdf"):
            ordinary = self.acting_home / name
            ordinary.write_text("x\n", encoding="utf-8")
            self.assertIsNone(
                file_tools._protected_instruction_reason(str(ordinary)),
                f"{name} under the agent's own home must stay ordinary")
            self.assertIsNone(self._write(ordinary))

    def test_privilege_expansion_by_another_route_is_untouched(self):
        """The guards that were already there still are.

        This gate covers instruction files. The hard blocks on credentials and
        on `config.yaml` — where an agent would go to widen its own permissions
        directly — are separate and unchanged by any of this.
        """
        from agent import file_safety
        denied = file_safety.build_write_denied_paths(Path(str(self.acting_home)))
        self.assertTrue(denied, "the credential denylist must still exist")
        # config.yaml is refused outright, with no approval offered at all.
        config = self.acting_home / "config.yaml"
        config.write_text("model: x\n", encoding="utf-8")
        self.assertIsNotNone(file_tools._check_sensitive_path(str(config)))

    def test_a_materially_unrelated_file_is_still_gated(self):
        self.assertTrue(self._owner_sends_a_task())
        with _gateway(["deny"]) as owner:
            blocked = self._write(self.unrelated)
            self.assertIsNotNone(blocked)
            self.assertEqual(len(owner.prompts), 1)

    def test_one_unrelated_target_gates_the_whole_write(self):
        """A batch is judged as a batch: one uncovered file asks for all."""
        self.assertTrue(self._owner_sends_a_task())
        with _gateway(["deny"]) as owner:
            blocked = file_tools._check_protected_instruction_write(
                [str(self.other_charter), str(self.unrelated)])
            self.assertIsNotNone(blocked)
            self.assertEqual(len(owner.prompts), 1)

    # -- boundaries ----------------------------------------------------------

    def test_another_agent_cannot_reuse_it(self):
        """A grant is filed under the conversation that produced it.

        Turn ids embed the agent's own session, so two agents never share one;
        the property is asserted where it is decided — another agent's turn
        finds nothing to claim, and its turn id holds no grant.
        """
        nonce = owner_authority.mint_pending(["abu-saud"], profile_home=str(self.acting_home))
        majed_turn = "20260901_1200_majed:task-9:ab12cd34"
        # Even carrying the nonce, a different agent's session cannot claim it:
        # the mint is filed under abu-saud's keys, not majed's.
        self.assertFalse(owner_authority.bind_turn(["majed"], majed_turn, nonce=nonce))
        self.assertIsNone(owner_authority.turn_authority(majed_turn))
        with _gateway(["deny"], turn=majed_turn) as other:
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(other.prompts), 1)

    def test_another_task_cannot_reuse_it(self):
        self.assertTrue(self._owner_sends_a_task(turn="turn-1"))
        with _gateway(["deny"], turn="turn-2") as later:
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(later.prompts), 1)

    def test_a_revoked_mint_cannot_be_claimed(self):
        """A submit that mints but does not start a turn revokes its mint, so a
        later turn on the same session finds nothing to claim.
        """
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        self.assertTrue(owner_authority.revoke_pending(nonce))
        # Even carrying the (revoked) nonce, there is nothing left to claim.
        self.assertFalse(owner_authority.bind_turn(["session-1"], "turn-later", nonce=nonce))
        # Revoking an unknown nonce is a harmless no-op.
        self.assertFalse(owner_authority.revoke_pending("does-not-exist"))

    def test_clearing_a_sessions_queue_drops_its_pending_mints(self):
        """When a session's queue is discarded — a stop, a cancel, a reset — the
        mints those queued submissions carried go with it, so a later turn on the
        same session cannot claim authority from a submission that was thrown
        away. A different session's pending mint is untouched.
        """
        n1 = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        n2 = owner_authority.mint_pending(["session-2"], profile_home=str(self.acting_home))
        dropped = owner_authority.clear_pending_for_session(["session-1"])
        self.assertEqual(dropped, 1)
        self.assertFalse(owner_authority.bind_turn(["session-1"], "turn-after-cancel", nonce=n1))
        # The other session keeps its mint.
        self.assertTrue(owner_authority.bind_turn(["session-2"], "turn-2", nonce=n2))
        # "default" is not a real session name, and clearing nothing is a no-op.
        self.assertEqual(owner_authority.clear_pending_for_session(["default"]), 0)
        self.assertEqual(owner_authority.clear_pending_for_session([]), 0)

    def test_a_grant_is_single_use_and_cannot_be_replayed(self):
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        self.assertTrue(owner_authority.bind_turn(["session-1"], "turn-1", nonce=nonce))
        self.assertFalse(owner_authority.bind_turn(["session-1"], "turn-2", nonce=nonce),
                         "one owner message starts one task")

    def test_an_orphaned_mint_is_claimable_only_by_the_turn_that_carries_it(self):
        """A mint whose own turn was refused before binding stays pending, but
        it is bound to a nonce no other turn carries. A synthesized turn (no
        nonce) and any turn carrying a different nonce both find nothing — so a
        wake-up, an auto-continue, or the owner's next task cannot inherit it.
        Only the turn dispatched with the exact nonce claims it.
        """
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        # A synthesized/later turn carries no nonce: no claim, and the mint is
        # left intact for its rightful turn.
        self.assertFalse(owner_authority.bind_turn(["session-1"], "synthesized-turn"))
        # A turn carrying a different nonce: still nothing.
        self.assertFalse(owner_authority.bind_turn(["session-1"], "other-turn", nonce="different"))
        # The turn the ingress actually minted for still binds it.
        self.assertTrue(owner_authority.bind_turn(["session-1"], "owners-turn", nonce=nonce))

    def test_authority_expires_when_the_task_ends(self):
        self.assertTrue(self._owner_sends_a_task())
        self.assertEqual(owner_authority.clear_turn("turn-1"), 1)
        with _gateway(["deny"]) as after:
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(after.prompts), 1)

    def test_a_profile_mismatch_is_rejected(self):
        """A grant minted for one profile says nothing about another."""
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.other_home))
        owner_authority.bind_turn(["session-1"], "turn-1", nonce=nonce)
        self.assertIsNone(
            owner_authority.turn_authority("turn-1", profile_home=str(self.acting_home)))
        self.assertIsNotNone(owner_authority.turn_authority("turn-1"))

    def test_a_stale_pending_mint_never_attaches_to_a_later_turn(self):
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        with owner_authority._lock:
            # Pending mints are a per-session queue, so two messages sent while
            # the agent is busy run as two turns in order.
            record = owner_authority._pending["session-1"][0]
            record["minted_at"] -= owner_authority.PENDING_TTL_SECONDS + 1
        self.assertFalse(owner_authority.bind_turn(["session-1"], "turn-late", nonce=nonce))

    def test_an_unresolved_session_name_can_neither_mint_nor_claim(self):
        """"default" is what an unresolved session key becomes, and it is refused.

        Filing a grant under it would let one conversation's authority be
        claimed by another conversation's turn.
        """
        self.assertIsNone(owner_authority.mint_pending(["default"], profile_home=None))
        self.assertIsNone(owner_authority.mint_pending([""], profile_home=None))
        nonce = owner_authority.mint_pending(["session-1"], profile_home=str(self.acting_home))
        self.assertFalse(owner_authority.bind_turn(["default"], "turn-x", nonce=nonce))

    def test_the_explicit_approval_flow_still_works(self):
        """Where the owner is asked, their answer still binds exactly as before."""
        with _gateway(["once", "deny"]) as owner:
            # No owner task minted: this is the ordinary path, unchanged.
            self.assertIsNone(self._write(self.other_charter))
            self.assertIsNotNone(self._write(self.other_charter))
            self.assertEqual(len(owner.prompts), 2)


class PerProfileHomeResolutionTests(unittest.TestCase):
    """The home the gate compares against is the one this request runs as."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home_a = root / "profiles" / "alpha"
        self.home_b = root / "profiles" / "beta"
        for home in (self.home_a, self.home_b):
            home.mkdir(parents=True, exist_ok=True)
        file_tools.reset_real_hermes_home_cache()

    def tearDown(self):
        file_tools.reset_real_hermes_home_cache()
        self._tmp.cleanup()

    @contextmanager
    def _running_as(self, home):
        token = hermes_constants.set_hermes_home_override(str(home))
        try:
            yield
        finally:
            try:
                hermes_constants.reset_hermes_home_override(token)
            except Exception:
                hermes_constants.set_hermes_home_override(None)

    def _resolved(self, home):
        with self._running_as(home):
            return file_tools._get_real_hermes_home()

    def test_a_then_b(self):
        self.assertEqual(self._resolved(self.home_a), os.path.realpath(str(self.home_a)))
        self.assertEqual(self._resolved(self.home_b), os.path.realpath(str(self.home_b)))

    def test_b_then_a(self):
        self.assertEqual(self._resolved(self.home_b), os.path.realpath(str(self.home_b)))
        self.assertEqual(self._resolved(self.home_a), os.path.realpath(str(self.home_a)))

    def test_concurrent_requests_do_not_leak_across_profiles(self):
        """Fourteen agents on one process, resolving at the same moment."""
        results: dict[str, set] = {"alpha": set(), "beta": set()}
        errors: list[BaseException] = []
        barrier = threading.Barrier(14)

        def run(home, name):
            try:
                barrier.wait(timeout=10)
                for _ in range(40):
                    with self._running_as(home):
                        results[name].add(file_tools._get_real_hermes_home())
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=run,
                             args=((self.home_a, "alpha") if index % 2 == 0
                                   else (self.home_b, "beta")))
            for index in range(14)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(results["alpha"], {os.path.realpath(str(self.home_a))})
        self.assertEqual(results["beta"], {os.path.realpath(str(self.home_b))})

    def test_the_exemption_follows_the_profile_that_is_running(self):
        """The whole point: which files this gate covers is per profile.

        An ordinary file inside a request's own home is its own business; the
        same file, seen by a different profile's request, is configuration
        steering somebody else and is gated. Latching one home process-globally
        got this backwards for thirteen of fourteen agents.
        """
        nested = self.home_a / ".hermes" / "settings.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("settings\n", encoding="utf-8")
        with self._running_as(self.home_a):
            self.assertIsNone(file_tools._protected_instruction_reason(str(nested)))
        with self._running_as(self.home_b):
            self.assertIsNotNone(file_tools._protected_instruction_reason(str(nested)))

    def test_a_charter_is_protected_whichever_profile_looks_at_it(self):
        """No home exempts a charter any more, including its own."""
        charter = self.home_a / "SOUL.md"
        charter.write_text("charter\n", encoding="utf-8")
        for home in (self.home_a, self.home_b):
            with self._running_as(home):
                self.assertEqual(
                    file_tools._protected_instruction_reason(str(charter)), "alpha/SOUL.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
