"""Behavioral regressions found during independent C0 revalidation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import multiprocessing
import time

import pytest

from gateway.agent_computer.adapter import InMemoryRuntime, RuntimeHandle
from gateway.agent_computer.errors import ConflictError
from gateway.agent_computer.keys import cdp_key_params
from gateway.agent_computer.location import public_location
from gateway.agent_computer.models import OWNER_PRINCIPAL, Lifecycle, agent_principal
from gateway.agent_computer.service import AgentComputerService
from gateway.agent_computer.store import AgentComputerStore
from gateway.agent_computer.stream import FrameBroker, OwnerStreamSession


class FileRuntime(InMemoryRuntime):
    """An actual file-backed runtime marker, visible across spawned processes."""
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def wake(self, computer, identity):
        # Widen the admission race; synchronization must protect the full launch.
        time.sleep(0.15)
        handle = RuntimeHandle(computer_id=computer.id, identity_id=None, user_data_dir=str(self.root))
        (self.root / computer.id).touch()
        return handle

    def attach(self, computer, identity):
        handle = RuntimeHandle(computer_id=computer.id, identity_id=None, user_data_dir=str(self.root))
        return handle if self.alive(handle) else None

    def alive(self, handle):
        return (self.root / handle.computer_id).exists()

    def sleep(self, handle):
        (self.root / handle.computer_id).unlink(missing_ok=True)


def _capacity_worker(root, computer_id, profile, barrier, results):
    svc = AgentComputerService(AgentComputerStore(Path(root) / 'state.db'), FileRuntime(root), data_root=root, max_active_computers=1)
    barrier.wait(timeout=10)
    try:
        svc.wake(computer_id, agent_principal(profile))
        results.put('started')
    except ConflictError:
        results.put('limited')
    finally:
        svc.store.close()


def test_capacity_is_serialized_across_processes(tmp_path):
    svc = AgentComputerService(AgentComputerStore(tmp_path / 'state.db'), FileRuntime(tmp_path), data_root=tmp_path)
    computers = [svc.ensure_computer(p) for p in ('first', 'second')]
    ctx = multiprocessing.get_context('spawn')
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=_capacity_worker, args=(str(tmp_path), c.id, c.agent_profile_id, barrier, results)) for c in computers]
    try:
        for p in procs: p.start()
        outcomes = [results.get(timeout=20) for _ in procs]
        for p in procs:
            p.join(timeout=10)
            assert p.exitcode == 0
        assert sorted(outcomes) == ['limited', 'started']
    finally:
        for p in procs:
            if p.is_alive(): p.terminate(); p.join(timeout=5)


def test_capacity_reconciles_stale_records_and_counts_attached_runtime(tmp_path):
    store = AgentComputerStore(tmp_path / 'state.db')
    svc = AgentComputerService(store, FileRuntime(tmp_path), data_root=tmp_path, max_active_computers=1)
    stale = svc.ensure_computer('stale')
    stale.lifecycle = Lifecycle.READY
    store.upsert_computer(stale)
    live = svc.ensure_computer('live')
    svc.wake(live.id, agent_principal('live'))
    assert store.get_computer(stale.id).lifecycle == Lifecycle.SLEEPING
    fresh = AgentComputerService(AgentComputerStore(tmp_path / 'state.db'), FileRuntime(tmp_path), data_root=tmp_path, max_active_computers=1)
    with pytest.raises(ConflictError): fresh.wake(stale.id, agent_principal('stale'))
    # Restarting the control service must not duplicate an already-running browser.
    fresh.wake(live.id, agent_principal('live'))
    fresh.sleep(live.id, OWNER_PRINCIPAL)
    fresh.wake(stale.id, agent_principal('stale'))


def test_agent_cannot_suspend_owner_controlled_runtime(tmp_path):
    svc = AgentComputerService(AgentComputerStore(tmp_path / 'state.db'), InMemoryRuntime(), data_root=tmp_path)
    computer = svc.ensure_computer('agent')
    svc.wake(computer.id, agent_principal('agent'))
    svc.request_takeover(computer.id, OWNER_PRINCIPAL)
    with pytest.raises(ConflictError): svc.sleep(computer.id, agent_principal('agent'))
    assert svc._runtime_alive(svc._handles[computer.id])


def test_frame_location_is_copied_and_ack_history_is_bounded():
    session = OwnerStreamSession('ac', 'bi', 1, 'lease', 1)
    session.location = public_location('https://example.com/first', 'First')
    frame = session.push_frame(1, 'pixels', 1440, 900)
    session.location['url'] = 'https://different.example/second'
    assert session.public_frame(frame)['location']['url'] == 'https://example.com/first'
    broker = FrameBroker()
    for i in range(1000):
        assert broker.offer(i)
        assert broker.ack(i)
    assert broker.inflight == 0
    assert len(broker.acked) <= 128


def test_location_origin_uses_host_and_does_not_expose_userinfo():
    loc = public_location('https://trusted.example:password@evil.example/path?x=1#fragment')
    assert loc['origin'] == 'https://evil.example'
    assert loc['url'] == 'https://evil.example/path?x=1#fragment'
    assert public_location('https://[::1]:8443/x')['origin'] == 'https://[::1]:8443'


def test_control_shortcut_has_platform_keycode():
    params = cdp_key_params(phase='down', key='a', code='KeyA', modifiers=2)
    assert params['windowsVirtualKeyCode'] == 65
    assert params['modifiers'] == 2
    assert 'text' not in params


def test_identity_switch_stops_old_runtime_and_fences_old_input(tmp_path):
    from gateway.agent_computer.errors import StaleControllerError
    svc = AgentComputerService(AgentComputerStore(tmp_path / 'state.db'), InMemoryRuntime(), data_root=tmp_path)
    c = svc.ensure_computer('majed')
    identities = [svc.create_identity(ownership=['majed']) for _ in range(2)]
    svc.attach_identity(c.id, identities[0].id, OWNER_PRINCIPAL)
    _, old_lease = svc.wake(c.id, agent_principal('majed'))
    old_handle = svc._handles[c.id]
    svc.attach_identity(c.id, identities[1].id, OWNER_PRINCIPAL)
    assert not svc.runtime.alive(old_handle)
    assert svc.get_identity(identities[0].id).lock_computer_id is None
    assert svc.get_identity(identities[1].id).lock_computer_id == c.id
    with pytest.raises(StaleControllerError):
        svc.act(c.id, agent_principal('majed'), lease_id=old_lease.lease_id, fencing_epoch=old_lease.fencing_epoch, kind='text', text='stale')
    svc.wake(c.id, agent_principal('majed'))
    assert svc._handles[c.id].identity_id == identities[1].id
    new_handle = svc._handles[c.id]
    svc.revoke_identity(identities[1].id, OWNER_PRINCIPAL)
    assert not svc.runtime.alive(new_handle)
    assert svc.get_computer(c.id).active_browser_identity_id is None


def test_recovery_obeys_runtime_capacity(tmp_path):
    svc = AgentComputerService(AgentComputerStore(tmp_path / 'state.db'), FileRuntime(tmp_path), data_root=tmp_path, max_active_computers=1)
    first, second = [svc.ensure_computer(p) for p in ('first', 'second')]
    svc.wake(first.id, agent_principal('first'))
    with svc._lock, pytest.raises(ConflictError):
        svc._handle(second)
    assert not (tmp_path / second.id).exists()


def test_stale_persisted_pid_does_not_terminate_unrelated_process(tmp_path):
    import subprocess
    import sys
    from gateway.agent_computer.adapter import HermesChromiumRuntime
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        HermesChromiumRuntime().sleep(RuntimeHandle('ac', None, str(tmp_path), process_id=proc.pid))
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
