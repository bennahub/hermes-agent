"""Backup-backed, idempotent metadata migration; never adjudicates owner work."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.autonomy.lifecycle import preserve


def migrate(root, backup_dir=None):
    paths = sorted([p for p in [root / 'autonomy/work.db',
                   *root.glob('profiles/*/autonomy/work.db')] if p.is_file()])
    results = []
    for path in paths:
        conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            conn.execute('BEGIN IMMEDIATE' if backup_dir else 'BEGIN')
            rows = conn.execute('SELECT id,state,refs_json FROM work ORDER BY id').fetchall()
            updates = []
            for work_id, state, raw in rows:
                refs = json.loads(raw or '{}')
                current = {'state': state, 'refs': refs}
                desired = {'state': state, 'refs': deepcopy(refs)}
                updated = preserve(current, desired)
                if updated != refs:
                    updates.append((work_id, raw, json.dumps(updated, ensure_ascii=False)))
            relative = path.relative_to(root)
            if updates and backup_dir:
                destination = backup_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise RuntimeError('backup already exists: ' + str(destination))
                # Separate read connection uses SQLite's online backup protocol.
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
                    with sqlite3.connect(destination) as target:
                        source.backup(target)
                for work_id, raw, updated in updates:
                    assert conn.execute('UPDATE work SET refs_json=? WHERE id=? AND refs_json=?',
                                        (updated, work_id, raw)).rowcount == 1
                conn.commit()
            else:
                conn.rollback()
            results.append({'database': str(relative), 'rows': len(rows),
                'changed_ids': [u[0] for u in updates],
                'before_digest': hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                'applied': bool(backup_dir and updates)})
        finally:
            conn.close()
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error('--apply requires --backup-dir')
    print(json.dumps(migrate(args.root.resolve(), args.backup_dir.resolve() if args.apply else None), indent=2))
