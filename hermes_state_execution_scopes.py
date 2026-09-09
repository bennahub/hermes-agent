"""Durable identities for native execution authority and consume-once tool calls.

Semantic admission belongs to the history-isolated policy caller. This ledger
never derives authority from transcript text and never retries an admitted call.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid


EXECUTION_SCOPE_SQL = """
CREATE TABLE IF NOT EXISTS execution_scopes (
    scope_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    source_json TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'closed')),
    created_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS execution_scope_actions (
    scope_id TEXT NOT NULL REFERENCES execution_scopes(scope_id),
    invocation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    arguments_json TEXT,
    attempt_id TEXT NOT NULL DEFAULT '',
    uncertain INTEGER NOT NULL DEFAULT 0 CHECK(uncertain IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN ('admitted', 'completed')),
    result_json TEXT,
    admitted_at REAL NOT NULL,
    completed_at REAL,
    PRIMARY KEY(scope_id, invocation_id)
);
"""


_UNRESOLVED_SCOPE_ACTION_SQL = (
    "SELECT invocation_id, attempt_id, uncertain FROM execution_scope_actions "
    "WHERE scope_id=? AND state='admitted' AND (uncertain=1 OR attempt_id<>?) LIMIT 1"
)


def _identity(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 2048:
        raise ValueError(f'{label} must be a nonempty identity of at most 2048 bytes')
    return value


def _encode(value, limit=262144):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > limit:
        raise ValueError('execution scope value exceeds its serialized size limit')
    return encoded


def _scope_row(row):
    if row is None:
        return None
    result = dict(row)
    result['source'] = json.loads(result.pop('source_json'))
    result['scope'] = json.loads(result.pop('scope_json'))
    return result


def _action_scope_lineage(conn, scope_id: str) -> list[str]:
    """Only immutable native amendment links carry prior invocation facts."""
    lineage = []
    while scope_id:
        if scope_id in lineage:
            raise ValueError("Cyclic execution amendment lineage")
        row = conn.execute("SELECT source_json FROM execution_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if row is None:
            raise ValueError("Execution amendment lineage is unavailable")
        lineage.append(scope_id)
        source = json.loads(row["source_json"])
        if source.get("kind") != "owner_amendment":
            break
        scope_id = _identity(source.get("prior_scope_id"), "prior_scope_id")
    return lineage


class SessionExecutionScopesMixin:
    """Uses SessionDB's native transaction/replacement guards and reader pool."""

    def create_or_get_scope(self, source_key: str, source: dict, scope: dict) -> dict:
        """Freeze one scope per trusted original source, including after closure.

        Callers must supply original ingress/work identity, never reconstructed
        message position or content matching. Reuse may not broaden the scope.
        """
        _identity(source_key, 'source_key')
        if not isinstance(source, dict) or not source or not isinstance(scope, dict) or not scope:
            raise ValueError('source and scope must be nonempty structured objects')
        source_json, scope_json = _encode(source), _encode(scope)
        def write(conn):
            row = conn.execute('SELECT * FROM execution_scopes WHERE source_key=?', (source_key,)).fetchone()
            if row is not None:
                if row['source_json'] != source_json or row['scope_json'] != scope_json:
                    raise ValueError('original authority source cannot be rebound or expanded')
                return _scope_row(row)
            parent_id = source.get('parent_scope_id')
            if parent_id is not None:
                parent = conn.execute('SELECT state FROM execution_scopes WHERE scope_id=?', (parent_id,)).fetchone()
                if parent is None or parent['state'] != 'active':
                    raise ValueError('Parent execution scope is no longer active')
            scope_id = uuid.uuid4().hex
            conn.execute('INSERT INTO execution_scopes VALUES (?, ?, ?, ?, ?, ?, NULL)',
                         (scope_id, source_key, source_json, scope_json, 'active', time.time()))
            return _scope_row(conn.execute('SELECT * FROM execution_scopes WHERE scope_id=?', (scope_id,)).fetchone())
        return self._execute_write(write)

    def close_scope_tree_and_has_actions(self, scope_id: str) -> bool:
        """Fence this exact assignment and descendants before observing admission."""
        _identity(scope_id, 'scope_id')
        def write(conn):
            rows = conn.execute("""WITH RECURSIVE assigned(scope_id) AS (
                SELECT scope_id FROM execution_scopes WHERE scope_id=?
                UNION SELECT s.scope_id FROM execution_scopes s JOIN assigned a
                  ON json_extract(s.source_json, '$.parent_scope_id')=a.scope_id
                ) SELECT scope_id FROM assigned""", (scope_id,)).fetchall()
            ids = [row['scope_id'] for row in rows]
            admitted = False
            for assigned_id in ids:
                conn.execute("UPDATE execution_scopes SET state='closed',closed_at=? WHERE scope_id=? AND state='active'",
                             (time.time(), assigned_id))
                admitted = admitted or conn.execute('SELECT 1 FROM execution_scope_actions WHERE scope_id=? LIMIT 1',
                                                     (assigned_id,)).fetchone() is not None
            return admitted
        return self._execute_write(write)

    def get_scope(self, scope_id: str):
        _identity(scope_id, 'scope_id')
        with self._read_ctx() as conn:
            return _scope_row(conn.execute('SELECT * FROM execution_scopes WHERE scope_id=?', (scope_id,)).fetchone())

    def scope_has_actions(self, scope_id: str, *, attempt_id: str) -> bool:
        """Read whether this exact current attempt has a durable action admission."""
        _identity(scope_id, 'scope_id')
        _identity(attempt_id, 'attempt_id')
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT 1 FROM execution_scope_actions "
                "WHERE scope_id=? AND attempt_id=? LIMIT 1",
                (scope_id, attempt_id),
            ).fetchone()
        return row is not None

    def get_scope_for_source(self, source_key: str):
        """Reuse the frozen policy without deriving it again on continuation."""
        _identity(source_key, 'source_key')
        with self._read_ctx() as conn:
            return _scope_row(conn.execute('SELECT * FROM execution_scopes WHERE source_key=?', (source_key,)).fetchone())

    def close_scope(self, scope_id: str) -> dict:
        """Closure is irreversible; in-flight results can still be recorded."""
        _identity(scope_id, 'scope_id')
        def write(conn):
            conn.execute("UPDATE execution_scopes SET state='closed', closed_at=? WHERE scope_id=? AND state='active'",
                         (time.time(), scope_id))
            row = conn.execute('SELECT * FROM execution_scopes WHERE scope_id=?', (scope_id,)).fetchone()
            if row is None:
                raise ValueError('unknown execution scope')
            return _scope_row(row)
        return self._execute_write(write)

    def get_scope_action(self, scope_id: str, invocation_id: str, *, include_amendments: bool = False):
        """Read an existing invocation without granting a new admission."""
        _identity(scope_id, 'scope_id')
        _identity(invocation_id, 'invocation_id')
        with self._read_ctx() as conn:
            ids = _action_scope_lineage(conn, scope_id) if include_amendments else [scope_id]
            row = None
            for candidate in ids:
                row = conn.execute('SELECT * FROM execution_scope_actions WHERE scope_id=? AND invocation_id=?',
                                   (candidate, invocation_id)).fetchone()
                if row is not None:
                    break
        if row is None:
            return None
        result = dict(row)
        result['result'] = json.loads(result.pop('result_json')) if row['state'] == 'completed' else None
        return result

    def get_scope_observations(self, scope_id: str) -> list[dict]:
        """Bounded resolved receipts from this exact active scope, never history.

        Legacy calls have no stored arguments and cannot establish observation
        provenance. Large receipts are omitted whole, not misleadingly truncated.
        """
        _identity(scope_id, 'scope_id')
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT a.invocation_id, a.tool_name, a.arguments_json, a.result_json "
                "FROM execution_scope_actions a JOIN execution_scopes s ON s.scope_id=a.scope_id "
                "WHERE a.scope_id=? AND s.state='active' AND a.state='completed' "
                "AND a.uncertain=0 AND a.arguments_json IS NOT NULL "
                "ORDER BY a.rowid DESC LIMIT 16", (scope_id,),
            ).fetchall()
        observations, remaining = [], 65536
        for row in rows:
            if len(row['arguments_json'].encode('utf-8')) + len(row['result_json'].encode('utf-8')) > remaining:
                continue
            item = {'invocation_id': row['invocation_id'], 'tool': row['tool_name'],
                    'arguments': json.loads(row['arguments_json']),
                    'result': json.loads(row['result_json'])}
            try:
                size = len(_encode(item, limit=remaining).encode('utf-8'))
            except ValueError:
                continue  # Informational receipts must never strand active work.
            if size <= remaining:
                observations.append(item)
                remaining -= size
        return list(reversed(observations))

    def get_scope_uncertainty(self, scope_id: str, attempt_id: str):
        """Read known uncertainty before policy; admission rechecks atomically."""
        _identity(scope_id, 'scope_id')
        _identity(attempt_id, 'attempt_id')
        with self._read_ctx() as conn:
            row = None
            for candidate in _action_scope_lineage(conn, scope_id):
                row = conn.execute(_UNRESOLVED_SCOPE_ACTION_SQL, (candidate, attempt_id)).fetchone()
                if row is not None:
                    break
        return dict(row) if row is not None else None

    def claim_scope_action(self, scope_id: str, invocation_id: str, tool_name: str, arguments: dict,
                           *, attempt_id: str = 'native-programmatic') -> dict:
        """Commit admission BEFORE invoking a tool, only after policy acceptance.

        An admitted call with no durable result is uncertain forever; restart or
        duplicate delivery cannot reacquire it. An uncertain action or an
        unresolved prior attempt fences all new invocation IDs in this scope.
        Concurrent siblings within one native attempt remain independent.
        """
        for value, label in ((scope_id, 'scope_id'), (invocation_id, 'invocation_id'), (tool_name, 'tool_name')):
            _identity(value, label)
        if not isinstance(arguments, dict):
            raise ValueError('tool arguments must be a structured object')
        _identity(attempt_id, 'attempt_id')
        arguments_json = _encode(arguments)
        digest = hashlib.sha256(arguments_json.encode('utf-8')).hexdigest()
        def write(conn):
            scope = conn.execute('SELECT state FROM execution_scopes WHERE scope_id=?', (scope_id,)).fetchone()
            if scope is None or scope['state'] != 'active':
                raise ValueError('execution scope is not active')
            lineage = _action_scope_lineage(conn, scope_id)
            for candidate in lineage:
                row = conn.execute('SELECT * FROM execution_scope_actions WHERE scope_id=? AND invocation_id=?',
                                   (candidate, invocation_id)).fetchone()
                if row is not None:
                    if row['tool_name'] != tool_name or row['arguments_digest'] != digest:
                        raise ValueError('invocation identity cannot be reused for a different action')
                    if row['state'] == 'completed':
                        return {'status': 'completed', 'result': json.loads(row['result_json'])}
                    return {'status': 'uncertain'}
            for candidate in lineage:
                unresolved = conn.execute(_UNRESOLVED_SCOPE_ACTION_SQL, (candidate, attempt_id)).fetchone()
                if unresolved is not None:
                    return {'status': 'uncertain', 'unresolved_invocation_id': unresolved['invocation_id']}
            conn.execute(
                'INSERT INTO execution_scope_actions '
                '(scope_id, invocation_id, tool_name, arguments_digest, arguments_json, attempt_id, uncertain, state, admitted_at) '
                'VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)',
                (scope_id, invocation_id, tool_name, digest, arguments_json, attempt_id, 'admitted', time.time()))
            return {'status': 'claimed'}
        return self._execute_write(write)

    def mark_scope_action_uncertain(self, scope_id: str, invocation_id: str) -> None:
        """Effect outcome was lost; new calls cannot bypass it with fresh IDs."""
        _identity(scope_id, 'scope_id')
        _identity(invocation_id, 'invocation_id')
        self._execute_write(lambda conn: conn.execute(
            "UPDATE execution_scope_actions SET uncertain=1 WHERE scope_id=? AND invocation_id=? AND state='admitted'",
            (scope_id, invocation_id)))

    def mark_scope_attempt_uncertain(self, scope_id: str, attempt_id: str) -> int:
        """Timeout/interruption fences only unresolved actions in this native turn."""
        _identity(scope_id, 'scope_id')
        _identity(attempt_id, 'attempt_id')
        def write(conn):
            count = 0
            for candidate in _action_scope_lineage(conn, scope_id):
                count += conn.execute(
                    "UPDATE execution_scope_actions SET uncertain=1 WHERE scope_id=? AND attempt_id=? AND state='admitted'",
                    (candidate, attempt_id)).rowcount
            return count
        return self._execute_write(write)

    def complete_scope_action(self, scope_id: str, invocation_id: str, result) -> dict:
        """Record the exact serializable tool result; conflicting completion fails."""
        _identity(scope_id, 'scope_id')
        _identity(invocation_id, 'invocation_id')
        result_json = _encode(result, limit=4 * 1024 * 1024)
        def write(conn):
            row = conn.execute('SELECT * FROM execution_scope_actions WHERE scope_id=? AND invocation_id=?',
                               (scope_id, invocation_id)).fetchone()
            if row is None:
                raise ValueError('action was never admitted')
            if row['state'] == 'completed' and row['result_json'] != result_json:
                raise ValueError('completed action result is immutable')
            conn.execute("UPDATE execution_scope_actions SET state='completed', uncertain=0, result_json=?, completed_at=? "
                         "WHERE scope_id=? AND invocation_id=? AND state='admitted'",
                         (result_json, time.time(), scope_id, invocation_id))
            return {'status': 'completed', 'result': json.loads(result_json)}
        return self._execute_write(write)
