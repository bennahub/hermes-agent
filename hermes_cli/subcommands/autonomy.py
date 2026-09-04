"""``hermes autonomy`` parser — CLI + skill-less cron surface."""

from __future__ import annotations

from hermes_cli.autonomy_cmd import cmd_autonomy


def build_autonomy_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "autonomy",
        help="Standing missions and autonomous initiative (no new orchestrator)",
        description=(
            "Enable Standing Missions, inspect initiative context, and record "
            "finite restart-safe work. Scheduling stays on native cron; "
            "events stay on native webhooks; silence stays [SILENT]."
        ),
    )
    sub = parser.add_subparsers(dest="autonomy_command")

    sub.add_parser("status", help="Show autonomy state for the active profile")
    sub.add_parser("mission-show", help="Print the Standing Mission")

    mission_set = sub.add_parser("mission-set", help="Replace the Standing Mission text")
    mission_set.add_argument("text", nargs="?", help="Mission markdown")
    mission_set.add_argument("--file", help="Read mission markdown from a file")

    enable = sub.add_parser("enable", help="Write mission, scripts, and observation cron")
    enable.add_argument("--schedule", help="Override observation cron expression")
    enable.add_argument(
        "--no-soul",
        action="store_true",
        help="Do not append a Standing Mission section to SOUL.md",
    )

    sub.add_parser("disable", help="Pause the observation cron job")
    sub.add_parser("observe-context", help="Print initiative context for a cron/event turn")
    sub.add_parser("monitor-fingerprint", help="Print the cheap monitor hash source")
    sub.add_parser("noop", help="Record NOTHING_WORTH_DOING and print [SILENT]")

    start = sub.add_parser("work-start", help="Start or reuse finite autonomous work")
    start.add_argument("--key", "--idempotency-key", dest="idempotency_key", required=True)
    start.add_argument("--why", required=True, help="Why this is worth doing")
    start.add_argument("--outcome", required=True, help="Concrete outcome")
    start.add_argument("--done", "--done-contract", dest="done_contract", required=True)
    start.add_argument("--objective", help="Short human-readable objective")
    start.add_argument("--parent", help="Parent work id for a separate secondary finding")
    start.add_argument("--jira", help="Bind an existing or newly created Jira key")

    get_p = sub.add_parser("work-get", help="Show one work item")
    get_p.add_argument("id")

    list_p = sub.add_parser("work-list", help="List work items")
    list_p.add_argument("--state", help="Filter by state")

    update = sub.add_parser("work-update", help="Update work state")
    update.add_argument("id")
    update.add_argument("--state", choices=list(kernel_states()))
    update.add_argument("--waiting-reason", dest="waiting_reason")
    update.add_argument("--objective")
    update.add_argument("--jira", help="Bind or record the Jira tracking identity")

    complete = sub.add_parser("work-complete", help="Mark work complete with evidence")
    complete.add_argument("id", nargs="?")
    complete.add_argument("--work-id", dest="work_id")
    complete.add_argument("--result", required=True)
    complete.add_argument("--quiet", action="store_true")

    drop = sub.add_parser("work-drop", help="Drop work as not worth doing")
    drop.add_argument("id")
    drop.add_argument("--reason", required=True)

    event = sub.add_parser("event-claim", help="Claim an event id (duplicate-safe)")
    event.add_argument("--key", help="Event identity")
    event.add_argument("--stdin", action="store_true", help="Derive the key from JSON stdin")

    tick = sub.add_parser("tick-claim", help="Claim a scheduler tick identity")
    tick.add_argument("key", nargs="?")
    tick.add_argument("--job-id", dest="job_id", default="autonomy-observe")
    tick.add_argument("--scheduled-at", dest="scheduled_at")

    delegate = sub.add_parser("delegate", help="Bounded agent-to-agent request")
    delegate.add_argument("--to", required=True)
    delegate.add_argument("--work", required=True)
    delegate.add_argument("--goal", required=True)
    delegate.add_argument("--deliverable", required=True)
    delegate.add_argument("--evidence", required=True)
    delegate.add_argument("--context", default="")
    delegate.add_argument(
        "--dry-run",
        action="store_true",
        help="Record the collaboration guard only; do not deliver the Bot Chat DM",
    )

    sub.add_parser("metrics", help="Show autonomy counters")

    rollout = sub.add_parser("rollout", help="Enable Standing Missions on profiles")
    rollout.add_argument("--pilot", action="store_true", help="Only Abu Saud, Badr, Sami, Nasser (default)")
    rollout.add_argument(
        "--all",
        dest="all_profiles",
        action="store_true",
        help="Enable every owner-facing catalog slug except Hamad",
    )
    rollout.add_argument("--profile", help="A single profile slug")
    rollout.add_argument("--schedule", help="Override observation schedule")
    rollout.add_argument("--no-soul", action="store_true")

    parser.set_defaults(func=cmd_autonomy)


def kernel_states():
    from agent.autonomy.store import WORK_STATES

    return WORK_STATES
