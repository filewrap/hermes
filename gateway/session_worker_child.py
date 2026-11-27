"""Owner-reserved delegated children of a verified worker execution (#141793).

A worker never mints a child. It asks on its OWN fenced receipt stream (epoch, assignment,
generation, sequence), and the owner creates the child's session row and execution, binds it to
the parent's verified process identity and the worker's fresh secret, and enforces the frozen
policy's delegation caps. The child then persists through the ordinary ``worker.persist`` path
under its own claim. Expected refusals (caps, draining) are receipts, not errors: an error would
leave the parent's journal pending and refuse every later write of its turn.
"""
import secrets

from gateway.session_persistence import authority_persistence
from persistence.core.errors import AuthorityRefusal
from persistence.workers.delegations import WorkerDelegationPayload
from persistence.workers.vocabulary import CHILD_SEPARATOR
from hermes_state_runtime import RuntimeStoreError
from hermes_state_runtime_workers import descendant_range

_DEFAULTS = {'max_concurrent_children': 10, 'max_spawn_depth': 1}


def _limit(section, key):
    try:
        return max(1, int(section.get(key, _DEFAULTS[key])))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def delegation_limits(authority, execution_id, session_id):
    """``(max_concurrent_children, max_spawn_depth)`` from the root conversation's frozen policy
    (the profile's config for a session with no local policy), never from the asking worker."""
    from gateway.session_policy import policy_for_source, profile_config
    root = execution_id.split(CHILD_SEPARATOR, 1)[0]
    if root != execution_id:
        if hasattr(authority, 'persistence'):
            found = authority_persistence(authority).worker_executions.execution_session(execution_id=root)
            session_id = found if found is not None else session_id
        else:
            with authority.db._read_ctx() as conn:
                row = conn.execute('SELECT session_id FROM worker_executions WHERE execution_id=?', (root,)).fetchone()
            session_id = row[0] if row else session_id
    live = authority.sessions.get(authority.logical_owner(session_id))
    policy = policy_for_source(authority.runner, live.source) if live is not None else None
    config = policy.config(authority) if policy is not None else profile_config(authority)
    section = config.get('delegation') or {}
    return _limit(section, 'max_concurrent_children'), _limit(section, 'max_spawn_depth')


async def worker_child_request(connection, ref, params):
    """worker.persist ``child.reserve``: the parent's full claim and assignment are verified first."""
    import asyncio
    from functools import partial

    from gateway.session_worker import _SCOPE, _claim, _verify
    from hermes_state_runtime import apply_worker_receipt
    authority = connection.authority
    if not hasattr(authority, 'persistence'):
        # Db-only authority: the fork's receipt path (SessionDB + handler).
        if set(params) != _SCOPE | {'epoch', 'sequence', 'operation', 'payload'}:
            raise RuntimeStoreError('invalid_params')
        claim = _claim(connection, ref, params)
        if 'worker:adopt' not in connection.actor.capabilities:
            raise RuntimeStoreError('permission_denied')
        _verify(connection, ref, params, claim)

        def reserve_db():
            refused, limits = None, (0, 0)
            try:
                authority._require_admission_open()
                limits = delegation_limits(authority, params['execution_id'], params['session_id'])
            except RuntimeStoreError as exc:
                refused = exc.reason
            handler = partial(reserve_child_execution, epoch=params['epoch'], execution_id=params['execution_id'],
                              identity={key: params[key] for key in ('profile_id', 'pid', 'birth')},
                              principal=connection.actor.subject, limits=limits, refused=refused)
            return apply_worker_receipt(authority.db, **{key: params[key] for key in (
                'epoch', 'execution_id', 'session_id', 'generation', 'sequence', 'operation', 'payload')},
                handlers={'child.reserve': handler})
        return await asyncio.to_thread(reserve_db)
    if set(params) != _SCOPE | {'epoch', 'sequence', 'operation', 'payload'}:
        raise AuthorityRefusal('invalid_params')
    claim = _claim(connection, ref, params)
    if 'worker:adopt' not in connection.actor.capabilities:
        raise AuthorityRefusal('permission_denied')
    _verify(connection, ref, params, claim)
    workers = authority_persistence(authority).worker_executions

    def reserve():
        refused, limits = None, (0, 0)
        try:
            authority._require_admission_open()
            limits = delegation_limits(authority, params['execution_id'], params['session_id'])
        except AuthorityRefusal as exc:
            refused = exc.reason
        reservation = workers.reserve_child(
            **{key: params[key] for key in ('epoch', 'execution_id', 'session_id', 'generation', 'sequence',
                                            'profile_id', 'pid', 'birth')},
            secret=WorkerDelegationPayload(params['payload']), principal=connection.actor.subject,
            max_concurrent_children=limits[0], max_spawn_depth=limits[1], refused=refused)
        child = reservation.child
        if child is None:
            return {'refused': reservation.refused}
        return {'child': {'session_id': child.session_id, 'execution_id': child.execution_id,
                          'generation': child.generation}}
    return await asyncio.to_thread(reserve)


def reserve_child_execution(db, conn, session_id, payload, *, epoch, execution_id, identity, principal, limits, refused):
    from hermes_state_ids import new_session_id
    from hermes_state_keys import admission_fingerprint
    from hermes_state_runtime import _secret_digest
    from hermes_state_worker_lifecycle import insert_session_row_in_transaction
    secret = payload.get('secret') if isinstance(payload, dict) else None
    if set(payload) != {'secret'} or not isinstance(secret, str) or not 16 <= len(secret) <= 256:
        raise RuntimeStoreError('invalid_params')
    max_children, max_depth = limits
    prefix = execution_id + CHILD_SEPARATOR
    live = conn.execute("SELECT COUNT(*) FROM worker_executions WHERE execution_id>=? AND execution_id<? "
                        "AND instr(substr(execution_id,?),'/')=0 AND status!='terminal'",
                        (*descendant_range(execution_id), len(prefix) + 1)).fetchone()[0]
    if refused is None and prefix.count(CHILD_SEPARATOR) > max_depth:
        refused = 'delegation_depth_exceeded'
    if refused is None and live >= max_children:
        refused = 'delegation_capacity'
    if refused is not None:
        return {'refused': refused}
    parent = conn.execute('SELECT profile_name FROM sessions WHERE id=?', (session_id,)).fetchone()
    child_sid, child_eid = new_session_id(), prefix + secrets.token_hex(8)
    insert_session_row_in_transaction(db, conn, child_sid, 'subagent', model_config={'_delegate_from': session_id},
                                      parent_session_id=session_id, profile_name=parent['profile_name'])
    scope = dict(identity, session_id=child_sid, execution_id=child_eid, generation=0, secret=secret)
    claim = admission_fingerprint(canonical_target=child_sid, payload=scope | {'principal': principal})
    conn.execute("INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
                 "VALUES(?,?,'child',?,0,'running',?)", (child_eid, child_sid, epoch, _secret_digest(claim)))
    return {'child': {'session_id': child_sid, 'execution_id': child_eid, 'generation': 0}}
