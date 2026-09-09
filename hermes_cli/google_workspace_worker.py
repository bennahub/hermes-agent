"""One isolated Google operation; native module globals never cross profile scopes."""
from __future__ import annotations
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from hermes_cli import google_workspace_onboarding as adapter


def execute(data):
    from hermes_cli.connections import scope_context
    from hermes_cli.config import is_managed
    if not isinstance(data,dict) or set(data)-{'scope','owner_id','action','value','session_id','loopback_port'}:
        raise ValueError('invalid_google_request')
    scope=data.get('scope','default')
    if not isinstance(scope,str):raise ValueError('invalid_google_request')
    with scope_context(scope) as home:
        if is_managed():raise ValueError('invalid_google_request')
        from hermes_constants import get_default_hermes_root
        credential_root = adapter.credential_module().credential_home(home, get_default_hermes_root())
        lock=credential_root/'.google-oauth.lock'
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('google_operation_busy') from None
            os.umask(0o077)
            spec=importlib.util.spec_from_file_location('_hermes_native_google_setup',adapter.native_path())
            native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
            if Path(native.HERMES_HOME).resolve()!=Path(home).resolve():raise ValueError('invalid_google_store')
            native.TOKEN_PATH = credential_root / 'google_token.json'
            native.CLIENT_SECRET_PATH = credential_root / 'google_client_secret.json'
            native.PENDING_AUTH_PATH = credential_root / 'google_oauth_pending.json'
            # Owner-authorized Gmail full mail and basic settings; no other Workspace grants.
            from hermes_cli.google_gmail_verification import SCOPES
            native.SCOPES=list(SCOPES)
            port=data.get('loopback_port')
            if port is not None:
                if data.get('action') != 'start' or type(port) is not int or not 1024 <= port <= 65535:
                    raise ValueError('invalid_google_request')
                client=adapter.validate_client(Path(native.CLIENT_SECRET_PATH).read_text())
                if 'installed' not in client:raise ValueError('invalid_google_client')
                # The authenticated desktop caller must bind this loopback listener before start.
                native.REDIRECT_URI=f'http://127.0.0.1:{port}'
            return adapter.operate(native,data.get('action'),scope=scope,owner_id=data.get('owner_id'),
                                   session_id=data.get('session_id'),value=data.get('value'))
        finally:os.close(fd)


def main():
    payload={'ok':False,'error':'google_runtime_failure'}
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('invalid_google_request')
        data=json.loads(raw)
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            payload=execute(data)
    except (Exception,SystemExit) as exc:
        # Match only enumerated application error tokens, never native text.
        code=str(exc) if isinstance(exc,ValueError) and str(exc) in adapter.ERRORS else 'google_runtime_failure'
        payload={'ok':False,'error':code}
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__=='__main__':raise SystemExit(main())
