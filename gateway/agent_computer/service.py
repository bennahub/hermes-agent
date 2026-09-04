"""Agent Computer control plane.

Binds durable computers to permanent Hermes profiles, attaches a separate
BrowserIdentity with exclusive lock, and fences input with a ControlLease.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .adapter import (
    ComputerRuntime,
    InMemoryRuntime,
    RuntimeHandle,
    new_identity_profile_dir,
    page_needs_restore,
    safe_workspace_url,
)
from .location import public_location
from .stream import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_VIEWPORT_HEIGHT,
    DEFAULT_VIEWPORT_WIDTH,
    OwnerStreamSession,
    apply_stream_event,
    get_stream_hub,
    normalize_owner_event,
)
from .errors import (
    CheckpointRequiredError,
    ConflictError,
    ForbiddenError,
    IdentityBusyError,
    InvalidTokenError,
    NotFoundError,
    ObserveRequiredError,
    RevokedError,
    StaleControllerError,
)
from .models import (
    OWNER_PRINCIPAL,
    SENSITIVE_ACTION_CLASSES,
    AgentComputer,
    AuditEvent,
    BrowserIdentity,
    Checkpoint,
    CheckpointStatus,
    ControlAuthority,
    ControlLease,
    Controller,
    InputReceipt,
    LeaseStatus,
    Lifecycle,
    Observation,
    TakeoverToken,
    agent_principal,
    is_agent_principal,
    is_owner_principal,
    project_control,
)
from .store import AgentComputerStore
from .locking import ComputerOperationLock

BACKEND_NAME = "hermes_chromium"
OWNER_TAKEOVER_TTL_S = 30 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AgentComputerService:
    def __init__(
        self,
        store: AgentComputerStore,
        runtime: ComputerRuntime | None = None,
        *,
        data_root: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        takeover_ttl_s: int = OWNER_TAKEOVER_TTL_S,
        max_active_computers: int = 2,
    ):
        self.store = store
        self.runtime = runtime or InMemoryRuntime()
        self.data_root = Path(data_root or ".")
        self.clock = clock or _now
        self.takeover_ttl_s = takeover_ttl_s
        self.max_active_computers = max(1, int(max_active_computers))
        self._lock = ComputerOperationLock(self.store.path.with_suffix(".control.lock"))
        self._handles: dict[str, RuntimeHandle] = {}
        self._owner_transports: dict[str, int] = {}

    # ── computers ────────────────────────────────────────────────────
    def ensure_computer(self, profile_id: str) -> AgentComputer:
        if not profile_id or profile_id.startswith("session:") or profile_id.startswith("run:"):
            raise ConflictError("computer ownership must bind to a permanent profile")
        existing = self.store.get_computer_by_profile(profile_id)
        if existing:
            return existing
        cid = f"ac_{uuid.uuid4().hex}"
        from .adapter import private_dir

        persistence = str(private_dir(self.data_root / "computers" / profile_id / cid))
        computer = AgentComputer(
            id=cid,
            agent_profile_id=profile_id,
            backend=BACKEND_NAME,
            persistence_ref=persistence,
            lifecycle=Lifecycle.IDLE,
            created_at=_iso(self.clock()),
            updated_at=_iso(self.clock()),
        )
        self.store.upsert_computer(computer)
        self._audit(cid, "computer_provision", OWNER_PRINCIPAL, {"profile_id": profile_id})
        return computer

    def get_computer(self, computer_id: str) -> AgentComputer:
        computer = self.store.get_computer(computer_id)
        if not computer:
            raise NotFoundError(f"computer not found: {computer_id}")
        return computer

    def list_computers(self) -> list[AgentComputer]:
        ids = [computer.id for computer in self.store.list_computers()]
        for computer_id in ids:
            self.expire_owner_if_needed(computer_id)
        return self.store.list_computers()

    def list_identities(self) -> list[BrowserIdentity]:
        return self.store.list_identities()

    def list_audit(self, computer_id: str | None = None) -> list[AuditEvent]:
        return self.store.list_audit(computer_id)

    def authorize_read(self, computer: AgentComputer, principal: str) -> None:
        if is_owner_principal(principal):
            return
        if is_agent_principal(principal, computer.agent_profile_id):
            return
        raise ForbiddenError("not authorized for this computer")

    def authorize_agent_input(self, computer: AgentComputer, principal: str) -> None:
        if not is_agent_principal(principal, computer.agent_profile_id):
            raise ForbiddenError("only the owning agent may use agent input")

    def authorize_owner(self, principal: str) -> None:
        if not is_owner_principal(principal):
            raise ForbiddenError("owner-only")

    # ── identities ───────────────────────────────────────────────────
    def create_identity(
        self,
        *,
        ownership: list[str],
        metadata: dict[str, Any] | None = None,
        identity_id: str | None = None,
    ) -> BrowserIdentity:
        if not ownership:
            raise ConflictError("BrowserIdentity ownership must name at least one profile")
        iid = identity_id or f"bi_{uuid.uuid4().hex}"
        profile_ref = new_identity_profile_dir(self.data_root, iid)
        identity = BrowserIdentity(
            id=iid,
            profile_ref=profile_ref,
            ownership=list(ownership),
            metadata=dict(metadata or {}),
            created_at=_iso(self.clock()),
        )
        self.store.upsert_identity(identity)
        return identity

    def get_identity(self, identity_id: str) -> BrowserIdentity:
        identity = self.store.get_identity(identity_id)
        if not identity:
            raise NotFoundError(f"browser identity not found: {identity_id}")
        return identity

    def attach_identity(self, computer_id: str, identity_id: str, principal: str) -> AgentComputer:
        with self._lock:
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            if computer.control_authority == ControlAuthority.OWNER_CONTROLLED and not is_owner_principal(principal):
                raise ConflictError("owner currently controls this computer")
            identity = self.get_identity(identity_id)
            if identity.revoked:
                raise RevokedError("browser identity revoked")
            if not identity.allows(computer.agent_profile_id):
                raise ForbiddenError("profile is not authorized for this BrowserIdentity")
            if computer.active_browser_identity_id == identity.id:
                return computer
            locked = self.store.try_lock_identity(identity.id, computer.id, principal)
            if locked is None:
                identity = self.get_identity(identity_id)
                raise IdentityBusyError(
                    "browser identity is exclusively locked",
                    details={
                        "identity_id": identity.id,
                        "locked_by_computer_id": identity.lock_computer_id,
                    },
                )
            identity = locked
            if computer.active_browser_identity_id:
                computer = self.detach_identity(computer.id, principal)
            elif self._handles.get(computer.id) or self._attach_handle(computer):
                computer = self._retire_identity_runtime(computer)
            computer.active_browser_identity_id = identity.id
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            self._audit(
                computer.id,
                "browser_identity_attach",
                principal,
                {"identity_id": identity.id},
            )
            return computer

    def detach_identity(self, computer_id: str, principal: str) -> AgentComputer:
        with self._lock:
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            if computer.control_authority == ControlAuthority.OWNER_CONTROLLED and not is_owner_principal(principal):
                raise ConflictError("owner currently controls this computer")
            iid = computer.active_browser_identity_id
            if iid:
                computer = self._retire_identity_runtime(computer)
                identity = self.get_identity(iid)
                if identity.lock_computer_id == computer.id:
                    identity.lock_computer_id = None
                    identity.lock_holder = None
                    self.store.upsert_identity(identity)
                computer.active_browser_identity_id = None
                computer.updated_at = _iso(self.clock())
                self.store.upsert_computer(computer)
                self._audit(computer.id, "browser_identity_detach", principal, {"identity_id": iid})
            return computer

    def _retire_identity_runtime(self, computer: AgentComputer) -> AgentComputer:
        """Stop using the old profile before its exclusive mount is released."""
        computer = self.sleep(computer.id, OWNER_PRINCIPAL)
        self.store.revoke_leases(computer.id)
        self.store.expire_tokens_for_computer(computer.id)
        self._drop_live_stream(computer.id)
        computer.fencing_epoch += 1
        computer.control_authority = ControlAuthority.AGENT_CONTROLLED
        computer.resume_observe_required = True
        computer.workspace_url = ""
        computer.workspace_title = ""
        self.store.upsert_computer(computer)
        return computer

    def revoke_identity(self, identity_id: str, principal: str) -> BrowserIdentity:
        self.authorize_owner(principal)
        with self._lock:
            for computer in self.store.list_computers():
                if computer.active_browser_identity_id == identity_id:
                    self.detach_identity(computer.id, principal)
            identity = self.get_identity(identity_id)
            identity.revoked = True
            identity.lock_computer_id = None
            identity.lock_holder = None
            self.store.upsert_identity(identity)
            self._audit(None, "identity_revoked", principal, {"identity_id": identity_id})
            return identity

    # ── lifecycle ────────────────────────────────────────────────────
    def wake(self, computer_id: str, principal: str) -> tuple[AgentComputer, ControlLease]:
        with self._lock:
            self.expire_owner_if_needed(computer_id)
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            if (
                computer.control_authority == ControlAuthority.OWNER_CONTROLLED
                and is_agent_principal(principal, computer.agent_profile_id)
            ):
                raise ConflictError("owner currently controls this computer")
            existing = self._handles.get(computer.id)
            if existing and existing.identity_id != computer.active_browser_identity_id:
                self._handles.pop(computer.id, None)
                existing = None
            if existing and not self._runtime_alive(existing):
                try:
                    self.runtime.sleep(existing)
                except Exception:
                    pass
                self._handles.pop(computer.id, None)
                existing = None
            if existing is None:
                existing = self._attach_handle(computer)
            if (
                existing
                and self._runtime_alive(existing)
            ):
                lease = self.store.active_lease_for_computer(computer.id)
                if lease is None:
                    if computer.control_authority == ControlAuthority.OWNER_CONTROLLED:
                        lease = self._issue_owner_lease(computer)
                    else:
                        lease = self._issue_agent_lease(computer)
                self._restore_workspace(computer, existing)
                computer.lifecycle = Lifecycle.READY
                computer.updated_at = _iso(self.clock())
                self.store.upsert_computer(computer)
                return computer, lease
            active = []
            for other in self.store.list_computers():
                if other.id == computer.id:
                    continue
                handle = self._handles.get(other.id)
                if handle is not None and not self._runtime_alive(handle):
                    self._handles.pop(other.id, None)
                    handle = None
                handle = handle or self._attach_handle(other)
                if handle is not None and self._runtime_alive(handle):
                    active.append(other.agent_profile_id)
                elif other.lifecycle in (Lifecycle.READY, Lifecycle.BUSY, Lifecycle.WAKING):
                    other.lifecycle = Lifecycle.SLEEPING
                    self.store.upsert_computer(other)
            if len(active) >= self.max_active_computers:
                raise ConflictError(
                    "Active computer limit reached. Suspend an unused computer before waking another.",
                    details={"max_active_computers": self.max_active_computers, "active_profiles": active},
                )
            computer.lifecycle = Lifecycle.WAKING
            self.store.upsert_computer(computer)
            identity = None
            if computer.active_browser_identity_id:
                identity = self.get_identity(computer.active_browser_identity_id)
                if identity.revoked:
                    raise RevokedError("attached browser identity is revoked")
            handle = self.runtime.wake(computer, identity)
            self._handles[computer.id] = handle
            self._restore_workspace(computer, handle)
            computer.lifecycle = Lifecycle.READY
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            lease = self.store.active_lease_for_computer(computer.id)
            if lease is None:
                if computer.control_authority in (
                    ControlAuthority.OWNER_CONTROLLED,
                    ControlAuthority.TAKEOVER_PENDING,
                    ControlAuthority.YIELDING,
                ):
                    lease = self._issue_owner_lease(computer)
                else:
                    lease = self._issue_agent_lease(computer)
            self._audit(computer.id, "runtime_start", principal, {"backend": handle.backend})
            return computer, lease

    def sleep(self, computer_id: str, principal: str) -> AgentComputer:
        with self._lock:
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            if computer.control_authority == ControlAuthority.OWNER_CONTROLLED and not is_owner_principal(principal):
                raise ConflictError("owner currently controls this computer")
            handle = self._handles.get(computer.id) or self._attach_handle(computer)
            self._handles.pop(computer.id, None)
            if handle:
                current_location = getattr(self.runtime, "current_location", None)
                if callable(current_location):
                    loc = current_location(handle)
                    self._remember_workspace(computer, str(loc.get("url") or ""), str(loc.get("title") or ""))
                self._drop_live_stream(computer.id)
                self.runtime.sleep(handle)
            computer.lifecycle = Lifecycle.SLEEPING
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            self._audit(computer.id, "runtime_sleep", principal, {})
            return computer

    def _runtime_alive(self, handle: RuntimeHandle) -> bool:
        alive = getattr(self.runtime, "alive", None)
        if not callable(alive):
            return True
        try:
            return bool(alive(handle))
        except Exception:
            return False

    def _attach_handle(self, computer: AgentComputer) -> RuntimeHandle | None:
        attach = getattr(self.runtime, "attach", None)
        if not callable(attach):
            return None
        identity = None
        if computer.active_browser_identity_id:
            identity = self.get_identity(computer.active_browser_identity_id)
        try:
            handle = attach(computer, identity)
        except Exception:
            return None
        if handle is None:
            return None
        self._handles[computer.id] = handle
        return handle

    def _drop_live_stream(self, computer_id: str) -> None:
        live = get_stream_hub().get(computer_id)
        if live:
            get_stream_hub().drop(computer_id, live.generation)

    def _handle(self, computer: AgentComputer) -> RuntimeHandle:
        handle = self._handles.get(computer.id)
        if handle and handle.identity_id != computer.active_browser_identity_id:
            self._handles.pop(computer.id, None)
            handle = None
        if handle and self._runtime_alive(handle):
            return handle
        if handle:
            try:
                self.runtime.sleep(handle)
            except Exception:
                pass
            self._handles.pop(computer.id, None)
        recovered = self._attach_handle(computer)
        if recovered is not None:
            self._audit(computer.id, "recovery", "system", {"reason": "reattach_runtime"})
            return recovered
        # Recovery uses the same serialized resource admission as explicit wake.
        self.wake(computer.id, OWNER_PRINCIPAL)
        handle = self._handles[computer.id]
        self._audit(computer.id, "recovery", "system", {"reason": "recreate_runtime"})
        return handle

    def _restore_workspace(self, computer: AgentComputer, handle: RuntimeHandle) -> None:
        restore = getattr(self.runtime, "ensure_workspace", None)
        if not callable(restore):
            return
        try:
            restore(handle, computer.workspace_url)
        except Exception:
            return

    def _remember_workspace(self, computer: AgentComputer, url: str, title: str = "") -> None:
        saved = safe_workspace_url(url)
        if not saved or page_needs_restore(saved):
            return
        computer.workspace_url = saved
        computer.workspace_title = title

    # ── observe / act ────────────────────────────────────────────────
    def observe(self, computer_id: str, principal: str, *, lease_id: str, fencing_epoch: int) -> Observation:
        with self._lock:
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            if not is_owner_principal(principal):
                self._require_lease(computer, principal, lease_id, fencing_epoch, allow_owner=True)
            computer = self.get_computer(computer_id)
            obs = self.runtime.observe(self._handle(computer))
            if not is_owner_principal(principal):
                computer.resume_observe_required = False
            self._remember_workspace(computer, obs.url, obs.title)
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            obs.fencing_epoch = computer.fencing_epoch
            obs.controller = computer.control_authority.value
            return obs

    def act(
        self,
        computer_id: str,
        principal: str,
        *,
        lease_id: str,
        fencing_epoch: int,
        kind: str,
        target: str = "",
        text: str = "",
        action_class: str = "",
        x: float | None = None,
        y: float | None = None,
        key: str = "",
        code: str = "",
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> InputReceipt:
        with self._lock:
            computer = self.get_computer(computer_id)
            self.authorize_read(computer, principal)
            controller = self._require_lease(computer, principal, lease_id, fencing_epoch, allow_owner=True)
            computer = self.get_computer(computer_id)
            if computer.resume_observe_required and controller == Controller.AGENT:
                raise ObserveRequiredError("agent must re-observe after control returns")
            consumed_checkpoint_id = None
            if action_class in SENSITIVE_ACTION_CLASSES:
                open_cp = self.store.open_checkpoint(computer.id, action_class)
                if open_cp is None:
                    cp = Checkpoint(
                        id=f"cp_{uuid.uuid4().hex}",
                        computer_id=computer.id,
                        action_class=action_class,
                        status=CheckpointStatus.BLOCKED,
                        created_at=_iso(self.clock()),
                    )
                    self.store.upsert_checkpoint(cp)
                    self._audit(
                        computer.id,
                        "checkpoint_blocked",
                        principal,
                        {"action_class": action_class, "checkpoint_id": cp.id},
                    )
                    raise CheckpointRequiredError(
                        "sensitive action requires owner checkpoint",
                        details={"checkpoint_id": cp.id, "action_class": action_class},
                    )
                consumed_checkpoint_id = open_cp.id
            computer.lifecycle = Lifecycle.BUSY
            obs = self.runtime.act(
                self._handle(computer),
                kind=kind,
                target=target,
                text=text,
                action_class=action_class,
                x=x,
                y=y,
                key=key,
                code=code,
                delta_x=delta_x,
                delta_y=delta_y,
            )
            self._remember_workspace(computer, obs.url, obs.title)
            computer.lifecycle = Lifecycle.READY
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            if consumed_checkpoint_id:
                self.store.consume_checkpoint(consumed_checkpoint_id)
            self._audit(
                computer.id,
                "input_accepted",
                principal,
                {
                    "kind": kind,
                    "controller": controller.value,
                    "epoch": computer.fencing_epoch,
                    "lease_id": lease_id,
                },
            )
            return InputReceipt(
                accepted=True,
                fencing_epoch=computer.fencing_epoch,
                url=obs.url,
                title=obs.title,
                text=obs.text,
            )

    # ── takeover ─────────────────────────────────────────────────────
    def request_takeover(self, computer_id: str, principal: str, *, reason: str = "") -> dict[str, Any]:
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            if computer.control_authority == ControlAuthority.OWNER_CONTROLLED:
                token = self._mint_takeover_token(computer, principal)
                return {
                    "status": computer.control_authority.value,
                    "fencing_epoch": computer.fencing_epoch,
                    "takeover_token": token,
                    "duplicate": True,
                }
            computer.control_authority = ControlAuthority.TAKEOVER_PENDING
            self.store.revoke_leases(computer.id)
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            self._audit(computer.id, "takeover_requested", principal, {"reason": reason})
            self._audit(computer.id, "agent_yielded", agent_principal(computer.agent_profile_id), {})
            computer.control_authority = ControlAuthority.OWNER_CONTROLLED
            lease = self._issue_owner_lease(computer)
            token = self._mint_takeover_token(computer, principal)
            self.store.upsert_computer(computer)
            self._audit(
                computer.id,
                "owner_takeover_granted",
                principal,
                {"fencing_epoch": computer.fencing_epoch, "lease_id": lease.lease_id},
            )
            return {
                "status": computer.control_authority.value,
                "fencing_epoch": computer.fencing_epoch,
                "lease_id": lease.lease_id,
                "takeover_token": token,
                "duplicate": False,
            }

    def bind_owner_transport(self, computer_id: str, transport: object | None) -> None:
        if transport is None:
            return
        self._owner_transports[computer_id] = id(transport)

    def release_owner_for_transport(self, transport: object) -> int:
        key = id(transport)
        released = 0
        for computer_id, tok in list(self._owner_transports.items()):
            if tok != key:
                continue
            self.owner_disconnect(computer_id, OWNER_PRINCIPAL)
            self._owner_transports.pop(computer_id, None)
            released += 1
        return released

    def connect_takeover(
        self, computer_id: str, principal: str, *, takeover_token: str
    ) -> ControlLease:
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            record = self.store.get_token_by_hash(_hash_token(takeover_token))
            if not record or record.consumed:
                raise InvalidTokenError("takeover token invalid or consumed")
            if record.computer_id != computer.id:
                raise ForbiddenError("takeover token is bound to another computer")
            if record.owner_principal != principal:
                raise ForbiddenError("takeover token is bound to another principal")
            if record.fencing_epoch != computer.fencing_epoch:
                raise StaleControllerError("takeover token epoch is stale")
            expires = datetime.fromisoformat(record.expires_at)
            if self.clock() >= expires:
                self._audit(computer.id, "takeover_expired", principal, {"token_id": record.token_id})
                raise InvalidTokenError("takeover token expired")
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                raise ConflictError("computer is not owner-controlled")
            self.store.mark_token_consumed(record.token_id)
            lease = self.store.active_lease_for_computer(computer.id)
            if lease is None:
                lease = self._issue_owner_lease(computer)
            self._audit(computer.id, "takeover_connected", principal, {"lease_id": lease.lease_id})
            return lease

    def give_back(self, computer_id: str, principal: str, *, lease_id: str, fencing_epoch: int) -> ControlLease:
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                # Duplicate give-back: return current agent lease if already returned.
                active = self.store.active_lease_for_computer(computer.id)
                if active and active.controller == Controller.AGENT:
                    return active
                raise ConflictError("computer is not owner-controlled")
            self._require_lease(computer, principal, lease_id, fencing_epoch, allow_owner=True)
            computer.control_authority = ControlAuthority.RETURNING
            self.store.revoke_leases(computer.id)
            self.store.expire_tokens_for_computer(computer.id)
            computer.resume_observe_required = True
            lease = self._issue_agent_lease(computer)
            computer.control_authority = ControlAuthority.AGENT_CONTROLLED
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            self._owner_transports.pop(computer.id, None)
            self._drop_live_stream(computer.id)
            self._audit(computer.id, "control_returned", principal, {"new_epoch": computer.fencing_epoch})
            return lease

    def owner_disconnect(self, computer_id: str, principal: str) -> Optional[ControlLease]:
        """Transport loss: return exclusive control to the agent exactly once."""
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                return self.store.active_lease_for_computer(computer.id)
            self.store.revoke_leases(computer.id)
            self.store.expire_tokens_for_computer(computer.id)
            computer.resume_observe_required = True
            lease = self._issue_agent_lease(computer)
            computer.control_authority = ControlAuthority.AGENT_CONTROLLED
            computer.updated_at = _iso(self.clock())
            self.store.upsert_computer(computer)
            self._owner_transports.pop(computer.id, None)
            self._drop_live_stream(computer.id)
            self._audit(computer.id, "owner_disconnect", principal, {"new_epoch": computer.fencing_epoch})
            self._audit(computer.id, "fencing_recovery", "system", {"new_epoch": computer.fencing_epoch})
            return lease

    def expire_owner_if_needed(self, computer_id: str) -> Optional[ControlLease]:
        with self._lock:
            computer = self.get_computer(computer_id)
            lease = self.store.active_lease_for_computer(computer.id)
            if not lease or lease.controller != Controller.OWNER:
                return None
            if not lease.expires_at:
                return None
            if self.clock() < datetime.fromisoformat(lease.expires_at):
                return None
            self.store.revoke_leases(computer.id)
            self.store.expire_tokens_for_computer(computer.id)
            computer.control_authority = ControlAuthority.AGENT_CONTROLLED
            computer.resume_observe_required = True
            new_lease = self._issue_agent_lease(computer)
            self.store.upsert_computer(computer)
            self._owner_transports.pop(computer.id, None)
            self._drop_live_stream(computer.id)
            self._audit(computer.id, "takeover_expired", "system", {"lease_id": lease.lease_id})
            self._audit(computer.id, "fencing_recovery", "system", {"new_epoch": computer.fencing_epoch})
            return new_lease

    # ── checkpoints ──────────────────────────────────────────────────
    def approve_checkpoint(self, checkpoint_id: str, principal: str) -> Checkpoint:
        self.authorize_owner(principal)
        cp = self.store.get_checkpoint(checkpoint_id)
        if not cp:
            raise NotFoundError("checkpoint not found")
        cp.status = CheckpointStatus.APPROVED
        self.store.upsert_checkpoint(cp)
        self._audit(cp.computer_id, "checkpoint_approved", principal, {"checkpoint_id": cp.id})
        return cp

    # ── owner stream ─────────────────────────────────────────────────
    def open_owner_stream(
        self,
        computer_id: str,
        principal: str,
        *,
        lease_id: str,
        fencing_epoch: int,
        width: int = 0,
        height: int = 0,
    ) -> tuple[OwnerStreamSession, AgentComputer]:
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                raise ConflictError("computer is not owner-controlled")
            self._require_lease(computer, principal, lease_id, fencing_epoch, allow_owner=True)
            # Pin the Chromium source viewport. The Owner window may be any
            # size; the canvas letterboxes/scales the 1440×900 frame. Using
            # the stage CSS box as the CDP viewport made clicks miss.
            _ = width, height
            vw, vh = DEFAULT_VIEWPORT_WIDTH, DEFAULT_VIEWPORT_HEIGHT
            hub = get_stream_hub()
            previous = hub.get(computer.id)
            generation = hub.next_generation(computer.id)
            session = OwnerStreamSession(
                computer_id=computer.id,
                identity_id=computer.active_browser_identity_id,
                generation=generation,
                lease_id=lease_id,
                fencing_epoch=fencing_epoch,
                viewport_width=vw,
                viewport_height=vh,
                jpeg_quality=DEFAULT_JPEG_QUALITY,
            )
            hub.attach(session)
            handle = self._handles.get(computer.id) or self._attach_handle(computer)
            if handle is not None:
                self._restore_workspace(computer, handle)
                try:
                    obs = self.runtime.observe(handle)
                    self._remember_workspace(computer, obs.url, obs.title)
                    self.store.upsert_computer(computer)
                except Exception:
                    pass
            self._audit(
                computer.id,
                "stream_replaced" if previous else "stream_opened",
                principal,
                {"generation": generation, "kind": "screencast"},
            )
            return session, computer

    def owner_stream_input(
        self,
        computer_id: str,
        principal: str,
        *,
        lease_id: str,
        fencing_epoch: int,
        generation: int,
        event: dict,
    ) -> dict:
        self.authorize_owner(principal)
        with self._lock:
            computer = self.get_computer(computer_id)
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                raise ConflictError("computer is not owner-controlled")
            self._require_lease(computer, principal, lease_id, fencing_epoch, allow_owner=True)
            session = get_stream_hub().get(computer.id)
            if session is None or session.generation != int(generation):
                raise StaleControllerError("stream generation is stale")
            normalized = normalize_owner_event(
                event,
                viewport_width=session.viewport_width,
                viewport_height=session.viewport_height,
            )
            kind = normalized.get("kind")
            if kind == "ack":
                session.ack(int(normalized.get("session_id") or 0))
                return {"ok": True, "kind": "ack"}
            if kind == "ping":
                return {"ok": True, "kind": "ping"}
            if kind == "cursor":
                handle = self._handles.get(computer.id) or self._handle(computer)
                probe = getattr(self.runtime, "probe_cursor", None)
                raw = "default"
                if callable(probe):
                    try:
                        raw = str(probe(handle, float(normalized["x"]), float(normalized["y"])) or "default")
                    except Exception:
                        raw = "default"
                from .cursor import map_remote_cursor

                return {"ok": True, "kind": "cursor", "cursor": map_remote_cursor(raw)}
            if kind == "nav":
                handle = self._handles.get(computer.id) or self._handle(computer)
                loc = apply_stream_event(self.runtime, handle, normalized) or {}
                raw_url = str((loc or {}).get("url") or "")
                title = str((loc or {}).get("title") or "")
                self._remember_workspace(computer, raw_url, title)
                self.store.upsert_computer(computer)
                from .location import public_location

                pub = public_location(raw_url, title)
                return {"ok": True, "kind": "nav", **pub}
            if kind == "resize":
                # Display-only. Chromium stays on the pinned source viewport
                # so mapping does not drift mid-session.
                return {"ok": True, "kind": kind}
            # Frames stay on the stream websocket. Input uses loopback CDP —
            # the same path that changes real page state on this host.
            # Fire-and-forget on the screencast socket was received by audit
            # but did not affect Chromium.
            handle = self._handles.get(computer.id) or self._handle(computer)
            apply_stream_event(self.runtime, handle, normalized)
            session.last_input_kind = str(kind)
            self._audit(computer.id, "stream_input", principal, {"kind": kind, "generation": session.generation})
            return {"ok": True, "kind": kind}

    def close_owner_stream(self, computer_id: str, generation: int) -> None:
        """Stop the live stream only. Does not return control to the agent."""
        get_stream_hub().drop(computer_id, generation)
        self._audit(computer_id, "stream_closed", "owner", {"generation": generation})

    # ── public status ────────────────────────────────────────────────
    def public_status(self, computer: AgentComputer) -> dict[str, Any]:
        lease = self.store.active_lease_for_computer(computer.id)
        identity = None
        if computer.active_browser_identity_id:
            identity = self.store.get_identity(computer.active_browser_identity_id)
        return {
            "computer_id": computer.id,
            "agent_profile_id": computer.agent_profile_id,
            "lifecycle": computer.lifecycle.value,
            "control": computer.control_authority.value,
            "control_label": project_control(computer.control_authority.value),
            "can_resume": bool(
                lease
                and lease.controller == Controller.OWNER
                and computer.control_authority == ControlAuthority.OWNER_CONTROLLED
            ),
            "location": public_location(computer.workspace_url, computer.workspace_title),
            "fencing_epoch": computer.fencing_epoch,
            "resume_observe_required": computer.resume_observe_required,
            "workspace": {
                "url": computer.workspace_url,
                "title": computer.workspace_title,
                "artifacts": self.list_workspace_artifacts(computer),
            },
            "browser_identity": (
                {
                    "id": identity.id,
                    "revoked": identity.revoked,
                    "locked": bool(identity.lock_computer_id),
                    "metadata": identity.metadata,
                }
                if identity
                else None
            ),
            "lease": (
                {
                    "lease_id": lease.lease_id,
                    "controller": lease.controller.value,
                    "fencing_epoch": lease.fencing_epoch,
                    "status": lease.status.value,
                    "expires_at": lease.expires_at,
                }
                if lease
                else None
            ),
            "stream": {
                "path": f"/api/agent-computers/{computer.id}/stream",
                "kind": "screencast_frames",
                "public_cdp": False,
            },
        }

    def workspace_root(self, computer: AgentComputer) -> Path:
        root = (Path(computer.persistence_ref) / "workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "downloads").mkdir(exist_ok=True)
        (root / "uploads").mkdir(exist_ok=True)
        return root

    def list_workspace_artifacts(self, computer: AgentComputer) -> list[dict[str, Any]]:
        items = []
        root = self.workspace_root(computer)
        for folder in ("downloads", "uploads"):
            directory = root / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                items.append(
                    {
                        "name": path.name,
                        "kind": folder.rstrip("s"),
                        "size": path.stat().st_size,
                    }
                )
        return items

    def resolve_workspace_file(self, computer: AgentComputer, name: str, *, folder: str = "downloads") -> Path:
        if folder not in {"downloads", "uploads"}:
            raise ConflictError("invalid workspace folder")
        basename = Path(name).name
        if not basename or basename != name:
            raise ForbiddenError("workspace file must be a basename")
        root = self.workspace_root(computer)
        path = (root / folder / basename).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ForbiddenError("workspace path escapes computer") from exc
        return path

    def write_workspace_upload(self, computer: AgentComputer, name: str, data: bytes) -> dict[str, Any]:
        path = self.resolve_workspace_file(computer, name, folder="uploads")
        path.write_bytes(data)
        return {"name": path.name, "kind": "upload", "size": path.stat().st_size}

    def public_identity(self, identity: BrowserIdentity) -> dict[str, Any]:
        return {
            "id": identity.id,
            "ownership": identity.ownership,
            "revoked": identity.revoked,
            "locked": bool(identity.lock_computer_id),
            "lock_computer_id": identity.lock_computer_id,
            "metadata": identity.metadata,
        }

    # ── internals ────────────────────────────────────────────────────
    def _bump_epoch(self, computer: AgentComputer) -> int:
        computer.fencing_epoch += 1
        return computer.fencing_epoch

    def _issue_agent_lease(self, computer: AgentComputer) -> ControlLease:
        self.store.revoke_leases(computer.id)
        epoch = self._bump_epoch(computer)
        lease = ControlLease(
            lease_id=f"ls_{uuid.uuid4().hex}",
            computer_id=computer.id,
            controller=Controller.AGENT,
            fencing_epoch=epoch,
            acquired_at=_iso(self.clock()),
            expires_at=None,
            status=LeaseStatus.ACTIVE,
        )
        self.store.upsert_lease(lease)
        computer.control_authority = ControlAuthority.AGENT_CONTROLLED
        self.store.upsert_computer(computer)
        return lease

    def _issue_owner_lease(self, computer: AgentComputer) -> ControlLease:
        self.store.revoke_leases(computer.id)
        epoch = self._bump_epoch(computer)
        exp = self.clock() + timedelta(seconds=self.takeover_ttl_s)
        lease = ControlLease(
            lease_id=f"ls_{uuid.uuid4().hex}",
            computer_id=computer.id,
            controller=Controller.OWNER,
            fencing_epoch=epoch,
            acquired_at=_iso(self.clock()),
            expires_at=_iso(exp),
            status=LeaseStatus.ACTIVE,
        )
        self.store.upsert_lease(lease)
        computer.control_authority = ControlAuthority.OWNER_CONTROLLED
        self.store.upsert_computer(computer)
        return lease

    def _mint_takeover_token(self, computer: AgentComputer, principal: str) -> str:
        raw = secrets.token_urlsafe(32)
        exp = self.clock() + timedelta(seconds=self.takeover_ttl_s)
        rec = TakeoverToken(
            token_id=f"tt_{uuid.uuid4().hex}",
            token_hash=_hash_token(raw),
            computer_id=computer.id,
            owner_principal=principal,
            fencing_epoch=computer.fencing_epoch,
            expires_at=_iso(exp),
        )
        self.store.insert_token(rec)
        return raw

    def _require_lease(
        self,
        computer: AgentComputer,
        principal: str,
        lease_id: str,
        fencing_epoch: int,
        *,
        allow_owner: bool,
    ) -> Controller:
        self.expire_owner_if_needed(computer.id)
        computer = self.get_computer(computer.id)
        lease = self.store.get_lease(lease_id)
        if not lease or lease.computer_id != computer.id:
            self._audit(computer.id, "stale_controller_rejected", principal, {"reason": "unknown_lease"})
            raise StaleControllerError("unknown lease")
        if lease.status != LeaseStatus.ACTIVE:
            self._audit(computer.id, "stale_controller_rejected", principal, {"reason": "revoked_lease"})
            raise StaleControllerError("lease is not active")
        if lease.fencing_epoch != computer.fencing_epoch or lease.fencing_epoch != fencing_epoch:
            self._audit(
                computer.id,
                "stale_controller_rejected",
                principal,
                {"presented": fencing_epoch, "current": computer.fencing_epoch},
            )
            raise StaleControllerError("stale fencing epoch")
        if lease.controller == Controller.OWNER:
            if not allow_owner or not is_owner_principal(principal):
                raise ForbiddenError("owner lease required")
            if computer.control_authority != ControlAuthority.OWNER_CONTROLLED:
                raise StaleControllerError("owner is not the authority")
            return Controller.OWNER
        if not is_agent_principal(principal, computer.agent_profile_id):
            raise ForbiddenError("agent lease required")
        if computer.control_authority != ControlAuthority.AGENT_CONTROLLED:
            self._audit(computer.id, "stale_controller_rejected", principal, {"reason": "agent_during_owner"})
            raise StaleControllerError("agent is not the authority")
        return Controller.AGENT

    def _audit(self, computer_id: str | None, event_type: str, actor: str, detail: dict[str, Any]) -> None:
        redact = {
            "cookie",
            "cookies",
            "token",
            "takeover_token",
            "password",
            "secret",
            "text",
            "value",
            "typed",
            "input",
            "content",
            "key",
            "keys",
            "code",
            "payload",
            "message_text",
        }
        safe = {k: v for k, v in detail.items() if str(k).lower() not in redact}
        lease = safe.get("lease_id")
        if isinstance(lease, str) and len(lease) > 11:
            safe["lease_id"] = lease[:11] + "…"
        self.store.append_audit(
            AuditEvent(
                event_type=event_type,
                computer_id=computer_id,
                actor=actor,
                detail=safe,
                created_at=_iso(self.clock()),
            )
        )
