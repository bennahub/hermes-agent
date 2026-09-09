import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from gateway.agent_computer.native_desktop import NativeDesktopRuntime, NativeHandle
from gateway.agent_computer.errors import NativeOperationError


@pytest.mark.parametrize("reported,expected", [("TimeoutError", "TimeoutError"), ("secret-url-or-token", "worker")])
def test_failed_rpc_stops_once_never_replays_and_bounds_diagnostics(monkeypatch, reported, expected):
    # Short actual AF_UNIX path also works under macOS's 104-byte sockaddr limit.
    with tempfile.TemporaryDirectory(prefix="hn-") as folder:
        path = str(Path(folder) / "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        requests = []
        def worker():
            with listener:
                conn, _ = listener.accept()
                with conn:
                    requests.append(conn.recv(4096))
                    conn.sendall(json.dumps({"ok": False, "failure_kind": reported}).encode() + b"\n")
        thread = threading.Thread(target=worker)
        thread.start()
        runtime = NativeDesktopRuntime()
        handle = NativeHandle("qa", None, folder, metadata={"socket": path, "token": "qa-token"})
        stops = []
        monkeypatch.setattr(runtime, "alive", lambda _: True)
        monkeypatch.setattr(runtime, "_force_stop", stops.append)
        with pytest.raises(NativeOperationError) as raised:
            runtime._rpc(handle, "nav", action="open", url="https://example.com")
        thread.join(2)
        assert not thread.is_alive()
        assert len(requests) == len(stops) == 1
        assert raised.value.details == {"phase": "nav", "failure_kind": expected}
        assert "qa-token" not in str(raised.value)
        assert "secret-url-or-token" not in str(raised.value.details)
