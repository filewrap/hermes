"""peek() must never hand out another user's unshared entry.

The fast path returns the current entry without consulting
``_available_entries`` (the choke point that filters cooldowns and the
acting user), so a current entry added by user A and left in cooldown
could be peeked by user B. The fix adds the acting-user check to the
fast-path condition; the slow path then falls through to the filtered
available list.
"""
import time

from agent.credential_pool import CredentialPool, PooledCredential
from agent.credential_pool_user_selection import bind_acting_user_context
from hermes_cli.auth import write_credential_pool


def _pool_with_current_other_user_entry():
    # row0: added by user "111", unshared, and in cooldown (exhausted).
    # row1: added by user "222" (the acting user), available.
    rows = [
        PooledCredential(
            provider="openrouter", id="row0", label="other-user-key",
            source="manual", auth_type="api_key", access_token="fixture-0",
            priority=0, last_status="exhausted", last_status_at=time.time(),
            last_error_code=429, last_error_reset_at=time.time() + 3600,
        ),
        PooledCredential(
            provider="openrouter", id="row1", label="own-key",
            source="manual", auth_type="api_key", access_token="fixture-1",
            priority=1,
        ),
    ]
    rows[0].extra.update({"added_by": "111"})
    rows[1].extra.update({"added_by": "222"})
    write_credential_pool("openrouter", [e.to_dict() for e in rows])
    pool = CredentialPool("openrouter", rows)
    pool._current_id = "row0"  # selected earlier, by user 111
    return pool


def test_peek_fast_path_does_not_hand_out_other_users_unshared_entry():
    pool = _pool_with_current_other_user_entry()
    with bind_acting_user_context("222"):
        peeked = pool.peek()
    assert peeked is not None
    assert peeked.id == "row1"


def test_peek_fast_path_still_returns_own_current_entry():
    pool = _pool_with_current_other_user_entry()
    pool._current_id = "row1"
    with bind_acting_user_context("222"):
        assert pool.peek().id == "row1"
