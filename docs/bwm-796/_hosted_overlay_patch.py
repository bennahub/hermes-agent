#!/usr/bin/env python3
"""Idempotent overlay of BWM-796 shared-file hunks onto the live hosted tree."""
from pathlib import Path

root = Path("/home/hermes/.hermes/hermes-agent")


def must_contain(path: Path, needle: str, label: str) -> str:
    text = path.read_text()
    if needle not in text:
        raise SystemExit(f"NEEDLE MISSING {label} in {path}")
    return text


# 1) config_defaults.py
p = root / "hermes_cli/config_defaults.py"
text = p.read_text()
if "agent_computer" in text and '"runtime": "memory"' in text:
    print("SKIP config_defaults")
else:
    needle = "    # Filesystem checkpoints — automatic snapshots before destructive file ops.\n"
    text = must_contain(p, needle, "config_defaults")
    insert = (
        "    # Durable Agent Computer runtime (BWM-796). New key — no version bump.\n"
        '    # "memory" is the process default so ordinary chat never launches Chrome.\n'
        '    # "chromium" uses the host Chromium-family binary on an identity-owned\n'
        "    # user-data-dir with loopback CDP. HERMES_AGENT_COMPUTER_RUNTIME remains\n"
        "    # a test/operator override only; user-facing docs point here.\n"
        '    "agent_computer": {\n'
        '        "runtime": "memory",\n'
        "    },\n"
        "\n"
    )
    p.write_text(text.replace(needle, insert + needle, 1))
    print("PATCHED config_defaults")

# 2) toolsets.py
p = root / "toolsets.py"
text = p.read_text()
if '"agent_computer"' in text:
    print("SKIP toolsets")
else:
    needle = '    "browser": {\n'
    text = must_contain(p, needle, "toolsets")
    insert = (
        '    "agent_computer": {\n'
        '        "description": (\n'
        '            "Opt-in durable AgentComputer tools. Ordinary chat does not "\n'
        '            "enable this toolset; enabling it still requires an explicit "\n'
        '            "computer_ensure / computer_wake call."\n'
        "        ),\n"
        '        "tools": [\n'
        '            "computer_ensure",\n'
        '            "computer_status",\n'
        '            "computer_wake",\n'
        '            "computer_observe",\n'
        '            "computer_act",\n'
        "        ],\n"
        '        "includes": [],\n'
        "    },\n"
        "\n"
    )
    p.write_text(text.replace(needle, insert + needle, 1))
    print("PATCHED toolsets")

# 3) backup.py
p = root / "hermes_cli/backup.py"
text = p.read_text()
if '"agent-computers"' in text:
    print("SKIP backup")
else:
    needle = '    "browser-profile",\n'
    text = must_contain(p, needle, "backup")
    insert = (
        "    # Durable AgentComputer / BrowserIdentity store (BWM-796). Holds managed\n"
        "    # Chromium user-data-dirs and control-plane SQLite. Excluded so cookie\n"
        "    # jars and fencing state never enter a backup archive.\n"
        '    "agent-computers",\n'
    )
    p.write_text(text.replace(needle, needle + insert, 1))
    print("PATCHED backup")

# 4) file_safety.py
p = root / "agent/file_safety.py"
text = p.read_text()
if "agent-computers" in text:
    print("SKIP file_safety")
else:
    needle = (
        '            f"Access denied: {path} is inside the Hermes real-profile browser "\n'
        '            "snapshot (copied cookies/logins) and cannot be read directly. "\n'
        '            "(Defense-in-depth — not a security boundary; the terminal tool "\n'
        '            "can still bypass.)"\n'
        "        )\n"
        "\n"
        "    # Block common secret-bearing project-local .env files anywhere on disk.\n"
    )
    text = must_contain(p, needle, "file_safety")
    extra_root = (
        ' + ([_hermes_root_path()] if "_hermes_root_path" in globals() else [])'
        if "_hermes_root_path" in text
        else ""
    )
    insert = (
        '            f"Access denied: {path} is inside the Hermes real-profile browser "\n'
        '            "snapshot (copied cookies/logins) and cannot be read directly. "\n'
        '            "(Defense-in-depth — not a security boundary; the terminal tool "\n'
        '            "can still bypass.)"\n'
        "        )\n"
        "\n"
        "    # agent-computers/: durable BrowserIdentity user-data-dirs (BWM-796).\n"
        "    # Same credential class as browser-profile/; deny the prefix so Cookies /\n"
        "    # Login Data / Web Data cannot be read via file tools.\n"
        f"    for hd in list(hermes_dirs){extra_root}:\n"
        "        try:\n"
        '            agent_computers = (hd / "agent-computers").resolve()\n'
        "        except Exception:\n"
        "            continue\n"
        "        if resolved == agent_computers:\n"
        "            return (\n"
        '                f"Access denied: {path} is the Hermes Agent Computer store "\n'
        '                "(managed browser identities) and cannot be read directly. "\n'
        '                "(Defense-in-depth — not a security boundary; the terminal "\n'
        '                "tool can still bypass.)"\n'
        "            )\n"
        "        try:\n"
        "            resolved.relative_to(agent_computers)\n"
        "        except ValueError:\n"
        "            continue\n"
        "        return (\n"
        '            f"Access denied: {path} is inside the Hermes Agent Computer "\n'
        '            "store (managed browser identities) and cannot be read directly. "\n'
        '            "(Defense-in-depth — not a security boundary; the terminal tool "\n'
        '            "can still bypass.)"\n'
        "        )\n"
        "\n"
        "    # Block common secret-bearing project-local .env files anywhere on disk.\n"
    )
    p.write_text(text.replace(needle, insert, 1))
    print("PATCHED file_safety")

# 5) web_server.py
p = root / "hermes_cli/web_server.py"
text = p.read_text()
if '"agent_computer": "agent"' not in text:
    needle = '    "session": "general",\n'
    text = must_contain(p, needle, "web_server category")
    insert = (
        '    "session": "general",\n'
        "    # `agent_computer.runtime` is the only schema-surfaced Agent Computer\n"
        "    # field — fold it into the agent tab rather than spawning a one-field\n"
        "    # orphan category.\n"
        '    "agent_computer": "agent",\n'
    )
    p.write_text(text.replace(needle, insert, 1))
    print("PATCHED web_server category")
else:
    print("SKIP web_server category")

text = p.read_text()
if "agent_computer as _agent_computer_routes" not in text:
    needle = "app.include_router(_tools_routes.router)\n"
    text = must_contain(p, needle, "web_server router")
    insert = (
        "app.include_router(_tools_routes.router)\n"
        "from hermes_cli.web_routers import agent_computer as _agent_computer_routes  # noqa: E402\n"
        "\n"
        "app.include_router(_agent_computer_routes.router)\n"
    )
    p.write_text(text.replace(needle, insert, 1))
    print("PATCHED web_server router")
else:
    print("SKIP web_server router")

# 6) tui_gateway/server.py
p = root / "tui_gateway/server.py"
text = p.read_text()
if "methods_agent_computer" not in text:
    needle = "    methods_browser_control as _methods_browser_control,\n"
    text = must_contain(p, needle, "server import")
    text = text.replace(
        needle,
        "    methods_agent_computer as _methods_agent_computer,\n" + needle,
        1,
    )
    needle2 = "    _methods_bot_relay,\n):\n"
    if needle2 not in text:
        raise SystemExit("NEEDLE MISSING server register")
    text = text.replace(
        needle2,
        "    _methods_bot_relay,\n    _methods_agent_computer,\n):\n",
        1,
    )
    p.write_text(text)
    print("PATCHED tui_gateway/server.py")
else:
    print("SKIP tui_gateway/server.py")

# 7) tui_gateway/ws.py
p = root / "tui_gateway/ws.py"
text = p.read_text()
if "release_owner_for_transport_if_active" not in text:
    needle = (
        "            except Exception:\n"
        '                _log.exception("ws browser-controller disconnect failed peer=%s", peer)\n'
        "\n"
        "            transport.close()\n"
    )
    text = must_contain(p, needle, "ws")
    insert = (
        "            except Exception:\n"
        '                _log.exception("ws browser-controller disconnect failed peer=%s", peer)\n'
        "            try:\n"
        "                from gateway.agent_computer import release_owner_for_transport_if_active\n"
        "\n"
        "                await asyncio.to_thread(\n"
        "                    release_owner_for_transport_if_active, transport\n"
        "                )\n"
        "            except Exception:\n"
        '                _log.exception("ws agent-computer owner disconnect failed peer=%s", peer)\n'
        "\n"
        "            transport.close()\n"
    )
    p.write_text(text.replace(needle, insert, 1))
    print("PATCHED tui_gateway/ws.py")
else:
    print("SKIP tui_gateway/ws.py")

print("ALL_PATCH_STEPS_DONE")
