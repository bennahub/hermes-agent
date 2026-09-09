"""Transport retries reuse identity; distinct RPC requests retain autonomy."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import execution_scope as scope
from hermes_state import SessionDB
from tools.code_execution_rpc import _default_dispatch, _handle_rpc_request
from tools.code_kernel import CellAuthority
from tools.registry import registry


@pytest.mark.parametrize('persistent', [False, True])
def test_rpc_sequence_retries_are_consume_once_per_native_cell(tmp_path, monkeypatch, persistent):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    record = db.create_or_get_scope('original:rpc', {'message_id': 1}, {'objective': 'test'})
    binding = scope.Binding(db, record['scope_id'])
    monkeypatch.setitem(scope._TURNS, 'rpc-turn', binding)
    monkeypatch.setattr(scope, 'judge_action', lambda *a, **kw: (True, 'test policy'))
    effects, dispatchers = [], []
    name = 'rpc_identity_effect'
    registry.register(name=name, toolset='test', schema={'name': name, 'parameters': {'type': 'object'}},
                      handler=lambda args, **kw: effects.append(args.copy()) or json.dumps({'result': len(effects)}))
    def cell(_args):
        dispatchers.append(CellAuthority('task').dispatch if persistent else _default_dispatch('task'))
        return '{}'
    try:
        for turn_id, cell_id in (('rpc-turn', 'cell-one'), ('rpc-turn', 'cell-two'), ('next-turn', 'cell-one')):
            monkeypatch.setitem(scope._TURNS, turn_id, binding)
            scope.execute_scoped('execute_code', {'code': 'test'}, cell,
                                 turn_id=turn_id, invocation_id=cell_id)
            counter, log = [0], []
            def request(seq, value):
                return _handle_rpc_request({'tool': name, 'args': {'value': value}, 'seq': seq},
                    allowed_tools=frozenset({name}), tool_call_counter=counter, max_tool_calls=20,
                    dispatch=dispatchers[-1], tool_call_log=log, call_start=time.monotonic(), where='test')
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(request, 1, 'same').result(timeout=10)
                replay = pool.submit(request, 1, 'same').result(timeout=10)
                conflict = pool.submit(request, 1, 'changed').result(timeout=10)
                next_request = pool.submit(request, 2, 'same').result(timeout=10)
            assert first == replay
            assert json.loads(conflict)['effect_disposition'] == 'not_started'
            assert json.loads(next_request)['result'] == json.loads(first)['result'] + 1
        assert effects == [{'value': 'same'}] * 6
    finally:
        registry._tools.pop(name, None)
        db.close()
