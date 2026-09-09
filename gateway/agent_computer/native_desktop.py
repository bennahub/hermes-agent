"""Identity-owned headed Chrome, private X display and acknowledged native input.

The worker has its own DISPLAY/XAUTHORITY and survives client disconnects. Only
frames and normalized input cross the existing Hermes public stream. Chrome is
never launched with a debugger, headless mode or a disabled sandbox.
"""
from __future__ import annotations

import json
import hashlib
import re
import stat
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .adapter import RuntimeHandle, _now, private_dir
from .errors import AgentComputerError, NativeOperationError
from .models import Observation

NATIVE_LOCATION = {"url": "", "title": "", "origin": "", "https": False,
                   "scheme": "", "source": "browser_chrome", "verified": False}
MAX_RPC_BYTES = 16 * 1024 * 1024


def native_chrome_argv(binary: str, profile: str) -> list[str]:
    return [binary, f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--window-size=1440,900",
            "--restore-last-session"]


def native_destination_url(url: str) -> str:
    """HTTP(S) destination encoded for the stable native ASCII keyboard."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AgentComputerError("native navigation requires an HTTP(S) destination")
    host = parts.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    if parts.port is not None:
        host += ":" + str(parts.port)
    if parts.username is not None:
        user = quote(parts.username, safe="%")
        if parts.password is not None:
            user += ":" + quote(parts.password, safe="%")
        host = user + "@" + host
    return urlunsplit((parts.scheme, host, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="%/?@!$&'()*+,;=:-._~"),
                       quote(parts.fragment, safe="%/?@!$&'()*+,;=:-._~")))


def merge_download_preferences(profile: Path, downloads: Path):
    """Called only after proving no browser owns this managed profile."""
    default = private_dir(profile / "Default")
    prefs_path = default / "Preferences"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    if not isinstance(prefs, dict):
        raise AgentComputerError("invalid native Chrome preferences")
    download = prefs.setdefault("download", {})
    if not isinstance(download, dict):
        raise AgentComputerError("invalid native Chrome download preferences")
    download.update(default_directory=str(downloads.resolve()), prompt_for_download=False,
                    directory_upgrade=True)
    session = prefs.setdefault("session", {})
    if not isinstance(session, dict):
        raise AgentComputerError("invalid native Chrome session preferences")
    session["restore_on_startup"] = 1
    for key, directory in (("selectfile", downloads.parent / "uploads"), ("savefile", downloads)):
        section = prefs.setdefault(key, {})
        if not isinstance(section, dict):
            raise AgentComputerError("invalid native Chrome file chooser preferences")
        field = "last_directory" if key == "selectfile" else "default_directory"
        section[field] = str(private_dir(directory).resolve())
    _write_private(prefs_path, prefs)


def _write_private(path: Path, value: dict):
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(value, f)
    os.replace(tmp, path)


def process_identity(pid: int) -> dict:
    import psutil
    p = psutil.Process(pid)
    return {"pid": p.pid, "started": p.create_time()}


def process_matches(identity: dict) -> bool:
    import psutil
    try:
        p = psutil.Process(int(identity["pid"]))
        return p.create_time() == float(identity["started"]) and p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


def _stop_process(identity: dict):
    """Never signal a reused PID; wait for termination before returning."""
    import psutil
    if not process_matches(identity):
        return
    try:
        parent = psutil.Process(identity["pid"])
        family = [parent, *parent.children(recursive=True)]
        # Let Chrome coordinate profile/session flushing while its cookie and
        # network helpers still live; only reap leftovers after bounded exit.
        parent.terminate()
        try:
            parent.wait(timeout=4)
        except psutil.TimeoutExpired:
            pass
        _, alive = psutil.wait_procs(family, timeout=1)
        for p in alive:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=3)
        if alive:
            raise RuntimeError("native runtime could not be stopped")
    except psutil.NoSuchProcess:
        return


def _read_message(conn: socket.socket) -> dict:
    parts, size = [], 0
    while True:
        chunk = conn.recv(min(65536, MAX_RPC_BYTES + 1 - size))
        if not chunk:
            raise ConnectionError("private native channel closed")
        size += len(chunk)
        if size > MAX_RPC_BYTES:
            raise ValueError("native message too large")
        parts.append(chunk)
        if b"\n" in chunk:
            raw = b"".join(parts)
            line, tail = raw.split(b"\n", 1)
            if tail:
                raise ValueError("one native command per connection")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("invalid native message")
            return value


def _read_display(fd: int, timeout: float = 8) -> str:
    """Xserver writes the display number and newline in separate writes."""
    import select
    deadline = time.monotonic() + timeout
    reply = b""
    while b"\n" not in reply:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise RuntimeError("private display startup timed out")
        chunk = os.read(fd, 32 - len(reply))
        if not chunk:
            raise RuntimeError("private display closed before allocation completed")
        reply += chunk
        if len(reply) >= 32:
            raise RuntimeError("invalid display allocation")
    number, tail = reply.split(b"\n", 1)
    if tail or not number.isdigit():
        raise RuntimeError("invalid display allocation")
    return ":" + number.decode("ascii")


def user_manager_env() -> dict[str, str]:
    """Address this UID's manager without mutating the caller's environment."""
    directory = Path('/run/user') / str(os.getuid())
    try:
        info, bus = directory.stat(), (directory / 'bus').stat()
        if (directory.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                or not stat.S_ISSOCK(bus.st_mode) or bus.st_uid != os.getuid()):
            raise OSError('invalid private user manager endpoint')
    except OSError:
        raise AgentComputerError('native desktop requires the service user systemd manager and lingering') from None
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL') if k in os.environ}
    env.update(XDG_RUNTIME_DIR=str(directory), DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(directory / 'bus'))
    return env


def user_unit_state(unit: str) -> dict[str, str]:
    result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
                             '--property=MainPID,InvocationID,ActiveState,ControlGroup,LoadState,Job'],
                            env=user_manager_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=5)
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and state.get('LoadState') != 'not-found':
        raise NativeUnitOwnershipError('native user manager state is unavailable')
    if not state:
        raise NativeUnitOwnershipError('native user manager state is unavailable')
    return state


def stop_owned_user_unit(metadata: dict) -> None:
    unit, invocation = metadata.get('systemd_unit', ''), metadata.get('systemd_invocation_id', '')
    if not (re.fullmatch(r'hermes-native-[0-9a-f]{16}-[0-9a-f]{12}\.service', unit)
            and re.fullmatch(r'[0-9a-f]{32}', invocation)):
        return
    state = user_unit_state(unit)
    if state.get('InvocationID') != invocation:
        return  # A collected/reused unit name is not our recorded invocation.
    subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit], env=user_manager_env(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)


def native_user_unit_argv(unit: str, config_path: Path, source_root: Path) -> list[str]:
    return ['/usr/bin/systemd-run', '--user', '--quiet', '--collect', '--expand-environment=no',
            '--unit=' + unit, '--property=Type=exec', '--property=KillMode=control-group',
            '--property=Restart=no', '--property=TimeoutStopSec=15s', '--property=UMask=0077',
            '--property=StandardInput=null', '--property=StandardOutput=null', '--property=StandardError=null',
            '--working-directory=' + str(source_root), '--setenv=PYTHONDONTWRITEBYTECODE=1',
            '--setenv=LANG=C.UTF-8', '--setenv=LC_ALL=C.UTF-8',
            sys.executable, '-m', 'gateway.agent_computer.native_desktop', '--worker', str(config_path)]


class NativeUnitOwnershipError(AgentComputerError):
    """An uncertain manager result must not authorize unrelated cleanup."""


def start_native_user_worker(unit: str, config_path: Path, source_root: Path) -> tuple[dict, dict]:
    try:
        subprocess.run(native_user_unit_argv(unit, config_path, source_root), env=user_manager_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        # The manager may have accepted the job before the client timed out.
        # Reconcile its actual invocation instead of abandoning a live worker.
        pass
    try:
        state = user_unit_state(unit)
        if (state.get('LoadState') == 'not-found'
                or state.get('ActiveState') in ('inactive', 'failed') and state.get('Job') == '0'):
            return {}, {}
        import psutil
        process = psutil.Process(int(state.get('MainPID') or 0))
        invocation = state.get('InvocationID', '')
        if (not re.fullmatch(r'[0-9a-f]{32}', invocation)
                or process.cmdline()[-4:] != ['-m', 'gateway.agent_computer.native_desktop', '--worker', str(config_path)]
                or Path(process.exe()).resolve() != Path(sys.executable).resolve()
                or process.environ().get('INVOCATION_ID') != invocation):
            raise ValueError('worker invocation mismatch')
        worker = {'pid': process.pid, 'started': process.create_time()}
        return worker, {'systemd_unit': unit, 'systemd_invocation_id': invocation}
    except Exception:
        raise NativeUnitOwnershipError('native worker startup ownership could not be verified; configuration retained') from None


@dataclass
class NativeHandle(RuntimeHandle):
    metadata: dict = field(default_factory=dict)
    rpc_lock: Any = field(default_factory=threading.RLock, repr=False)


class NativeDesktopRuntime:
    backend = "native_desktop"
    native_desktop = True

    def __init__(self, *, executable="/opt/google/chrome/chrome", window_manager="/usr/bin/openbox",
                 libxdo_path="", xvfb="/usr/bin/Xvfb", xauth="/usr/bin/xauth",
                 setxkbmap="/usr/bin/setxkbmap", worker_launcher="systemd_user", rpc_timeout=10):
        if worker_launcher not in ("systemd_user", "process"):
            raise ValueError("unsupported native worker launcher")
        self.options = {"executable": executable, "window_manager": window_manager,
                        "libxdo_path": libxdo_path, "xvfb": xvfb, "xauth": xauth, "setxkbmap": setxkbmap, "worker_launcher": worker_launcher}
        self.rpc_timeout = float(rpc_timeout)

    @staticmethod
    def _record(computer):
        return Path(computer.persistence_ref) / "native-runtime.json"

    def _handle_from(self, computer, identity, metadata):
        profile = str(Path(identity.profile_ref if identity else computer.persistence_ref).resolve())
        if metadata.get("computer_id") != computer.id or metadata.get("identity_id") != (identity.id if identity else None) or metadata.get("profile") != profile:
            return None
        return NativeHandle(computer_id=computer.id, identity_id=identity.id if identity else None,
                            user_data_dir=profile, process_id=(metadata.get("browser") or {}).get("pid"),
                            backend=self.backend, headed_same_host=True,
                            workspace_root=str(Path(computer.persistence_ref) / "workspace"),
                            viewport_width=1440, viewport_height=900, metadata=metadata)

    def _foreign_handle(self, computer, identity, metadata=None):
        import psutil
        profile = str(Path(identity.profile_ref if identity else computer.persistence_ref).resolve())
        for proc in psutil.process_iter(["cmdline", "exe"]):
            try:
                args = proc.info["cmdline"] or []
                if Path(proc.info["exe"] or "").name not in ("chrome", "chromium", "chromium-browser"):
                    continue
                if f"--user-data-dir={profile}" in args and not any(a.startswith("--type=") for a in args):
                    data = dict(metadata or {})
                    data.update(computer_id=computer.id, identity_id=identity.id if identity else None,
                                profile=profile, browser=process_identity(proc.pid), foreign=True)
                    return self._handle_from(computer, identity, data)
            except psutil.Error:
                continue
        return None

    def attach(self, computer, identity):
        try:
            metadata = json.loads(self._record(computer).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._foreign_handle(computer, identity)
        handle = self._handle_from(computer, identity, metadata)
        if handle and self.alive(handle):
            return handle
        if handle and any(process_matches(metadata.get(k) or {}) for k in ("worker", "browser", "display", "window_manager")):
            # This record binds our own native process group to this identity.
            # Partial helper failure must not be mistaken for a legacy browser.
            self._force_stop(handle)
        return self._foreign_handle(computer, identity)

    def alive(self, handle):
        if handle.metadata.get("foreign"):
            return process_matches(handle.metadata.get("browser") or {})
        required = ["worker", "browser", "display"]
        if handle.metadata.get("window_manager"):
            required.append("window_manager")
        return all(process_matches(handle.metadata.get(k) or {}) for k in required)

    def wake(self, computer, identity):
        if sys.platform != "linux":
            raise AgentComputerError("native desktop requires Linux with a private X display")
        existing = self.attach(computer, identity)
        if existing:
            return existing
        for key in ("executable", "xvfb", "xauth", "window_manager", "setxkbmap"):
            value = self.options[key]
            if not value or not Path(value).is_absolute() or not os.access(value, os.X_OK):
                raise AgentComputerError(f"native desktop prerequisite unavailable: {key}")
        if self.options["worker_launcher"] == "systemd_user":
            user_manager_env()  # Fail before profile changes; never fall back to caller cgroup.
        profile = str(Path(identity.profile_ref if identity else computer.persistence_ref).resolve())
        # An old runtime may still own the profile. Never spawn a second browser
        # on it or remove its SingletonLock during an operator mode transition.
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            args = p.info["cmdline"] or []
            if f"--user-data-dir={profile}" in args and not any(a.startswith("--type=") for a in args):
                raise AgentComputerError("Suspend the existing browser before changing its runtime")
        private_dir(Path(profile))
        downloads = private_dir(Path(computer.persistence_ref) / "workspace" / "downloads")
        merge_download_preferences(Path(profile), downloads)
        private_dir(Path(computer.persistence_ref))
        folder = Path(tempfile.mkdtemp(prefix="hermes-native-", dir="/tmp"))
        os.chmod(folder, 0o700)
        config = {**self.options, "computer_id": computer.id, "identity_id": identity.id if identity else None,
                  "workspace": str(Path(computer.persistence_ref) / "workspace"),
                  "profile": profile, "token": secrets.token_hex(32), "socket": str(folder / "ipc.sock"),
                  "record": str(self._record(computer)), "folder": str(folder)}
        config_path = folder / "worker.json"
        managed = self.options["worker_launcher"] == "systemd_user"
        if managed:
            config["systemd_unit"] = ('hermes-native-' + hashlib.sha256(computer.id.encode()).hexdigest()[:16]
                                      + '-' + secrets.token_hex(6) + '.service')
        _write_private(config_path, config)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        worker, unit_record = {}, {}
        try:
            if managed:
                worker, unit_record = start_native_user_worker(config["systemd_unit"], config_path, Path(__file__).resolve().parents[2])
            else:
                # Explicit test/non-service-host mode retains the caller's
                # namespace; it is never selected as a production fallback.
                proc = subprocess.Popen([sys.executable, "-m", "gateway.agent_computer.native_desktop", "--worker", str(config_path)],
                                        cwd=str(Path(__file__).resolve().parents[2]), env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                worker = process_identity(proc.pid)
            deadline = time.monotonic() + 25
            while process_matches(worker) and time.monotonic() < deadline:
                handle = self.attach(computer, identity)
                if (handle and handle.metadata.get("worker") == worker
                        and (not managed or all(handle.metadata.get(k) == v for k, v in unit_record.items()))):
                    self._rpc(handle, "ping")
                    return handle
                time.sleep(0.1)
        except NativeUnitOwnershipError:
            raise  # Do not delete a possibly active invocation's private files.
        except Exception:
            pass
        try:
            stop_owned_user_unit(unit_record)
        finally:
            _stop_process(worker)
            shutil.rmtree(folder, ignore_errors=True)
        raise AgentComputerError("native desktop startup failed; check installed display, input and sandbox prerequisites")

    def _rpc(self, handle, command, **params):
        # Benign validation errors must not enter the worker or stop a session.
        if command == "text":
            text = params.get("text", "")
            if not isinstance(text, str) or len(text) > 8192 or "\0" in text:
                raise AgentComputerError("native text must contain at most 8192 characters without NUL")
            try:
                text.encode("utf-8")
            except UnicodeError:
                raise AgentComputerError("native text must be valid Unicode") from None
        if command in ("key", "chord"):
            from .native_x11 import NATIVE_KEY_NAMES
            key = params.get("key", "")
            chars = handle.metadata.get("key_characters", "".join(chr(i) for i in range(32, 127)))
            if (not isinstance(key, str) or not (key in NATIVE_KEY_NAMES or len(key) == 1 and key in chars)
                    or command == "key" and params.get("phase", "down") not in ("down", "up")):
                raise AgentComputerError("unsupported native key; use text or paste for other characters")
        with handle.rpc_lock:
            if handle.metadata.get("foreign"):
                raise AgentComputerError("Suspend the existing browser before changing its runtime")
            if not self.alive(handle):
                self._force_stop(handle)
                raise AgentComputerError("native desktop is not running")
            failure_kind = "transport"
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(self.rpc_timeout)
                    conn.connect(handle.metadata["socket"])
                    conn.sendall(json.dumps({"token": handle.metadata["token"], "command": command,
                                            "deadline": time.monotonic() + self.rpc_timeout - 0.25,
                                            "params": params}).encode() + b"\n")
                    result = _read_message(conn)
                if result.get("ok") is not True:
                    reported = result.get("failure_kind")
                    failure_kind = reported if reported in {"TimeoutError", "ValueError", "RuntimeError", "OSError"} else "worker"
                    raise RuntimeError("native command failed")
                return result.get("result") or {}
            except Exception:
                # No queued or uncertain input may survive a returned failure.
                self._force_stop(handle)
                raise NativeOperationError("native desktop operation failed; runtime stopped",
                                           details={"phase": command, "failure_kind": failure_kind}) from None

    def capture_frame(self, handle):
        return self._rpc(handle, "capture")

    def observe(self, handle):
        shot = self.capture_frame(handle)
        handle.screenshot_width = handle.screenshot_viewport_width = shot["width"]
        handle.screenshot_height = handle.screenshot_viewport_height = shot["height"]
        return Observation(url="", title="", text="", fencing_epoch=0, controller="", observed_at=_now(),
                           screenshot_b64=shot["data"], screenshot_mime="image/jpeg",
                           screenshot_width=shot["width"], screenshot_height=shot["height"],
                           viewport_width=shot["width"], viewport_height=shot["height"])

    def current_location(self, handle):
        return dict(NATIVE_LOCATION)

    def stream_pointer(self, handle, **event):
        self._rpc(handle, "pointer", **event)

    def stream_wheel(self, handle, **event):
        self._rpc(handle, "wheel", **event)

    def stream_key(self, handle, **event):
        self._rpc(handle, "key", **event)

    def stream_nav(self, handle, action, url=""):
        if action == "open":
            url = native_destination_url(url)
        self._rpc(handle, "nav", action=action, url=url)
        return dict(NATIVE_LOCATION)

    def release_inputs(self, handle):
        if self.alive(handle):
            self._rpc(handle, "release")
        else:
            self._force_stop(handle)

    def inputs_stopped(self, handle):
        return not any(process_matches(handle.metadata.get(k) or {})
                       for k in ("worker", "browser", "display", "window_manager"))

    def act(self, handle, *, kind, target="", text="", x=None, y=None, key="", code="", delta_x=0, delta_y=0, **_):
        if target and kind != "navigate":
            raise AgentComputerError("DOM selectors are unavailable on the native desktop; use screenshot coordinates")
        if kind == "navigate":
            self.stream_nav(handle, "open", target)
        elif kind in ("text", "type"):
            self._rpc(handle, "text", text=text)
        elif kind in ("pointer_click", "click", "pointer_move"):
            if x is None or y is None:
                raise AgentComputerError("native pointer actions require screenshot coordinates")
            self.stream_pointer(handle, phase="move" if kind == "pointer_move" else "click", x=x, y=y)
        elif kind == "key":
            self._rpc(handle, "chord", key=key or code)
        elif kind == "scroll":
            self.stream_wheel(handle, x=x or 0, y=y or 0, delta_x=delta_x, delta_y=delta_y)
        else:
            raise AgentComputerError("unsupported native desktop action")
        return self.observe(handle)

    def sleep(self, handle):
        if not handle.metadata.get("foreign") and self.alive(handle):
            try:
                self._rpc(handle, "shutdown")
            except AgentComputerError:
                # The failed RPC already performed immediate verified cleanup.
                pass
        self._force_stop(handle)

    def _force_stop(self, handle):
        # Verify every stored PID/start pair. Helpers remain separately stoppable
        # even if the worker already exited and its children were reparented.
        metadata = handle.metadata
        try:
            stop_owned_user_unit(metadata)
        except (AgentComputerError, subprocess.SubprocessError):
            pass  # Verified PID/start cleanup remains available if manager died.
        for name in ("browser", "window_manager", "worker", "display"):
            _stop_process(metadata.get(name) or {})
        folder = Path(metadata.get("folder", ""))
        if folder.name.startswith("hermes-native-") and folder.is_dir():
            shutil.rmtree(folder)


def _worker(config_path):
    """One serial acknowledged command stream, confined to one private display."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if config.get("worker_launcher") == "systemd_user":
        invocation = os.environ.get("INVOCATION_ID", "")
        if not re.fullmatch(r'[0-9a-f]{32}', invocation):
            raise RuntimeError("native worker requires its own systemd invocation")
        config["systemd_invocation_id"] = invocation
    children = []
    native = None
    server = None
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
        raise SystemExit()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Xvfb chooses an unused display atomically; no process-global DISPLAY
        # mutation occurs in the Hermes gateway/serve process.
        read_fd, write_fd = os.pipe()
        auth = Path(config["folder"]) / "Xauthority"
        auth.touch(mode=0o600)
        cookie = secrets.token_hex(16)
        # Xvfb loads the cookie before exposing the display. The server accepts
        # its auth file's cookie independently of the client's display entry.
        subprocess.run([config["xauth"], "-f", str(auth)], input=f"add :0 . {cookie}\n",
                       text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3)
        display_proc = subprocess.Popen([config["xvfb"], "-displayfd", str(write_fd), "-screen", "0", "1440x900x24",
                                         "-noreset", "-nolisten", "tcp", "-auth", str(auth)], pass_fds=(write_fd,),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append((display_proc, process_identity(display_proc.pid)))
        os.close(write_fd)
        try:
            display = _read_display(read_fd)
        finally:
            os.close(read_fd)
        # Populate the authorization file before any browser/input connection.
        subprocess.run([config["xauth"], "-f", str(auth)],
                       input=f"add {display} . {cookie}\n", text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3)
        os.environ.update(DISPLAY=display, XAUTHORITY=str(auth))
        for key, suffix in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
            os.environ[key] = str(private_dir(Path(config["folder"]) / suffix))
        os.chdir(config["workspace"])

        import locale
        locale.setlocale(locale.LC_ALL, "C.UTF-8")
        # Stable, standard mappings must exist before libxdo caches them and
        # before Chrome consumes any keyboard events. This affects only our X.
        subprocess.run([config["setxkbmap"], "-display", display, "-layout", "us,ara"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=3)
        from .native_x11 import NativeX11
        native = NativeX11(display, config["libxdo_path"])
        wm = None
        if config["window_manager"]:
            wm_config = Path(config["folder"]) / "openbox.xml"
            with os.fdopen(os.open(wm_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                f.write('<openbox_config xmlns="http://openbox.org/3.4/rc"><focus>'
                        '<focusNew>yes</focusNew><followMouse>no</followMouse>'
                        '<focusLast>yes</focusLast></focus></openbox_config>')
            wm = subprocess.Popen([config["window_manager"], "--sm-disable", "--config-file", str(wm_config)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append((wm, process_identity(wm.pid)))
        chrome = subprocess.Popen(native_chrome_argv(config["executable"], config["profile"]),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append((chrome, process_identity(chrome.pid)))
        ready_deadline = time.monotonic() + 12
        while not native.focus_browser(chrome.pid):
            if chrome.poll() is not None or time.monotonic() >= ready_deadline:
                raise RuntimeError("native browser did not become ready")
            time.sleep(0.05)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(config["socket"])
        os.chmod(config["socket"], 0o600)
        server.listen(4)
        server.settimeout(0.02)
        metadata = {**config, "x_display": display, "key_characters": native.key_characters, "worker": process_identity(os.getpid()), "browser": process_identity(chrome.pid),
                    "display": process_identity(display_proc.pid), "window_manager": process_identity(wm.pid) if wm else {}}
        _write_private(Path(config["record"]), metadata)
        while (not stopping and chrome.poll() is None and display_proc.poll() is None
               and (wm is None or wm.poll() is None)):
            native.clipboard.pump()
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    request = _read_message(conn)
                    if not secrets.compare_digest(str(request.get("token", "")), config["token"]):
                        raise ValueError("private channel authorization failed")
                    deadline = float(request["deadline"])
                    if time.monotonic() >= deadline:
                        raise TimeoutError("command expired")
                    cmd, p = request["command"], request.get("params") or {}
                    result = {}
                    if cmd == "ping":
                        pass
                    elif cmd == "capture":
                        result = native.capture()
                    elif cmd == "release":
                        native.release_inputs()
                    elif cmd == "shutdown":
                        # Chrome's internal quit action calls AttemptExit and
                        # preserves all session windows. Use a real normal-role
                        # browser window, since a popup may lack an omnibox.
                        while not native.focus_browser(chrome.pid):
                            if chrome.poll() is not None or time.monotonic() >= deadline:
                                raise TimeoutError("native browser quit focus unavailable")
                            native.clipboard.pump()
                            time.sleep(.02)
                        native.destination("chrome://quit/", deadline)
                        while chrome.poll() is None and time.monotonic() < deadline:
                            native.clipboard.pump()
                            time.sleep(.02)
                        if chrome.poll() is None:
                            raise TimeoutError("native browser graceful close expired")
                    elif cmd == "text":
                        result = {"delivered": native.text(str(p.get("text", "")), deadline)}
                    elif cmd in ("pointer", "wheel", "key", "chord"):
                        getattr(native, cmd)(**p)
                    elif cmd == "nav":
                        action = p.get("action")
                        if action == "open":
                            url = native_destination_url(str(p.get("url", "")))
                            # A focused popup can expose a read-only address;
                            # route destination entry through an owned omnibox.
                            while not native.focus_browser(chrome.pid):
                                if chrome.poll() is not None or time.monotonic() >= deadline:
                                    raise TimeoutError("native browser navigation focus unavailable")
                                native.clipboard.pump()
                                time.sleep(.02)
                            native.destination(url, deadline)
                        elif action in ("back", "forward", "reload"):
                            native.chord({"back": "ArrowLeft", "forward": "ArrowRight", "reload": "r"}[action], 2 if action == "reload" else 1)
                        else:
                            raise ValueError("unsupported native navigation")
                    else:
                        raise ValueError("unsupported native command")
                    native.sync()
                    conn.sendall(json.dumps({"ok": True, "result": result}).encode() + b"\n")
                except Exception as exc:
                    # Once execution is uncertain, fail closed and stop this
                    # runtime; no subsequent queued input can be accepted.
                    try:
                        # Bounded exception category only: never leak a URL,
                        # text entry, socket token, clipboard, or stack trace.
                        category = type(exc).__name__
                        if category not in {"TimeoutError", "ValueError", "RuntimeError", "OSError"}:
                            category = "worker"
                        conn.sendall(json.dumps({"ok": False, "failure_kind": category}).encode() + b"\n")
                    except OSError:
                        pass
                    break
    finally:
        if server:
            server.close()
        if native:
            try:
                native.close()
            except Exception:
                pass
        for child, identity in reversed(children):
            try:
                _stop_process(identity)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2])
