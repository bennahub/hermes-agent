"""``hermes runtime`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_runtime_parser(subparsers, *, cmd_runtime: Callable) -> None:
    """Attach the ``runtime`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "runtime",
        help="Show canonical hosted Hermes runtime status",
        description=(
            "Report Hermes health from the hosted VPS release only: SHA, "
            "gateway, serve, profile schema, providers, scheduler. "
            "Does not use local checkouts or Agent Computers as truth."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Print JSON (default). Agents should use this form.",
    )
    parser.add_argument(
        "--pretty",
        action="store_false",
        dest="json",
        help="Print a short human summary instead of JSON.",
    )
    parser.set_defaults(func=cmd_runtime)
