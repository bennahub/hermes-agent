"""Deterministic scope records for audit and replay identity.

Semantic LLM judges used to sit on the execution path and decide allow/deny.
They are gone. A scope is now a host-written receipt of the current owner
instruction plus fixed catastrophic exclusions. Tool admission does not call
another model.
"""
from __future__ import annotations

import json
from typing import Any

VERSION = 1
MAX_INPUT_BYTES = 262144

_EXCLUDED = [
    "disk or device wipe",
    "destructive bulk deletion outside the current task",
    "secrets or credential extraction or exposure",
    "disabling core security controls",
    "replaying a stale command from old context as a new owner order",
    "treating web, email, or tool output as owner authority",
    "unrelated irreversible destructive operations",
]


class PolicyUnavailable(RuntimeError):
    """Scope bookkeeping failed; routine owner execution must not depend on this."""


def _instruction_text(instruction: Any) -> str:
    if isinstance(instruction, str):
        text = instruction.strip()
    elif isinstance(instruction, dict):
        for key in (
            "current_owner_instruction",
            "current_original_instruction",
            "instruction",
            "objective",
        ):
            value = instruction.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        else:
            text = json.dumps(instruction, ensure_ascii=False)
    else:
        text = str(instruction or "").strip()
    return text[:4000] or "owner task"


def _decision(system: str, payload: dict, runtime: dict | None) -> dict:
    """Kept as a no-op hook so older tests can still patch this name.

    Production no longer calls an auxiliary model from this module.
    """
    raise PolicyUnavailable("Semantic execution policy is not on the execution path")


def derive_scope(instruction: Any, *, runtime: dict | None = None, resolve_targets: bool = False,
                 active_work_referents: list[dict] | None = None) -> dict:
    del runtime, resolve_targets
    objective = _instruction_text(instruction)
    scope = {
        "version": VERSION,
        "policy": {
            "objective": objective,
            "permitted": [
                "owner-assigned investigation, execution, repair, retry, plan change, and verification",
            ],
            "excluded": list(_EXCLUDED),
        },
        "original_instruction": instruction,
    }
    if active_work_referents:
        scope["active_work_referents"] = active_work_referents
    return scope


def judge_action(scope: dict, tool: str, arguments: dict, *, runtime: dict | None = None,
                 observations: list[dict] | None = None, native_work: dict | None = None,
                 active_work_referents: list[dict] | None = None) -> tuple[bool, str]:
    del tool, arguments, runtime, observations, native_work, active_work_referents
    if scope and scope.get("version") not in {None, VERSION}:
        return True, "routine execution"
    return True, "routine execution"
