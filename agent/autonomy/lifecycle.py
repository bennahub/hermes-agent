"""Durable execution and delivery facts within the existing work ledger.

Scheduling generations fence jobs. They do not own delivery or verification facts.
Current pointers may move; identity-keyed history is retained transactionally.
"""
from copy import deepcopy
import uuid


def identity():
    return uuid.uuid4().hex


def delivery_key(delivery):
    if not delivery:
        return None
    return delivery.get('result_id') or (
        'legacy:' + str(delivery.get('session_id')) + ':' + str(delivery['message_id'])
        if delivery.get('message_id') is not None else None)


def adjudicated(refs):
    marker = refs.get('continuation_unstarted') or {}
    delivery = refs.get('owner_delivery') or {}
    if not isinstance(marker, dict) or not isinstance(delivery, dict):
        return False
    return bool(marker.get('delivery') is not None and delivery.get('message_id') is not None
                and str(marker['delivery']) == str(delivery['message_id']))


def preserve(current, desired):
    """Called inside store's write transaction, after its compare-and-swap fence."""
    old = (current or {}).get('refs') or {}
    refs = desired['refs']
    pending = refs.get('pending_owner_result')
    if pending and not pending.get('id'):
        raise ValueError('legacy pending result must be drained before lifecycle migration')
    history = deepcopy(old.get('lifecycle') or {'version': 1, 'attempts': {},
                       'verifications': {}, 'deliveries': {}, 'obligations': {}})
    legacy_verification_id = identity()
    history.setdefault('executions', {})
    # Archive both sides: a pointer may be cleared or replaced in this write.
    for source in (old, refs):
        execution = source.get('execution') or {}
        if execution.get('id'):
            history['executions'].setdefault(execution['id'], deepcopy(execution))
        dispatch = source.get('dispatch') or {}
        if dispatch.get('nonce'):
            key = dispatch['nonce']
            previous = history['attempts'].get(key, {})
            history['attempts'][key] = {**previous, **dispatch,
                'admitted': bool(previous.get('admitted') or dispatch.get('admitted'))}
        verification = source.get('verification')
        if verification:
            verification = dict(verification)
            key = verification.get('id') or legacy_verification_id
            verification['id'] = key
            history['verifications'].setdefault(key, verification)
            if source is refs:
                refs['verification'] = verification
        delivery = source.get('owner_delivery')
        key = delivery_key(delivery)
        if key:
            history['deliveries'].setdefault(key, deepcopy(delivery))
            if adjudicated(source):
                history['deliveries'][key].setdefault('adjudication', deepcopy(source['continuation_unstarted']))
    # An old dispatch is historical evidence, never evidence of a fresh job.
    dispatch = refs.get('dispatch') or {}
    if dispatch.get('nonce') and history['attempts'][dispatch['nonce']].get('admitted'):
        refs['dispatch'] = {**dispatch, 'admitted': True}
    if dispatch and dispatch.get('generation') != refs.get('resume_generation'):
        refs['dispatch'] = None
    if old.get('resume_generation') != refs.get('resume_generation'):
        refs['execution'] = None
        refs['verification'] = None
    prior = deepcopy(old.get('owner_obligation'))
    current_delivery = delivery_key(refs.get('owner_delivery'))
    prior_delivery = delivery_key(old.get('owner_delivery'))
    if prior and (desired['state'] != 'needs_owner' or current_delivery != prior_delivery):
        prior['status'] = 'resolved'
        history['obligations'][prior['id']] = prior
        prior = None
    if desired['state'] == 'needs_owner':
        obligation = prior or {'id': identity(), 'delivery_id': current_delivery,
                              'kind': refs.get('outcome_kind') or 'owner_input'}
        obligation['status'] = 'adjudicated' if adjudicated(refs) else 'unresolved'
        history['obligations'][obligation['id']] = deepcopy(obligation)
        refs['owner_obligation'] = obligation
    else:
        refs['owner_obligation'] = None
    refs['lifecycle'] = history
    return refs
