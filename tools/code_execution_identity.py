"""Carry exact sandbox request identity across captured cell contexts."""
import contextvars
from contextlib import contextmanager

_REQUEST_ID = contextvars.ContextVar('code_execution_request_identity', default=None)


@contextmanager
def request_identity(sequence):
    if type(sequence) is not int or sequence < 0:
        raise ValueError('Sandbox RPC requires a nonnegative integer sequence')
    token = _REQUEST_ID.set('rpc:' + str(sequence))
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


def consume_request_identity():
    """Only the requested registry invocation owns the RPC identity."""
    value = _REQUEST_ID.get()
    _REQUEST_ID.set(None)
    return value


def run_in_context(context, function, *args, **kwargs):
    """Restore cell authority while preserving this incoming RPC's identity."""
    request_id = _REQUEST_ID.get()
    def invoke():
        token = _REQUEST_ID.set(request_id)
        try:
            return function(*args, **kwargs)
        finally:
            _REQUEST_ID.reset(token)
    return context.copy().run(invoke)
