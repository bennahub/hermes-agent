from types import SimpleNamespace

from agent.autonomy import owner_continuity as oc, store
from agent.session_persistence import _db_flush_row
from hermes_state import SessionDB
from tui_gateway.activity import work_state


def owner_work(home, db, session='owner'):
    if not db.get_session(session):
        db.create_session(session, source='desktop')
    mid = db.append_message(session, 'user', 'Implement the task and report the verified result.')
    return oc.register_owner_request(db, session, mid, hermes_home=home)


def publish(home, work, text='Which option should I use?'):
    oc.request_finish(work['id'], text, terminal='needs_owner', hermes_home=home)
    assert oc.deliver_pending(store.get_work(work['id'], home), home)
    return store.get_work(work['id'], home)


def test_delivery_verification_and_admission_survive_a_new_generation(autonomy_home):
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = owner_work(home, db)
        oc.wait(work['id'], until='2020-01-01T00:00:00Z', hermes_home=home)
        store.update_work(work['id'], state='working', refs={'dispatch': {
            'generation': 1, 'nonce': 'attempt-one', 'admitted': True}}, hermes_home=home)
        evidence_file = home / 'verified.txt'
        evidence_file.write_text('actual verified artifact')
        proof = oc.verify(work['id'], file=evidence_file, hermes_home=home)['refs']['verification']
        d1 = publish(home, work)
        receipt = d1['refs']['owner_delivery']
        store.update_work(work['id'], refs={'continuation_unstarted': {
            'delivery': receipt['message_id'], 'adjudicated': 'test-only engineering obligation'}}, hermes_home=home)
        assert work_state(home) != 'needs_owner'
        mid = db.append_message('owner', 'user', 'continue ' + work['id'])
        assert oc._bind_owner_decision(db, 'owner', mid)
        next_work = oc.wait(work['id'], until='2020-01-01T00:00:00Z', hermes_home=home)
        assert next_work['refs']['resume_generation'] > 1
        assert next_work['refs']['dispatch'] is None
        assert next_work['refs']['verification'] is None
        history = next_work['refs']['lifecycle']
        assert history['attempts']['attempt-one']['admitted'] is True
        assert history['verifications'][proof['id']] == proof
        assert history['deliveries'][receipt['result_id']]['verification'] == proof
        assert history['deliveries'][receipt['result_id']]['adjudication']['delivery'] == receipt['message_id']
        # Identical legitimate questions on the same work are distinct results.
        d2 = publish(home, work)
        assert d2['refs']['owner_delivery']['result_id'] != receipt['result_id']
        assert d2['refs']['owner_delivery']['message_id'] != receipt['message_id']
        assert work_state(home) == 'needs_owner'
        other = publish(home, owner_work(home, db, 'other'))
        obligation_two = other['refs']['owner_obligation']
        mid = db.append_message('owner', 'user', 'continue ' + work['id'])
        assert oc._bind_owner_decision(db, 'owner', mid)
        assert store.get_work(other['id'], home)['refs']['owner_obligation'] == obligation_two
        assert work_state(home) == 'needs_owner'
        mid = db.append_message('other', 'user', 'continue ' + other['id'])
        assert oc._bind_owner_decision(db, 'other', mid)
        assert work_state(home) != 'needs_owner'


def test_native_persistence_binds_result_by_id_not_time_or_text(autonomy_home):
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = owner_work(home, db)
        pending = oc.request_finish(work['id'], 'same words', terminal='needs_owner', hermes_home=home)
        # This unrelated row has identical content and is newer than the request.
        unrelated = db.append_message('owner', 'assistant', 'same words')
        assert oc._delivery_candidate(db, pending)[1] is None
        agent = SimpleNamespace(_owner_continuity_work_id=work['id'])
        from agent.message_projection import stamp_final
        final = {'role': 'assistant', 'content': 'same words', 'timestamp': 1}
        stamp_final(agent, [final])
        row = _db_flush_row(agent, final, False)
        db.append_messages_batch('owner', [row])
        selected = oc._delivery_candidate(db, pending)[1]
        assert selected['id'] == row['_row_id'] and selected['id'] != unrelated
        oc.finish_turn(SimpleNamespace(_owner_continuity_work_id=work['id'], _session_db=db))
        assert store.get_work(work['id'], home)['refs']['owner_delivery']['message_id'] == str(row['_row_id'])
        assert not oc.deliver_pending(store.get_work(work['id'], home), home)


def test_repeated_identical_owner_events_keep_distinct_work_identity(autonomy_home):
    with SessionDB(autonomy_home / 'state.db') as db:
        first = owner_work(autonomy_home, db)
        second = owner_work(autonomy_home, db)
        assert first['id'] != second['id']
        assert first['refs']['owner_request'] != second['refs']['owner_request']


def test_migration_is_backup_backed_idempotent_and_preserves_obligations(autonomy_home):
    import json
    import sqlite3
    from scripts.migrate_owner_lifecycle import migrate
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = publish(home, owner_work(home, db))
        path = home / 'autonomy/work.db'
        with sqlite3.connect(path) as conn:
            refs = work['refs']
            refs.pop('lifecycle')
            refs.pop('owner_obligation')
            conn.execute('UPDATE work SET refs_json=? WHERE id=?', (json.dumps(refs), work['id']))
        assert work_state(home) == 'needs_owner'
        backup = home / 'migration-backup'
        receipt = migrate(home, backup)
        assert receipt[0]['changed_ids'] == [work['id']]
        assert (backup / 'autonomy/work.db').is_file()
        assert work_state(home) == 'needs_owner'
        assert migrate(home, backup)[0]['changed_ids'] == []
        current = store.get_work(work['id'], home)
        assert current['refs']['owner_delivery'] == work['refs']['owner_delivery']
        assert current['refs']['owner_obligation']['status'] == 'unresolved'


def test_stop_outbox_retry_keeps_one_logical_delivery(autonomy_home, monkeypatch):
    import pytest
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = owner_work(home, db)
        source = work['refs']['owner_request']
        store.record_owner_stop({'owner': int(source['message_id'])}, home)
        stopped = store.get_work(work['id'], home)
        assert stopped['refs']['pending_owner_result']['id']
        with monkeypatch.context() as patch:
            def crash(*args, **kwargs):
                raise RuntimeError('crash after transcript commit')
            patch.setattr(store, 'update_work', crash)
            with pytest.raises(RuntimeError, match='crash after transcript'):
                oc.deliver_pending(stopped, home)
        assert oc.deliver_pending(store.get_work(work['id'], home), home)
        rows = [r for r in db.get_messages('owner') if r['role'] == 'assistant']
        assert len(rows) == 1
        assert store.get_work(work['id'], home)['state'] == 'cancelled'


def test_compaction_carrier_cannot_take_a_pending_result_identity(autonomy_home):
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
    from agent.message_projection import stamp_final
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = owner_work(home, db)
        pending = oc.request_finish(work['id'], 'owner question', terminal='needs_owner', hermes_home=home)
        agent = SimpleNamespace(_owner_continuity_work_id=work['id'])
        summary = {'role': 'assistant', 'content': 'compacted context', COMPRESSED_SUMMARY_METADATA_KEY: True}
        carrier = _db_flush_row(agent, summary, False)
        db.append_messages_batch('owner', [carrier])
        assert not (carrier.get('display_metadata') or {}).get('owner_result_id')
        final = {'role':'assistant', 'content':'owner question'}
        stamp_final(agent, [final])
        row = _db_flush_row(agent, final, False)
        db.append_messages_batch('owner', [row])
        assert oc._delivery_candidate(db, pending)[1]['id'] == row['_row_id']


def test_reply_to_d1_cannot_discard_a_concurrently_pending_d2(autonomy_home, monkeypatch):
    home = autonomy_home
    with SessionDB(home / 'state.db') as db:
        work = publish(home, owner_work(home, db))
        first = work['refs']['owner_delivery']
        mid = db.append_message('owner', 'user', 'continue ' + work['id'])
        update = store.update_work
        inserted = []
        def concurrent_question(*args, **kwargs):
            if kwargs.get('state') == 'working' and not inserted:
                inserted.append(True)
                oc.request_finish(work['id'], 'new D2 decision', terminal='needs_owner', hermes_home=home)
            return update(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'update_work', concurrent_question)
            assert oc._bind_owner_decision(db, 'owner', mid) is None
        current = store.get_work(work['id'], home)
        assert current['refs']['pending_owner_result']['text'] == 'new D2 decision'
        assert oc.deliver_pending(current, home)
        current = store.get_work(work['id'], home)
        assert current['refs']['owner_delivery']['result_id'] != first['result_id']
        assert current['refs']['owner_obligation']['status'] == 'unresolved'
        assert work_state(home) == 'needs_owner'
