"""Canonical Google Workspace skill adapter; execute in an isolated worker.

Does not introduce an auth store. Existing google_client_secret.json,
google_oauth_pending.json and google_token.json remain the only persistence.
HTTP routing and owner authentication belong to the caller; the isolated worker
owns the per-scope operation lock and restrictive creation permissions.
"""
from contextlib import redirect_stdout
from io import StringIO
import json
from functools import lru_cache
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

MAX_CLIENT_JSON = 32768


def status(home):
    home = Path(home)
    return {"client_configured": (home / "google_client_secret.json").is_file(),
            "credential_present": (home / "google_token.json").is_file(),
            "pending": (home / "google_oauth_pending.json").is_file()}


def validate_client(value):
    if not isinstance(value, str) or len(value) > MAX_CLIENT_JSON:
        raise ValueError("invalid_google_client")
    try:
        raw = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_google_client") from None
    if not isinstance(raw, dict) or len(set(raw) & {"web", "installed"}) != 1:
        raise ValueError("invalid_google_client")
    client = raw.get("installed") or raw.get("web")
    if not isinstance(client, dict):
        raise ValueError("invalid_google_client")
    if not isinstance(client.get("client_id"), str) or not client["client_id"].endswith(".apps.googleusercontent.com"):
        raise ValueError("invalid_google_client")
    if not isinstance(client.get("client_secret"), str) or not client["client_secret"]:
        raise ValueError("invalid_google_client")
    # The native Flow must never send code/client secret to caller-selected hosts.
    for key, allowed in (("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
                         ("token_uri", "https://oauth2.googleapis.com/token")):
        if client.get(key) != allowed:
            raise ValueError("invalid_google_client_endpoint")
    return raw


def save_client(native, value):
    """Use native store_client_secret after validation, no shell arguments."""
    raw = validate_client(value)
    target = Path(native.CLIENT_SECRET_PATH)
    if target.is_symlink():
        raise ValueError("invalid_google_store")
    with tempfile.TemporaryDirectory(prefix="hermes-google-client-") as directory:
        src = Path(directory) / "client.json"
        src.write_text(json.dumps(raw), encoding="utf-8")
        src.chmod(0o600)
        with redirect_stdout(StringIO()):
            native.store_client_secret(str(src))
    target.chmod(0o600)
    return {"ok": True, "detail_code": "google_client_saved"}


def begin(native):
    if not Path(native.CLIENT_SECRET_PATH).is_file():
        return {"ok": False, "error": "google_client_required"}
    # Never install dependencies implicitly as part of an Owner auth click.
    if native._missing_required_packages():
        return {"ok": False, "error": "google_runtime_dependencies_required"}
    if Path(native.PENDING_AUTH_PATH).is_symlink():
        raise ValueError("invalid_google_store")
    output = StringIO()
    with redirect_stdout(output):
        native.get_auth_url()
    url = output.getvalue().strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "accounts.google.com" or parts.username or parts.password:
        raise ValueError("invalid_google_authorization_url")
    pending = json.loads(Path(native.PENDING_AUTH_PATH).read_text())
    if parse_qs(parts.query).get("state") != [pending.get("state")]:
        raise ValueError("invalid_google_pending")
    Path(native.PENDING_AUTH_PATH).chmod(0o600)
    return {"ok": True, "verification_uri": url, "flow": "manual_code",
            "requested_scopes": list(native.SCOPES), "expires_in": 600}


def submit(native, callback):
    """Require full native callback URL and state, not unauthenticated raw code."""
    if not isinstance(callback, str) or len(callback) > 16384:
        raise ValueError("invalid_google_callback")
    pending = json.loads(Path(native.PENDING_AUTH_PATH).read_text())
    parts = urlsplit(callback)
    expected = urlsplit(pending.get("redirect_uri") or native.REDIRECT_URI)
    if not _callback_location_matches(parts, expected):
        raise ValueError("invalid_google_callback")
    query = parse_qs(parts.query, keep_blank_values=True)
    if query.get("state") != [pending.get("state")] or len(query.get("code", [])) != 1 or not query["code"][0]:
        raise ValueError("invalid_google_callback_state")
    if Path(native.TOKEN_PATH).is_symlink():
        raise ValueError("invalid_google_store")
    target=Path(native.TOKEN_PATH)
    from hermes_cli.google_gmail_verification import verify, META as VERIFY_META
    expected_account=None
    if target.is_file():
        existing=json.loads(target.read_text())
        expected_account=existing.get(VERIFY_META,{}).get('account')
    # A partial/failed reconsent must never replace the working credential.
    with tempfile.TemporaryDirectory(prefix='.google-exchange-',dir=target.parent) as directory:
        staged=Path(directory)/'google_token.json'
        native.TOKEN_PATH=staged
        try:
            with redirect_stdout(StringIO()):
                # Callback scope is never authority to widen the approved Flow.
                cleaned=parts._replace(query=urlencode([(k,v) for k,v in parse_qsl(parts.query) if k != 'scope']))
                native.exchange_auth_code(urlunsplit(cleaned), require_granted_scopes=True)
            proof=verify(staged)
            if expected_account and proof['account'].casefold()!=expected_account.casefold():
                raise ValueError('google_account_mismatch')
            os.replace(staged,target)
        finally:
            native.TOKEN_PATH=target
    target.chmod(0o600)
    return {"ok": True, "detail_code": "google_credential_saved"}


def cancel(native):
    pending = Path(native.PENDING_AUTH_PATH)
    if pending.is_symlink():
        raise ValueError("invalid_google_store")
    pending.unlink(missing_ok=True)
    return {"ok": True, "detail_code": "cancelled"}

# Operation bookkeeping lives with the native pending authorization; no alternate
# client/token store. Only a trusted authenticated router supplies owner_id.
TTL = 600
META = '_hermes_operation'
ERRORS = frozenset({'invalid_google_client','invalid_google_client_endpoint',
 'invalid_google_store','invalid_google_authorization_url','invalid_google_pending',
 'invalid_google_callback','invalid_google_callback_state','google_client_required',
 'google_runtime_dependencies_required','google_operation_busy','google_operation_not_found',
 'google_operation_forbidden','google_operation_expired','google_operation_consumed',
 'google_operation_failed','google_runtime_failure','invalid_google_request','google_credential_required',
 'google_refresh_required','google_verification_failed','google_account_mismatch'})


def _safe_store(native):
    for attr in ('CLIENT_SECRET_PATH','TOKEN_PATH','PENDING_AUTH_PATH'):
        path = Path(getattr(native,attr))
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('invalid_google_store')


def _write_pending(native, value):
    target=Path(native.PENDING_AUTH_PATH)
    _safe_store(native)
    fd, name=tempfile.mkstemp(prefix='.google-pending-',dir=target.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)


def _pending(native):
    path=Path(native.PENDING_AUTH_PATH)
    if not path.exists():return None
    try:
        if path.stat().st_size>65536:raise ValueError()
        result=json.loads(path.read_text())
        if not isinstance(result,dict):raise ValueError()
        return result
    except (ValueError,OSError):raise ValueError('invalid_google_pending') from None


def _owner(owner_id):
    import hashlib
    if not isinstance(owner_id,str) or not owner_id or len(owner_id)>512:
        raise ValueError('invalid_google_request')
    return hashlib.sha256(owner_id.encode()).hexdigest()


def _public(meta,now):
    result={'ok':True,'session_id':meta['id'],'status':meta['status'],
            'expires_in':max(0,int(meta['expires_at']-now))}
    if meta['status']=='pending':
        result.update(flow='manual_code',verification_uri=meta['verification_uri'],
                      requested_scopes=meta['requested_scopes'])
    return result


def operate(native, action, *, scope, owner_id, session_id=None, value=None, now=None):
    """Call only inside the worker's per-scope lock and restrictive umask."""
    import secrets
    now=time.time() if now is None else now
    owner=_owner(owner_id);_safe_store(native)
    pending=_pending(native)
    meta=pending.get(META) if pending else None
    if action=='test':
        from hermes_cli.google_gmail_verification import verify
        try:return verify(native.TOKEN_PATH)
        except Exception as exc:
            code=str(exc) if isinstance(exc,ValueError) and str(exc) in ERRORS else 'google_verification_failed'
            raise ValueError(code) from None
    if action=='disconnect':
        # Revoke the stored grant and delete only the token. The admin OAuth
        # client stays; other Google accounts are not touched because this
        # store is one token file.
        token=Path(native.TOKEN_PATH)
        if token.is_symlink():
            raise ValueError('invalid_google_store')
        try:
            native.revoke()
        except Exception:
            token.unlink(missing_ok=True)
        return {'ok': True, 'detail_code': 'disconnected'}
    if action=='status':
        result=status(Path(native.CLIENT_SECRET_PATH).parent)
        result['pending']=bool(isinstance(meta,dict) and meta.get('expires_at',0)>now and meta.get('status')=='pending')
        return {'ok':True,**result,'requested_scopes':list(native.SCOPES)}
    if action in ('save_client','start'):
        # Never overwrite another pending native CLI authorization or another
        # authenticated owner's operation. Expired owned metadata may be replaced.
        if pending and not isinstance(meta,dict):raise ValueError('google_operation_busy')
        if meta and meta.get('expires_at',0)>now:
            if action=='start' and meta.get('owner')==owner and meta.get('scope')==scope and meta.get('status')=='pending':
                return _public(meta,now)
            if meta.get('status')=='pending':raise ValueError('google_operation_busy')
        if action=='save_client':return save_client(native,value)
        if Path(native.CLIENT_SECRET_PATH).exists():validate_client(Path(native.CLIENT_SECRET_PATH).read_text())
        started=begin(native)
        if not started['ok']:return started
        pending=_pending(native)
        meta={'id':secrets.token_urlsafe(24),'owner':owner,'scope':scope,'created_at':now,
              'expires_at':now+TTL,'status':'pending','verification_uri':started['verification_uri'],
              'requested_scopes':started['requested_scopes']}
        pending[META]=meta;_write_pending(native,pending)
        return _public(meta,now)
    if action not in ('poll','submit','cancel'):raise ValueError('invalid_google_request')
    if not isinstance(meta,dict) or not isinstance(session_id,str) or not secrets.compare_digest(str(meta.get('id','')),session_id):
        raise ValueError('google_operation_not_found')
    if not secrets.compare_digest(str(meta.get('owner','')),owner) or meta.get('scope')!=scope:
        raise ValueError('google_operation_forbidden')
    if meta.get('expires_at',0)<=now:
        # Remove state/PKCE, retain a bounded marker for an honest expired poll.
        meta={k:v for k,v in meta.items() if k not in ('verification_uri','requested_scopes')}
        meta['status']='expired';_write_pending(native,{META:meta})
        if action=='poll':return _public(meta,now)
        raise ValueError('google_operation_expired')
    if action=='poll':return _public(meta,now)
    if action=='cancel':
        cancel(native)
        return {'ok':True,'session_id':session_id,'status':'cancelled'}
    if meta['status']!='pending':raise ValueError('google_operation_consumed')
    # Validate before consuming an attempt; a malformed paste can be corrected.
    _validate_callback(native,value,pending)
    if native._missing_required_packages():raise ValueError('google_runtime_dependencies_required')
    validate_client(Path(native.CLIENT_SECRET_PATH).read_text())
    # Persist consumption before network exchange; a crash/timeout cannot replay
    # an uncertain authorization code. Native helper still reads PKCE fields.
    meta['status']='exchanging';pending[META]=meta;_write_pending(native,pending)
    try:
        result=submit(native,value)
    except (Exception,SystemExit):
        meta={k:v for k,v in meta.items() if k not in ('verification_uri','requested_scopes')}
        meta['status']='failed';_write_pending(native,{META:meta})
        raise ValueError('google_operation_failed') from None
    meta={k:v for k,v in meta.items() if k not in ('verification_uri','requested_scopes')}
    meta['status']='completed';_write_pending(native,{META:meta})
    return {'ok':True,'session_id':session_id,'status':'completed',**result}


def _callback_location_matches(parts, expected):
    if parts.fragment or (parts.scheme, parts.netloc) != (expected.scheme, expected.netloc):
        return False
    # Browsers serialize the native localhost root with a slash. Do not relax
    # origin matching or normalize arbitrary callback paths/redirect endpoints.
    if expected.scheme == 'http' and expected.hostname in ('localhost', '127.0.0.1'):
        if expected.path in ('', '/') and parts.path in ('', '/'):
            return True
    return parts.path == expected.path


def _validate_callback(native,callback,pending):
    if not isinstance(callback,str) or len(callback)>16384:raise ValueError('invalid_google_callback')
    parts=urlsplit(callback);expected=urlsplit(pending.get('redirect_uri') or native.REDIRECT_URI)
    if not _callback_location_matches(parts,expected):
        raise ValueError('invalid_google_callback')
    query=parse_qs(parts.query,keep_blank_values=True)
    if query.get('state')!=[pending.get('state')] or len(query.get('code',[]))!=1 or not query['code'][0]:
        raise ValueError('invalid_google_callback_state')


def native_path():
    import hermes_constants
    from hermes_constants import get_bundled_skills_dir
    return get_bundled_skills_dir(Path(hermes_constants.__file__).resolve().parent/'skills')/'productivity/google-workspace/scripts/setup.py'


@lru_cache(maxsize=1)
def credential_module():
    import importlib.util
    spec=importlib.util.spec_from_file_location('_hermes_google_credentials', native_path().with_name('_google_credentials.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=4)
def _native_constants(path):
    import ast
    tree=ast.parse(Path(path).read_text())
    return {n.targets[0].id:ast.literal_eval(n.value) for n in tree.body
            if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)
            and n.targets[0].id in ('SCOPES','REQUIRED_PACKAGES')}


def describe(home):
    """Read-only safe inventory projection; no refresh, network or setup import."""
    from importlib.metadata import version
    try:
        constants=_native_constants(str(native_path()))
        available=True
        for spec in constants['REQUIRED_PACKAGES']:
            name,_,wanted=spec.partition('==')
            try:available=available and version(name)==wanted
            except Exception:available=False
        home=Path(home)
        from hermes_cli.google_gmail_verification import SCOPES,receipt
        from hermes_constants import get_default_hermes_root
        root=get_default_hermes_root()
        store=credential_module()
        selected=store.credential_home(home,root)
        verified=receipt(store.token_path(home,root))
        present=lambda name:(selected/name).is_file() and not (selected/name).is_symlink()
        return {'client_present':present('google_client_secret.json'),'token_present':store.token_path(home,root).is_file(), 'shared':selected != home,
                'dependencies_available':available,'requested_scopes':list(SCOPES),'verification':verified}
    except Exception:
        return {'client_present':False,'token_present':False,'dependencies_available':False,'requested_scopes':[],'verification':None}


def run_operation(scope,owner_id,action,*,value=None,session_id=None,loopback_port=None,account=None):
    """Trusted router boundary. Values go through stdin, never shell/argv/logs."""
    import subprocess
    import sys
    import hermes_cli
    from hermes_constants import get_default_hermes_root
    bootstrap=('import sys;sys.path[:0]='+repr(list(sys.path))+
               ';import hermes_cli;hermes_cli.__path__[:0]='+repr(list(hermes_cli.__path__))+
               ';from hermes_cli.google_workspace_worker import main;raise SystemExit(main())')
    data={'scope':scope,'owner_id':owner_id,'action':action,'value':value,'session_id':session_id,'loopback_port':loopback_port}
    if account:
        data['account']=account
    encoded=json.dumps(data).encode()
    if len(encoded)>65536:return {'ok':False,'error':'invalid_google_request'}
    env=dict(os.environ);env['HERMES_HOME']=str(get_default_hermes_root())
    try:
        result=subprocess.run([sys.executable,'-c',bootstrap],input=encoded,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,env=env,timeout=60,check=False)
        if len(result.stdout)>32768:raise ValueError()
        payload=json.loads(result.stdout)
        if not isinstance(payload,dict) or type(payload.get('ok')) is not bool:raise ValueError()
        if not payload['ok'] and payload.get('error') not in ERRORS:raise ValueError()
        return payload
    except Exception:return {'ok':False,'error':'google_runtime_failure'}
