"""Real registry/RPC seams keep frozen authority through nested execution."""
import json
from concurrent.futures import ThreadPoolExecutor

from hermes_state import SessionDB
from agent import execution_scope as authority
from model_tools import handle_function_call
from tools.registry import registry
from tools.code_execution_rpc import _default_dispatch
from tools.code_kernel import CellAuthority


def test_registry_receipt_is_once_and_rpc_workers_keep_frozen_scope(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB()
    record = db.create_or_get_scope('owner:session:1', {'message_id': 1}, {'objective': 'finance'})
    binding = authority.Binding(db, record['scope_id'])
    monkeypatch.setitem(authority._TURNS, 'scope-test-turn', binding)
    decisions, effects, workers = [], [], []
    def judge(scope, name, arguments, **kwargs):
        decisions.append((scope, name, arguments.copy()))
        return arguments.get('purpose') == 'finance', 'Outside current financial work'
    monkeypatch.setattr(authority, 'judge_action', judge)
    name = 'scope_nested_probe'
    def handler(args, **kwargs):
        effects.append(args.copy())
        if args.get('capture'):
            workers.extend((_default_dispatch('task'), CellAuthority('task')))
        return json.dumps({'ok': True})
    registry.register(name=name, toolset='test', schema={'name': name, 'parameters': {'type': 'object'}}, handler=handler)
    root_args = {'purpose': 'finance', 'capture': True}
    try:
        result = authority.execute_scoped(name, root_args,
            lambda args: handle_function_call(name, args, turn_id='scope-test-turn', tool_call_id='root-call'),
            turn_id='scope-test-turn', invocation_id='root-call')
        assert json.loads(result)['ok'] is True
        assert len(decisions) == len(effects) == 1
        # Both transports cross a fresh thread. Equal nested arguments are NEW
        # invocations; the consumed root receipt must not exempt their judgment.
        for dispatch in (workers[0], workers[1].dispatch):
            with ThreadPoolExecutor(max_workers=1) as pool:
                denied = pool.submit(dispatch, name, {'purpose': 'historical probe'}).result(timeout=10)
                allowed = pool.submit(dispatch, name, {'purpose': 'finance'}).result(timeout=10)
            assert json.loads(denied)['effect_disposition'] == 'not_started'
            assert json.loads(allowed)['ok'] is True
        assert json.loads(workers[0](name, root_args))['ok'] is True
        assert len(effects) == 4 and len(decisions) == 6
        assert all(item[0] == record['scope'] for item in decisions)
        # Retired durable authority is rechecked even by captured live workers.
        db.close_scope(binding.scope_id)
        assert json.loads(workers[0](name, {'purpose': 'finance'}))['effect_disposition'] == 'not_started'
        assert len(effects) == 4
    finally:
        registry._tools.pop(name, None)
        db.close()


def test_registry_model_turn_without_scope_runs_without_recording(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    effects = []
    name = 'scope_missing_probe'
    registry.register(name=name, toolset='test', schema={'name': name, 'parameters': {'type': 'object'}},
                      handler=lambda args, **kwargs: effects.append(args) or '{}')
    try:
        result = handle_function_call(name, {}, turn_id='unbound-original-turn', tool_call_id='new-call')
        # Unbound turns run: with no scope to admit against, the call executes
        # directly and nothing is recorded for the turn.
        assert json.loads(result) == {}
        assert effects == [{}]
        assert authority.get_binding('unbound-original-turn') is None
    finally:
        registry._tools.pop(name, None)
