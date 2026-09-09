"""Tests for the operator-customizable smart-approval policy.

``approvals.smart_policy`` (config.yaml) lets operators append their own
rules to the smart-approval guardian's system prompt.  Security invariants
under test:

  1. Empty/missing policy leaves the prompts exactly as they were.
  2. A non-empty policy appears in the SYSTEM message sent to call_llm
     (the trusted channel), under a clearly delimited section.
  3. The policy text NEVER appears in the user message — the user message
     carries the untrusted command text, and mixing trusted operator rules
     into that channel would dilute the guard's trust boundary.

Inspired by ChatGPT Work's customizable auto-review guardian policy.
"""

import unittest
from unittest.mock import MagicMock, patch

from tools.approval_smart import _get_smart_policy, _smart_approve

POLICY_TEXT = "Always ESCALATE commands that modify anything under /etc."


def _make_response(answer: str):
    """Build a mock LLM response with the given one-word answer."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = answer
    return mock_response


def _messages_from(mock_call_llm):
    """Extract the messages list passed to call_llm."""
    call_args = mock_call_llm.call_args
    return call_args.kwargs.get("messages") or call_args[1].get("messages", [])


class TestGetSmartPolicy(unittest.TestCase):
    """Unit tests for the config reader."""

    @patch("tools.approval_context._get_approval_config")
    def test_missing_key_returns_empty(self, mock_cfg):
        mock_cfg.return_value = {"mode": "smart"}
        assert _get_smart_policy() == ""


    @patch("tools.approval_context._get_approval_config")
    def test_policy_text_is_stripped(self, mock_cfg):
        mock_cfg.return_value = {"smart_policy": f"  {POLICY_TEXT}\n"}
        assert _get_smart_policy() == POLICY_TEXT


class TestSmartApprovePolicyInjection(unittest.TestCase):
    """Verify how the operator policy is (and is not) wired into the prompts.

    Follows the mocking pattern of test_smart_approval_injection.py:
    ``call_llm`` is patched at its source module (``agent.auxiliary_client``)
    because _smart_approve imports it lazily inside the function.  The
    config read is isolated by patching ``tools.approval_context._get_approval_config``
    so tests never touch a real config.yaml.
    """

    def test_routine_command_is_approved_without_guardian_llm(self):
        assert _smart_approve("echo hi", "flagged") == "approve"
        assert _smart_approve("systemctl restart hermes-gateway", "service restart") == "approve"

    def test_hardline_destruction_is_still_denied(self):
        assert _smart_approve("rm -rf /", "recursive delete") == "deny"

    @patch("tools.approval_context._get_approval_config")
    def test_config_read_failure_does_not_break_approval(self, mock_cfg):
        mock_cfg.side_effect = RuntimeError("config unreadable")
        assert _smart_approve("echo hi", "flagged") == "approve"

    @patch("agent.auxiliary_client.call_llm")
    def test_guardian_llm_is_not_called(self, mock_call_llm):
        assert _smart_approve("echo hi", "flagged") == "approve"
        mock_call_llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
