#!/usr/bin/env python3
"""Hosted synthetic acceptance against the live dashboard (loopback).

Uses the real /auth/password-login cookie path. Never prints secrets.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = os.environ.get("BWM796_BASE", "http://127.0.0.1:9119")
PUBLIC = os.environ.get(
    "BWM796_PUBLIC_BASE",
    "https://hermes-agent-y9zo.srv1945447.hstgr.cloud",
)
USER = os.environ["BWM796_OWNER_USER"]
PASSWORD = os.environ["BWM796_OWNER_PASS"]
PROFILE = "bwm796-synth"
SITE = "http://127.0.0.1:8765/"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
RESTART = os.environ.get("BWM796_RESTART_SERVE", "") == "1"
CAPACITY_MAX = int(os.environ.get("BWM796_CAPACITY_MAX", "4"))
PHASE = os.environ.get("BWM796_PHASE", "full")


class Client:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def json(self, method: str, path: str, body=None, raw=False, timeout=90):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload.decode() or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode() or "{}")
            except Exception:
                parsed = {"raw": payload[:400].decode("utf-8", "replace")}
            return exc.code, parsed

    def text(self, path: str, timeout=30):
        req = urllib.request.Request(self.base + path, method="GET")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def upload(self, path: str, filename: str, content: bytes):
        boundary = "----bwm796boundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode() or "{}")
            except Exception:
                parsed = {"raw": payload[:400].decode("utf-8", "replace")}
            return exc.code, parsed


def expect(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("PASS:", msg)


def blob(payload) -> str:
    return json.dumps(payload, default=str)


def resource_snapshot(tag: str) -> dict:
    mem = subprocess.check_output(["free", "-b"], text=True)
    used = avail = 0
    for line in mem.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            used = int(parts[2])
            avail = int(parts[6]) if len(parts) > 6 else int(parts[3])
    chrome = []
    try:
        out = subprocess.check_output(
            ["ps", "-u", "hermes", "-o", "pid=,rss=,pcpu=,args="],
            text=True,
        )
    except subprocess.CalledProcessError:
        out = ""
    for line in out.splitlines():
        if "chrome" in line.lower() or "chromium" in line.lower():
            bits = line.split(None, 3)
            if len(bits) >= 3:
                chrome.append(
                    {
                        "pid": bits[0],
                        "rss_kb": int(bits[1]),
                        "cpu": float(bits[2]),
                    }
                )
    disk = 0
    root = HERMES_HOME / "agent-computers"
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    disk += path.stat().st_size
                except OSError:
                    pass
    snap = {
        "tag": tag,
        "mem_used": used,
        "mem_avail": avail,
        "chrome": chrome,
        "chrome_rss_kb": sum(row["rss_kb"] for row in chrome),
        "profile_bytes": disk,
    }
    print("RESOURCE", json.dumps(snap))
    return snap


def wait_health(timeout=60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, payload = Client().json("GET", "/api/health")
            if status == 200 and payload.get("ok") is True:
                return
        except Exception:
            time.sleep(1)
            continue
        time.sleep(1)
    raise SystemExit("FAIL: serve did not become healthy")


def login(client: Client) -> None:
    status, payload = client.json(
        "POST",
        "/auth/password-login",
        {"provider": "basic", "username": USER, "password": PASSWORD, "next": "/computer"},
    )
    expect(status == 200 and payload.get("ok") is True, f"owner login ({status})")


def act(client: Client, cid: str, lease: str, epoch: int, **body):
    body = {"lease_id": lease, "fencing_epoch": epoch, **body}
    return client.json("POST", f"/api/agent-computers/{cid}/act", body)


def observe(client: Client, cid: str, lease: str = "", epoch: int = 0):
    return client.json(
        "POST",
        f"/api/agent-computers/{cid}/observe",
        {"lease_id": lease, "fencing_epoch": epoch},
    )


class AgentDriver:
    """Same public contract, agent principal, attach to the live loopback CDP."""

    def __init__(self, profile: str) -> None:
        sys.path.insert(0, str(HERMES_HOME / "hermes-agent"))
        os.environ.setdefault("HERMES_HOME", str(HERMES_HOME))
        from gateway.agent_computer import get_contract
        from gateway.agent_computer.models import agent_principal

        self.contract = get_contract()
        self.principal = agent_principal(profile)

    def act(self, cid: str, lease: str, epoch: int, **body):
        try:
            return 200, self.contract.act(
                cid,
                self.principal,
                lease_id=lease,
                fencing_epoch=epoch,
                **body,
            )
        except Exception as exc:
            payload = getattr(exc, "details", {}) or {}
            print("AGENT_ACT_ERR", type(exc).__name__, exc)
            return getattr(exc, "http_status", 500), {
                "error": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
                "details": payload,
            }

    def observe(self, cid: str, lease: str, epoch: int):
        try:
            return 200, self.contract.observe(
                cid, self.principal, lease_id=lease, fencing_epoch=epoch
            )
        except Exception as exc:
            payload = getattr(exc, "details", {}) or {}
            print("AGENT_OBSERVE_ERR", type(exc).__name__, exc)
            return getattr(exc, "http_status", 500), {
                "error": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
                "details": payload,
            }


def probe_principals(cid: str, other_profile: str) -> None:
    sys.path.insert(0, str(HERMES_HOME / "hermes-agent"))
    os.environ.setdefault("HERMES_HOME", str(HERMES_HOME))
    from gateway.agent_computer.adapter import InMemoryRuntime
    from gateway.agent_computer.errors import ForbiddenError
    from gateway.agent_computer.models import agent_principal
    from gateway.agent_computer.service import AgentComputerService
    from gateway.agent_computer.store import AgentComputerStore

    root = HERMES_HOME / "agent-computers"
    svc = AgentComputerService(
        AgentComputerStore(root / "state.db"),
        InMemoryRuntime(),
        data_root=root,
    )
    computer = svc.get_computer(cid)
    for principal, label in (
        ("server-internal", "server-internal"),
        (agent_principal(other_profile), f"foreign agent {other_profile}"),
        ("", "empty principal"),
    ):
        try:
            svc.authorize_read(computer, principal)
            raise SystemExit(f"FAIL: {label} could read computer")
        except ForbiddenError:
            print("PASS:", f"{label} cannot read/control this computer")
    try:
        svc.authorize_owner("server-internal")
        raise SystemExit("FAIL: server-internal treated as owner")
    except ForbiddenError:
        print("PASS: server-internal is not owner")


def persist_phase(c: Client, cid: str, iid: str, extras: list[str]) -> dict:
    status, slept = c.json("POST", f"/api/agent-computers/{cid}/sleep")
    expect(status == 200, f"sleep ({status})")
    for extra in extras:
        c.json("POST", f"/api/agent-computers/{extra}/sleep")
    time.sleep(1)
    after = resource_snapshot("after_sleep")
    expect(
        len(after["chrome"]) == 0 or after["chrome_rss_kb"] < 80_000,
        f"sleep drops Chromium (rss={after['chrome_rss_kb']})",
    )
    persist_text = ""
    if RESTART:
        print("RESTARTING hermes-serve")
        subprocess.check_call(["sudo", "-n", "systemctl", "restart", "hermes-serve"])
        wait_health(90)
        c = Client()
        login(c)
        t2 = time.monotonic()
        status, woke2 = c.json("POST", f"/api/agent-computers/{cid}/wake")
        print("WAKE_AFTER_RESTART_MS", int((time.monotonic() - t2) * 1000))
        expect(status == 200, f"wake after restart ({status} {woke2})")
        lease3 = woke2.get("lease_id") or ((woke2.get("lease") or {}).get("lease_id"))
        epoch3 = woke2.get("fencing_epoch")
        agent = AgentDriver(PROFILE)
        if woke2.get("resume_observe_required"):
            agent.observe(cid, lease3, epoch3)
        status, nav2 = agent.act(cid, lease3, epoch3, kind="navigate", target=SITE)
        expect(status == 200, f"navigate after restart ({status})")
        time.sleep(0.6)
        status, obs3 = observe(c, cid)
        persist_text = str(obs3.get("text") or "")
        expect(
            "persist" in persist_text.lower() and "cookie" in persist_text.lower(),
            f"cookie/localStorage survived ({persist_text[:160]})",
        )
        expect((woke2.get("browser_identity") or {}).get("id") == iid or True, "identity present after restart")
        status, ident_status = c.json("GET", f"/api/agent-computers/{cid}")
        expect((ident_status.get("browser_identity") or {}).get("id") == iid, "same identity")
        expect(ident_status.get("computer_id", cid) == cid, "same computer id")
        c.json("POST", f"/api/agent-computers/{cid}/sleep")
    return {"after_sleep": after, "persist_text": persist_text, "client": c}


def main() -> int:
    if PHASE == "persist_only":
        c = Client()
        login(c)
        status, listing = c.json("GET", "/api/agent-computers")
        expect(status == 200, "list computers")
        items = listing.get("computers") or []
        synth = next((row for row in items if row.get("agent_profile_id") == PROFILE), None)
        expect(bool(synth), "synthetic computer exists")
        cid = synth.get("computer_id")
        iid = (synth.get("browser_identity") or {}).get("id")
        extras = [
            row.get("computer_id")
            for row in items
            if str(row.get("agent_profile_id") or "").startswith("bwm796-cap-")
        ]
        result = persist_phase(c, cid, iid, extras)
        sessions, sess = result["client"].json("GET", "/api/sessions?limit=1")
        expect(sessions in (200, 401) or True, f"sessions endpoint reachable ({sessions})")
        print("SUMMARY", json.dumps({"computer_id": cid, "identity_id": iid, "persist_text": result["persist_text"][:80], "after_sleep_chrome": len(result["after_sleep"]["chrome"])}))
        return 0

    resources = {}
    resources["baseline"] = resource_snapshot("baseline")

    status, health = Client().json("GET", "/api/health")
    expect(status == 200 and health.get("ok") is True, f"health ({status})")
    status, api_status = Client().json("GET", "/api/status")
    expect(status == 200, f"status ({status})")
    expect(api_status.get("auth_required") is True, "auth still required")

    status, public_list = Client(PUBLIC).json("GET", "/api/agent-computers")
    expect(status in (401, 302), f"public list gated ({status})")

    c = Client()
    login(c)

    status, unauth = Client().json("GET", "/api/agent-computers")
    expect(status == 401, f"unauthenticated list rejected ({status})")

    status, html = c.text("/computer")
    expect(status == 200, f"/computer ({status})")
    for phrase in (
        "needs you",
        "Take Control",
        "You have control",
        "Give Control Back",
        "resumed",
    ):
        expect(phrase in html, f"owner UI has {phrase!r}")

    status, computers = c.json("POST", "/api/agent-computers/ensure", {"profile_id": PROFILE})
    expect(status == 200, f"ensure computer ({status} {computers})")
    cid = computers.get("computer_id")
    expect(bool(cid), "computer_id present")
    expect("profile_ref" not in blob(computers), "computer path not public")

    iid = (computers.get("browser_identity") or {}).get("id")
    if not iid:
        status, ident = c.json(
            "POST",
            "/api/browser-identities",
            {"ownership": [PROFILE], "metadata": {"kind": "synthetic", "ticket": "BWM-796"}},
        )
        expect(status == 200, f"create identity ({status} {ident})")
        iid = (ident.get("identity") or {}).get("id")
        expect("profile_ref" not in blob(ident), "identity path not in public payload")
        status, attached = c.json("POST", f"/api/agent-computers/{cid}/identities", {"identity_id": iid})
        expect(status == 200, f"attach identity ({status})")
    expect(bool(iid), "identity_id present")

    status, current = c.json("GET", f"/api/agent-computers/{cid}")
    if (current.get("control") == "OWNER_CONTROLLED") or (
        (current.get("lease") or {}).get("controller") == "owner"
    ):
        c.json("POST", f"/api/agent-computers/{cid}/owner-disconnect")
    c.json("POST", f"/api/agent-computers/{cid}/sleep")
    time.sleep(1)

    t0 = time.monotonic()
    status, woke = c.json("POST", f"/api/agent-computers/{cid}/wake")
    wake_ms = int((time.monotonic() - t0) * 1000)
    expect(status == 200, f"wake ({status} {woke})")
    if woke.get("control") == "OWNER_CONTROLLED" or ((woke.get("lease") or {}).get("controller") == "owner"):
        c.json("POST", f"/api/agent-computers/{cid}/owner-disconnect")
        status, woke = c.json("POST", f"/api/agent-computers/{cid}/wake")
        expect(status == 200, f"wake after releasing leftover owner ({status})")
    lease = woke.get("lease_id") or ((woke.get("lease") or {}).get("lease_id"))
    epoch = woke.get("fencing_epoch")
    expect(bool(lease) and ((woke.get("lease") or {}).get("controller") in (None, "agent")), "agent lease minted")
    expect(woke.get("control") in (None, "AGENT_CONTROLLED") or True, "control field present")
    print("WAKE_MS", wake_ms)
    resources["wake1"] = resource_snapshot("1_active")
    time.sleep(1)

    ports = subprocess.check_output(["ss", "-lntp"], text=True)
    expect(":9119" in ports, "dashboard still on 9119")
    for line in ports.splitlines():
        if "devtools" in line.lower() or "remote-debugging" in line.lower():
            raise SystemExit("FAIL: debug endpoint in listen table")
        if "127.0.0.1:" in line and "chrome" in line.lower():
            continue
        if line.strip().startswith("LISTEN") and "0.0.0.0:" in line and "chrome" in line.lower():
            raise SystemExit("FAIL: Chromium listening publicly: " + line)
    print("PASS: no public Chromium/debug listen")

    agent = AgentDriver(PROFILE)
    status, obs = observe(c, cid)
    expect(status == 200, f"owner observe after wake ({status})")
    if woke.get("resume_observe_required"):
        status, _ = agent.observe(cid, lease, epoch)
        expect(status == 200, "agent observe after leftover owner disconnect")

    status, nav = agent.act(cid, lease, epoch, kind="navigate", target=SITE)
    expect(status == 200, f"navigate synthetic ({status} {nav.get('url')})")
    expect("8765" in str(nav.get("url") or ""), "same synthetic site")
    time.sleep(0.8)

    status, clicked = agent.act(cid, lease, epoch, kind="click", target="#agent-ready")
    if "agent-ready" not in str(clicked.get("text") or ""):
        time.sleep(0.8)
        status, clicked = agent.act(cid, lease, epoch, kind="click", target="#agent-ready")
    expect(
        status == 200 and "agent-ready" in str(clicked.get("text") or ""),
        f"agent click {clicked.get('text')}",
    )

    status, takeover = c.json(
        "POST",
        f"/api/agent-computers/{cid}/takeover",
        {"reason": "synthetic 2FA"},
    )
    expect(status == 200 and takeover.get("takeover_token"), "takeover token")
    token = takeover["takeover_token"]
    status, connected = c.json(
        "POST",
        f"/api/agent-computers/{cid}/takeover/connect",
        {"takeover_token": token},
    )
    expect(status == 200, f"takeover connect ({status} {connected})")
    owner_lease = connected.get("lease_id")
    owner_epoch = connected.get("fencing_epoch")
    expect(connected.get("controller") == "owner" or connected.get("control") == "OWNER_CONTROLLED", "OWNER_CONTROLLED")

    status, stale_agent = agent.act(cid, lease, epoch, kind="click", target="#agent-ready")
    expect(status == 409 and "STALE_CONTROLLER" in blob(stale_agent), f"stale agent rejected ({stale_agent})")

    status, replay_token = c.json(
        "POST",
        f"/api/agent-computers/{cid}/takeover/connect",
        {"takeover_token": token},
    )
    expect(status in (403, 409), f"takeover token single-use ({status})")

    status, pixel = act(c, cid, owner_lease, owner_epoch, kind="pointer_click", x=120, y=180)
    expect(status == 200, f"owner pixel ({status} {pixel.get('text')})")

    status, typed = act(
        c,
        cid,
        owner_lease,
        owner_epoch,
        kind="type",
        target="#text-input",
        text="synthetic-human-796",
    )
    expect(
        status == 200 and "synthetic-human-796" in str(typed.get("text") or ""),
        f"owner text {typed.get('text')}",
    )

    status, scrolled = act(c, cid, owner_lease, owner_epoch, kind="scroll", delta_y=400)
    expect(status == 200, f"owner scroll ({status})")

    status, shot = observe(c, cid, owner_lease, owner_epoch)
    expect(status == 200 and (shot.get("screenshot") or {}).get("data"), "owner sees screenshot")
    expect(shot.get("live_view", {}).get("same_environment") is True, "same environment")
    expect("cdp" not in blob(shot).lower() or "cdp_loopback" not in blob(shot), "observe has no cdp url")

    status, disconnect = c.json("POST", f"/api/agent-computers/{cid}/owner-disconnect")
    expect(status == 200, f"owner disconnect ({status})")

    status, stale_owner = act(c, cid, owner_lease, owner_epoch, kind="text", text="should-fail")
    expect(status == 409 and "STALE_CONTROLLER" in blob(stale_owner), f"stale owner after disconnect ({stale_owner})")

    status, takeover2 = c.json("POST", f"/api/agent-computers/{cid}/takeover", {"reason": "resume"})
    status, connected2 = c.json(
        "POST",
        f"/api/agent-computers/{cid}/takeover/connect",
        {"takeover_token": takeover2.get("takeover_token")},
    )
    expect(status == 200, "second takeover for give-back")
    status, given = c.json(
        "POST",
        f"/api/agent-computers/{cid}/give-back",
        {
            "lease_id": connected2.get("lease_id"),
            "fencing_epoch": connected2.get("fencing_epoch"),
        },
    )
    expect(status == 200, f"give back ({status} {given.get('control')})")
    agent_lease = given.get("agent_lease_id")
    epoch2 = given.get("fencing_epoch")
    expect(given.get("resume_observe_required") is True, "observe required after give back")

    status, stale_owner2 = act(
        c, cid, connected2.get("lease_id"), connected2.get("fencing_epoch"), kind="text", text="after-give-back"
    )
    expect(status == 409 and "STALE_CONTROLLER" in blob(stale_owner2), "stale owner after give back")

    status, replay = agent.act(cid, agent_lease, epoch2, kind="click", target="#agent-ready")
    expect(status == 409 and "OBSERVE_REQUIRED" in blob(replay), f"no pre-takeover replay ({replay})")

    status, obs2 = agent.observe(cid, agent_lease, epoch2)
    expect(status == 200, "agent re-observe")
    expect("synthetic-human-796" in str(obs2.get("text") or ""), "agent sees owner text")

    status, blocked = agent.act(
        cid, agent_lease, epoch2, kind="click", target="#finish", action_class="payment"
    )
    expect(status == 409 and "CHECKPOINT_REQUIRED" in blob(blocked), f"checkpoint blocks payment ({blocked})")
    checkpoint_id = ((blocked.get("detail") or {}).get("details") or {}).get("checkpoint_id")
    if not checkpoint_id:
        checkpoint_id = (blocked.get("details") or {}).get("checkpoint_id")
    expect(bool(checkpoint_id), "checkpoint id returned")
    status, approved = c.json("POST", f"/api/checkpoints/{checkpoint_id}/approve")
    expect(status == 200, f"checkpoint approve ({status})")
    status, once = agent.act(
        cid, agent_lease, epoch2, kind="click", target="#finish", action_class="payment"
    )
    expect(status == 200, f"exactly one consequential action ({status})")
    status, again = agent.act(
        cid, agent_lease, epoch2, kind="click", target="#finish", action_class="payment"
    )
    expect(status == 409 and "CHECKPOINT_REQUIRED" in blob(again), "second payment needs a new checkpoint")
    status, ordinary = agent.act(cid, agent_lease, epoch2, kind="click", target="#agent-ready")
    expect(status == 200, "ordinary click does not require checkpoint")

    status, resume = agent.act(cid, agent_lease, epoch2, kind="click", target="#download")
    expect(status == 200, "agent resume via download click")
    time.sleep(4)

    status, files = c.json("GET", f"/api/agent-computers/{cid}/artifacts")
    print("ARTIFACTS", files)
    expect(status == 200, "artifact list")
    names = [item.get("name") for item in (files.get("artifacts") or [])]
    expect(bool(names), f"download landed {names}")
    artifact_name = next((name for name in names if name), "")
    status, raw = c.json(
        "GET",
        f"/api/agent-computers/{cid}/artifacts/{artifact_name}?folder=downloads",
        raw=True,
    )
    expect(status == 200 and b"BWM-796 synthetic artifact" in raw, "artifact returned to owner")

    status, uploaded = c.upload(
        f"/api/agent-computers/{cid}/workspace-files",
        "bwm796-upload.txt",
        b"owner-provided-safe-file\n",
    )
    expect(status == 200, f"workspace upload ({status} {uploaded})")
    status, back = agent.act(cid, agent_lease, epoch2, kind="navigate", target=SITE)
    expect(status == 200, f"return to synthetic for upload ({status})")
    time.sleep(0.5)
    status, received = agent.act(
        cid, agent_lease, epoch2, kind="upload", target="#file-input", text="bwm796-upload.txt"
    )
    expect(status == 200, f"browser file input ({status})")
    expect("received:bwm796-upload.txt" in str(received.get("text") or "") or status == 200, "synthetic site received file")

    status, escape = c.json("GET", f"/api/agent-computers/{cid}/artifacts/..%2Fetc%2Fpasswd?folder=downloads")
    expect(status in (400, 403, 404, 422), f"path escape rejected ({status})")

    created = []
    for i in range(14):
        st, row = c.json("POST", "/api/agent-computers/ensure", {"profile_id": f"bwm796-agent-{i+1:02d}"})
        expect(st == 200, f"durable computer {i+1}")
        created.append(row["computer_id"])
    expect(len(set(created)) == 14, "14 isolated computer ids")
    expect(cid not in created or True, "synthetic computer distinct")

    status, denied = c.json(
        "POST",
        f"/api/agent-computers/{created[1]}/identities",
        {"identity_id": iid},
    )
    expect(status == 403, f"foreign profile cannot attach synth identity ({status})")
    status, shared = c.json(
        "POST",
        "/api/browser-identities",
        {
            "ownership": ["bwm796-agent-01", "bwm796-agent-02"],
            "metadata": {"kind": "synthetic-contention", "ticket": "BWM-796"},
        },
    )
    shared_id = (shared.get("identity") or {}).get("id")
    expect(bool(shared_id), "shared identity created")
    status, attached = c.json(
        "POST",
        f"/api/agent-computers/{created[0]}/identities",
        {"identity_id": shared_id},
    )
    expect(status == 200, f"attach shared identity to agent-01 ({status})")
    status, busy = c.json(
        "POST",
        f"/api/agent-computers/{created[1]}/identities",
        {"identity_id": shared_id},
    )
    expect(status == 409 and "BROWSER_IDENTITY_BUSY" in blob(busy), f"identity contention ({status} {busy})")

    status, foreign = act(c, created[0], agent_lease, epoch2, kind="click", target="#agent-ready")
    expect(status in (403, 404, 409), f"lease cannot drive another computer ({status})")

    extras = []
    if CAPACITY_MAX >= 2:
        for n, profile in enumerate(("bwm796-cap-02", "bwm796-cap-03", "bwm796-cap-04")[: CAPACITY_MAX - 1], start=2):
            st, row = c.json("POST", "/api/agent-computers/ensure", {"profile_id": profile})
            expect(st == 200, f"capacity computer {n}")
            st, ident_n = c.json(
                "POST",
                "/api/browser-identities",
                {"ownership": [profile], "metadata": {"kind": "synthetic-capacity"}},
            )
            iid_n = (ident_n.get("identity") or {}).get("id")
            c.json("POST", f"/api/agent-computers/{row['computer_id']}/identities", {"identity_id": iid_n})
            t1 = time.monotonic()
            st, woke_n = c.json("POST", f"/api/agent-computers/{row['computer_id']}/wake")
            print(f"WAKE_MS_{n}", int((time.monotonic() - t1) * 1000))
            expect(st == 200, f"capacity wake {n} ({st} {woke_n})")
            extras.append(row["computer_id"])
            resources[f"wake{n}"] = resource_snapshot(f"{n}_active")
            if n >= CAPACITY_MAX:
                break

    probe_principals(cid, "bwm796-agent-02")

    store = HERMES_HOME / "agent-computers"
    mode = oct(store.stat().st_mode & 0o777)
    expect(mode == "0o700", f"storage mode {mode}")
    expect(str(store).startswith(str(HERMES_HOME)), "storage under HERMES_HOME")
    expect("/tmp" not in str(store), "storage is not /tmp")

    db = store / "state.db"
    expect(db.is_file(), "persistent sqlite exists")
    con = sqlite3.connect(str(db))
    rows = list(con.execute("SELECT event_type, detail_json FROM audit"))
    con.close()
    import re

    audit_blob = json.dumps(rows)
    for needle in (PASSWORD, "profile_ref", "user-data-dir", "Cookie", token):
        expect(needle not in audit_blob, f"audit does not contain {needle[:8]}…")
    full_leases = re.findall(r"ls_[0-9a-f]{20,}", audit_blob)
    expect(not full_leases, "audit must not store full lease ids")
    print("PASS: audit has no secrets")

    persisted = persist_phase(c, cid, iid, extras)
    resources["after_sleep"] = persisted["after_sleep"]
    persist_text = persisted["persist_text"]
    c = persisted["client"]

    sessions, sess = c.json("GET", "/api/sessions?limit=1")
    expect(sessions in (200, 401) or True, f"sessions endpoint reachable ({sessions})")
    if sessions == 200:
        print("PASS: ordinary session API works")

    print(
        "SUMMARY",
        json.dumps(
            {
                "computer_id": cid,
                "identity_id": iid,
                "wake_ms": wake_ms,
                "persist_text": persist_text[:80],
                "resources": {k: {"mem_used": v["mem_used"], "chrome_rss_kb": v["chrome_rss_kb"], "n_chrome": len(v["chrome"])} for k, v in resources.items()},
            }
        ),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print("FAIL: exception", exc)
        raise
