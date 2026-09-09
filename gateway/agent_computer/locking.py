"""Reuse Hermes' bounded native file lock for cross-process control fencing."""

from pathlib import Path
import threading


class ComputerOperationLock:
    """Reentrant across service calls; shared by every process using the store."""

    def __init__(self, path: Path):
        self.path = path
        self.thread_lock = threading.RLock()
        self.holder = threading.local()
        self.contexts = threading.local()

    def __enter__(self):
        from hermes_cli import auth

        self.thread_lock.acquire()
        try:
            if auth.fcntl is None and auth.msvcrt is None:
                raise RuntimeError("computer control requires a supported process lock")
            ctx = auth._file_lock(self.path, self.holder, 45, "computer control is busy")
            ctx.__enter__()
            stack = getattr(self.contexts, "stack", None)
            if stack is None:
                stack = self.contexts.stack = []
            stack.append(ctx)
            return self
        except BaseException:
            self.thread_lock.release()
            raise

    def __exit__(self, *exc):
        try:
            return self.contexts.stack.pop().__exit__(*exc)
        finally:
            self.thread_lock.release()
