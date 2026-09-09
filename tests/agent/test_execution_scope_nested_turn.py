"""A child model turn cannot borrow the surrounding parent's tool receipt."""
import json

from agent import execution_scope as scope
from hermes_state import SessionDB


def test_explicit_child_turn_uses_narrowed_scope_even_inside_matching_parent_receipt(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    parent = db.create_or_get_scope('owner:parent', {'id': 'parent'}, {'allowed': ['parent', 'child']})
    child = db.create_or_get_scope('derived:child', {'id': 'child'}, {'allowed': ['child']})
    monkeypatch.setitem(scope._TURNS, 'parent-turn', scope.Binding(db, parent['scope_id']))
    monkeypatch.setitem(scope._TURNS, 'child-turn', scope.Binding(db, child['scope_id']))
    judged, effects = [], []
    def judge(policy, tool, args, **kwargs):
        judged.append((policy['allowed'], args['purpose']))
        return args['purpose'] in policy['allowed'], 'Outside current assignment'
    monkeypatch.setattr(scope, 'judge_action', judge)
    def execute(args):
        effects.append(args['purpose'])
        return 'done'
    def parent_dispatch(args):
        # Same tool + exact args would have consumed parent's unentered receipt.
        for registry in (False, True):
            denied = scope.execute_scoped('terminal', args, execute,
                turn_id='child-turn', invocation_id='child-forbidden', registry=registry)
            assert isinstance(denied, scope.ExecutionScopeDenied) and not denied.uncertain
            assert json.loads(denied)['effect_disposition'] == 'not_started'
        missing = scope.execute_scoped('terminal', args, execute,
            turn_id='unbound-new-turn', invocation_id='missing', registry=True)
        assert json.loads(missing)['effect_disposition'] == 'not_started'
        assert scope.execute_scoped('terminal', {'purpose': 'child'}, execute,
            turn_id='child-turn', invocation_id='child-allowed') == 'done'
        # Exact parent registry continuation still consumes its original receipt.
        return scope.execute_scoped('terminal', args, execute,
            turn_id='parent-turn', invocation_id='parent-call', registry=True)
    try:
        assert scope.execute_scoped('terminal', {'purpose': 'parent'}, parent_dispatch,
            turn_id='parent-turn', invocation_id='parent-call') == 'done'
        assert effects == ['child', 'parent']
        assert judged == [(['parent', 'child'], 'parent'), (['child'], 'parent'),
                          (['child'], 'parent'), (['child'], 'child')]
        assert db.get_scope_action(child['scope_id'], 'turn:child-turn:call:child-allowed')['state'] == 'completed'
        assert db.get_scope_action(parent['scope_id'], 'turn:parent-turn:call:parent-call')['state'] == 'completed'
    finally:
        db.close()
