"""Owner-authenticated REST surface for Agent Computers.

Mounted by web_server. Auth is the existing dashboard gate
(``_require_token`` / gated cookie). This router never exposes CDP,
cookies, or managed-profile filesystem paths.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from gateway.agent_computer import get_contract
from gateway.agent_computer.contract import error_payload, owner_principal
from gateway.agent_computer.errors import AgentComputerError
from hermes_cli.web_deps import late
from hermes_cli.web_routers.agent_computer_ui import COMPUTER_UI_HTML

router = APIRouter()
_require_token = late("_require_token")
_ws_auth_reason = late("_ws_auth_reason")
_ws_host_origin_reason = late("_ws_host_origin_reason")
_ws_client_reason = late("_ws_client_reason")
_ws_close_reason = late("_ws_close_reason")


class EnsureBody(BaseModel):
    profile_id: str


class IdentityCreateBody(BaseModel):
    ownership: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttachBody(BaseModel):
    identity_id: str


class LeaseBody(BaseModel):
    lease_id: str = ""
    fencing_epoch: int = 0
    kind: str = ""
    target: str = ""
    text: str = ""
    action_class: str = ""
    x: float | None = None
    y: float | None = None
    key: str = ""
    code: str = ""
    delta_x: float = 0
    delta_y: float = 0


class TakeoverBody(BaseModel):
    reason: str = ""


class ConnectBody(BaseModel):
    takeover_token: str


def _owner(request: Request) -> str:
    _require_token(request)
    return owner_principal()


def _http(exc: AgentComputerError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=error_payload(exc))


@router.get("/computer")
def computer_ui(request: Request):
    """Authenticated owner takeover surface. Human language only."""
    _require_token(request)
    return HTMLResponse(COMPUTER_UI_HTML)


@router.get("/api/agent-computers")
def list_computers(request: Request):
    try:
        return get_contract().list_computers(_owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/ensure")
def ensure_computer(request: Request, body: EnsureBody):
    try:
        return get_contract().ensure(body.profile_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.get("/api/agent-computers/{computer_id}")
def get_computer(request: Request, computer_id: str):
    try:
        return get_contract().status(computer_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/wake")
def wake_computer(request: Request, computer_id: str):
    try:
        return get_contract().wake(computer_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/sleep")
def sleep_computer(request: Request, computer_id: str):
    try:
        return get_contract().sleep(computer_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/observe")
def observe_computer(request: Request, computer_id: str, body: LeaseBody):
    try:
        return get_contract().observe(
            computer_id,
            _owner(request),
            lease_id=body.lease_id,
            fencing_epoch=body.fencing_epoch,
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/act")
def act_computer(request: Request, computer_id: str, body: LeaseBody):
    try:
        return get_contract().act(
            computer_id,
            _owner(request),
            lease_id=body.lease_id,
            fencing_epoch=body.fencing_epoch,
            kind=body.kind,
            target=body.target,
            text=body.text,
            action_class=body.action_class,
            x=body.x,
            y=body.y,
            key=body.key,
            code=body.code,
            delta_x=body.delta_x,
            delta_y=body.delta_y,
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/takeover")
def takeover_computer(request: Request, computer_id: str, body: Optional[TakeoverBody] = None):
    try:
        return get_contract().request_takeover(
            computer_id, _owner(request), reason=(body.reason if body else "")
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/takeover/connect")
def connect_takeover(request: Request, computer_id: str, body: ConnectBody):
    try:
        return get_contract().connect_takeover(
            computer_id, _owner(request), takeover_token=body.takeover_token
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/give-back")
def give_back(request: Request, computer_id: str, body: LeaseBody):
    try:
        return get_contract().give_back(
            computer_id,
            _owner(request),
            lease_id=body.lease_id,
            fencing_epoch=body.fencing_epoch,
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.websocket("/api/agent-computers/{computer_id}/stream")
async def computer_stream(ws: WebSocket, computer_id: str):
    """Authenticated Owner screencast. Loopback CDP never leaves the host.

    Accept the upgrade before Chromium/lease work. Close-before-accept
    becomes HTTP 403 with no close code — the browser then loops on
    1006 / "Reconnecting..." and never renders a frame.
    """
    import asyncio
    import logging

    from gateway.agent_computer.stream import (
        emit_memory_frame,
        get_stream_hub,
        run_chromium_screencast,
    )

    log = logging.getLogger("hermes.agent_computer.stream")
    peer = ws.client.host if ws.client else "?"
    auth_reason, cred = _ws_auth_reason(ws)
    origin_reason = _ws_host_origin_reason(ws)
    client_reason = _ws_client_reason(ws)
    await ws.accept()

    async def _reject(code: int, reason: str, *, retry: bool = False) -> None:
        try:
            await ws.send_json(
                {"type": "error", "code": code, "reason": reason, "retry": retry}
            )
        except Exception:
            pass
        try:
            await ws.close(code=code, reason=_ws_close_reason(reason))
        except Exception:
            pass

    if auth_reason is not None:
        log.warning(
            "stream auth rejected reason=%s cred=%s peer=%s computer=%s",
            auth_reason, cred, peer, computer_id,
        )
        await _reject(4401, "auth")
        return
    if origin_reason is not None:
        log.warning("stream refused: %s peer=%s computer=%s", origin_reason, peer, computer_id)
        await _reject(4403, "origin")
        return
    if client_reason is not None:
        log.warning("stream refused: %s computer=%s", client_reason, computer_id)
        await _reject(4403, "peer")
        return

    lease_id = str(ws.query_params.get("lease_id") or "")
    try:
        fencing_epoch = int(ws.query_params.get("fencing_epoch") or 0)
        width = int(ws.query_params.get("width") or 0)
        height = int(ws.query_params.get("height") or 0)
    except ValueError:
        await _reject(4400, "bad_params")
        return

    contract = get_contract()
    principal = owner_principal()
    try:
        await ws.send_json({"type": "status", "phase": "starting"})
        hello = await asyncio.to_thread(
            lambda: contract.open_stream(
                computer_id,
                principal,
                lease_id=lease_id,
                fencing_epoch=fencing_epoch,
                width=width,
                height=height,
            )
        )
    except AgentComputerError as exc:
        log.warning(
            "stream open rejected code=%s status=%s computer=%s",
            exc.code, exc.http_status, computer_id,
        )
        await _reject(4403 if exc.http_status == 403 else 4409, exc.code.lower())
        return

    generation = int(hello.get("generation") or 0)
    live = get_stream_hub().get(computer_id)
    pump_task = None
    cdp_task = None

    async def _pump() -> None:
        checked_at = 0.0
        try:
            while live and not live.closed:
                now = asyncio.get_running_loop().time()
                if now - checked_at >= 0.5:
                    await asyncio.to_thread(
                        contract.stream_input, computer_id, principal,
                        lease_id=lease_id, fencing_epoch=fencing_epoch,
                        generation=generation, event={"type": "ping"},
                    )
                    checked_at = now
                frame = live.pop_frame()
                if frame:
                    await ws.send_json(live.public_frame(frame))
                else:
                    await asyncio.sleep(0.02)
            await ws.close(code=4409)
        except AgentComputerError:
            await ws.close(code=4409)

    try:
        await ws.send_json(hello)
        if live is not None:
            pump_task = asyncio.create_task(_pump())
        handle = None
        try:
            handle = await asyncio.to_thread(contract.stream_runtime_handle, computer_id)
        except Exception:
            log.warning("stream runtime attach failed computer=%s", computer_id)
            handle = None
        has_cdp = bool(handle is not None and getattr(handle, "cdp_loopback", None))
        log.info("stream runtime computer=%s has_cdp=%s", computer_id, has_cdp)
        if live and has_cdp:
            async def _screencast() -> None:
                try:
                    await run_chromium_screencast(live, handle)
                except Exception:
                    log.warning("screencast ended computer=%s", computer_id)
                    try:
                        await ws.send_json(
                            {"type": "error", "code": 4500, "reason": "screencast", "retry": True}
                        )
                        await ws.close(code=4500)
                    except Exception:
                        pass

            cdp_task = asyncio.create_task(_screencast())
        elif live is not None:
            emit_memory_frame(live, str(hello.get("url") or ""))
        while True:
            message = await ws.receive_json()
            result = await asyncio.to_thread(
                contract.stream_input,
                computer_id,
                principal,
                lease_id=lease_id,
                fencing_epoch=fencing_epoch,
                generation=generation,
                event=message if isinstance(message, dict) else {},
            )
            if isinstance(result, dict) and result.get("kind") == "cursor" and result.get("cursor"):
                await ws.send_json({"type": "cursor", "cursor": result["cursor"]})
            if isinstance(result, dict) and result.get("kind") == "nav":
                await ws.send_json(
                    {
                        "type": "location",
                        "origin": result.get("origin") or "",
                        "url": result.get("url") or "",
                        "title": result.get("title") or "",
                        "https": bool(result.get("https")),
                        "scheme": result.get("scheme") or "",
                    }
                )
    except WebSocketDisconnect:
        pass
    except AgentComputerError:
        try:
            await ws.close(code=4409)
        except Exception:
            pass
    except (ValueError, TypeError):
        await _reject(4400, "bad_event")
    finally:
        if pump_task:
            pump_task.cancel()
        if cdp_task:
            cdp_task.cancel()
        await asyncio.gather(*(task for task in (pump_task, cdp_task) if task), return_exceptions=True)
        await asyncio.to_thread(contract.close_stream, computer_id, generation)


@router.post("/api/agent-computers/{computer_id}/owner-disconnect")
def owner_disconnect(request: Request, computer_id: str):
    try:
        return get_contract().owner_disconnect(computer_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/identities")
def attach_identity(request: Request, computer_id: str, body: AttachBody):
    try:
        return get_contract().attach_identity(computer_id, body.identity_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/browser-identities")
def create_identity(request: Request, body: IdentityCreateBody):
    try:
        return get_contract().create_identity(
            _owner(request), ownership=body.ownership, metadata=body.metadata
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/browser-identities/{identity_id}/revoke")
def revoke_identity(request: Request, identity_id: str):
    try:
        return get_contract().revoke_identity(identity_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.get("/api/agent-computers/{computer_id}/artifacts")
def list_artifacts(request: Request, computer_id: str):
    try:
        return get_contract().list_artifacts(computer_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.get("/api/agent-computers/{computer_id}/artifacts/{name}")
def get_artifact(request: Request, computer_id: str, name: str, folder: str = "downloads"):
    try:
        path = get_contract().artifact_path(
            computer_id, _owner(request), name, folder=folder
        )
        return FileResponse(path, filename=path.name)
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/agent-computers/{computer_id}/workspace-files")
async def put_workspace_file(request: Request, computer_id: str, file: UploadFile = File(...)):
    try:
        data = await file.read()
        return get_contract().put_upload(
            computer_id,
            _owner(request),
            name=file.filename or "upload.bin",
            data=data,
        )
    except AgentComputerError as exc:
        raise _http(exc) from exc


@router.post("/api/checkpoints/{checkpoint_id}/approve")
def approve_checkpoint(request: Request, checkpoint_id: str):
    try:
        return get_contract().approve_checkpoint(checkpoint_id, _owner(request))
    except AgentComputerError as exc:
        raise _http(exc) from exc
