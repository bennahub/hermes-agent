"""``hermes runtime`` — canonical hosted Hermes status."""

from __future__ import annotations

import json
from types import SimpleNamespace


def runtime_command(args) -> None:
    from hermes_cli.runtime_truth import collect_canonical_runtime, render_runtime

    payload = collect_canonical_runtime(persist=True)
    as_json = getattr(args, "json", True)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(render_runtime(payload))


def runtime_args(*, as_json: bool = True):
    return SimpleNamespace(json=as_json)
