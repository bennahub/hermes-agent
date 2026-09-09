"""Carry inherited execution authority through parsed native CLI dispatch."""
import json
import copy
import os
import sys
import uuid
from contextvars import ContextVar

_ADMIN_INGRESS = ContextVar("native_cli_admin_ingress", default=None)


def current_admin_ingress():
    """Only a direct native parsed dispatch may authorize a fresh admin assignment."""
    return copy.deepcopy(_ADMIN_INGRESS.get())


def dispatch_native_command(args, *, argv=None):
    """A shell-started subcommand inherits its native assignment, never owner status."""
    marker = os.environ.get("HERMES_EXECUTION_SCOPE")
    if marker is None:
        command = getattr(args, "command", None)
        action = getattr(args, "cron_command" if command == "cron" else "autonomy_command", None)
        fields = {
            "cron": ("prompt", "schedule", "job_id", "name", "deliver", "repeat", "skills", "script", "no_agent", "monitor_script", "monitor_url", "workdir", "context_from"),
            "autonomy": ("to", "target", "goal", "deliverable", "scope", "evidence", "work", "work_id"),
        }
        permitted = (command == "cron" and action in {"create", "edit"}) or (command == "autonomy" and action == "delegate")
        ingress = {"id": "native-admin:" + uuid.uuid4().hex, "command": command, "action": action,
                   "arguments": {key: getattr(args, key) for key in fields[command] if hasattr(args, key)}} if permitted else None
        token = _ADMIN_INGRESS.set(copy.deepcopy(ingress))
        try:
            return args.func(args)
        finally:
            _ADMIN_INGRESS.reset(token)
    if getattr(args, "command", None) in {None, "chat", "acp", "rl"}:
        return args.func(args)
    from agent.execution_scope import execute_native_scoped
    from gateway.status import get_process_start_time
    invocation_id = f"cli:{os.getpid()}:{get_process_start_time(os.getpid())}:{uuid.uuid4().hex}"
    exit_holder = []
    def invoke(_arguments):
        try:
            return args.func(args)
        except SystemExit as exc:
            # The handler completed normally through its native exit contract;
            # record that outcome before reproducing the exit at the boundary.
            exit_holder.append(exc)
            return {"exit_code": exc.code}
    try:
        locator = json.loads(marker)
        if not isinstance(locator, dict) or not isinstance(locator.get("assignment_id"), str):
            raise ValueError("Malformed inherited CLI execution assignment")
        result = execute_native_scoped(locator, locator["assignment_id"], None,
            "hermes_cli", {"subcommand": args.command,
                           "argv": list(sys.argv[1:] if argv is None else argv)},
            invoke, invocation_id=invocation_id)
    except (ValueError, KeyError, TypeError) as exc:
        print("Execution blocked: " + str(exc), file=sys.stderr)
        return 1
    if isinstance(result, str):
        try:
            denied = json.loads(result)
        except ValueError:
            denied = None
        if isinstance(denied, dict) and str(denied.get("error_type", "")).startswith("execution_scope_"):
            print("Execution blocked: " + str(denied.get("error", "scope unavailable")), file=sys.stderr)
            return 1
    if exit_holder:
        raise exit_holder[0]
    return result
