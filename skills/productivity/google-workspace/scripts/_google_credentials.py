"""Canonical Google credential selection: local override, then installation account.

Only Google credentials and their serialized authorization operation use this
store; other profile state stays local. No token copies or additional database.
"""
from contextlib import contextmanager
import os
from pathlib import Path


def credential_home(home, root):
    home, root = Path(home).absolute(), Path(root).absolute()
    # Reject redirected stores before examining even an existing local token.
    # System ancestors may legitimately be symlinked (e.g. macOS /var), but the
    # installation root and every profile component are explicit trust boundaries.
    if root.is_symlink() or home.is_symlink():
        raise ValueError("invalid_google_store")
    if home != root:
        if home.parent != root / "profiles" or home.parent.is_symlink():
            raise ValueError("invalid_google_store")
        try:
            from hermes_cli.profiles import validate_profile_name
            from hermes_constants import named_profile_is_deleted
        except (ImportError, ModuleNotFoundError):
            import re
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", home.name):
                raise ValueError("invalid_google_store")
            deleted = (root / "profiles/.deleted" / home.name).exists()
        else:
            validate_profile_name(home.name)
            deleted = named_profile_is_deleted(home)
        if deleted or not home.is_dir():
            raise ValueError("invalid_google_store")
    local = home / "google_token.json"
    if local.exists() or local.is_symlink():
        return home
    if home != root and (root / "google_token.json").is_file():
        return root
    return home


def token_path(home, root):
    path = credential_home(home, root) / 'google_token.json'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('invalid_google_store')
    return path


@contextmanager
def credential_lock(home):
    """Serialize refreshes and existing gws credential-file operations."""
    home = Path(home)
    import stat
    lock = home / '.google-oauth.lock'
    if lock.is_symlink():
        raise ValueError('invalid_google_store')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('invalid_google_store')
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_token(path, payload):
    import json
    import tempfile
    path = Path(path)
    if path.is_symlink():
        raise ValueError('invalid_google_store')
    fd, name = tempfile.mkstemp(prefix='.google-token-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
