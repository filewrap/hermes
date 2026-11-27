"""Failover-only rotation for OAuth pool entries with no refresh implementation.

qwen-oauth / minimax-oauth (and any plugin provider without a
``refresh_credential`` hook) hold OAuth pool entries the pool cannot refresh:
``_refresh_entry_impl`` used to return the entry unchanged, which the 401
recovery path read as a successful refresh — so it retried the same dead key
until the per-entry refresh cap and failed the turn without ever trying the
next key in the pool. A forced refresh with no implementation must report
failure so the caller rotates instead; a proactive (select-time) refresh
keeps leasing the entry unchanged.
"""

from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.credential_pool import CredentialPool, PooledCredential

_QWEN_BASE_URL = "https://portal.qwen.ai/v1"


def _entry(idx: int, key: str) -> PooledCredential:
    return PooledCredential(
        provider="qwen-oauth",
        id=f"qwen-{idx}",
        label=f"qwen-{idx}",
        auth_type="oauth",
        priority=idx,
        source="manual",
        access_token=key,
        refresh_token=f"qwen-rt-{idx}",
        base_url=_QWEN_BASE_URL,
    )


def _pool() -> CredentialPool:
    return CredentialPool("qwen-oauth", [_entry(1, "qwen-key-1"), _entry(2, "qwen-key-2")])


class _Agent:
    provider = "qwen-oauth"
    model = "qwen3-next"
    base_url = _QWEN_BASE_URL
    _credential_pool_revert_id = None

    def __init__(self, pool: CredentialPool) -> None:
        self._credential_pool = pool
        self._credential_pool_entry_id = "qwen-1"
        self.api_key = "qwen-key-1"
        self.swapped: list[str] = []

    def _is_entitlement_failure(self, *_args) -> bool:
        return False

    def _swap_credential(self, entry) -> bool:
        self.api_key = entry.runtime_api_key
        self._credential_pool_entry_id = entry.id
        self.swapped.append(entry.id)
        return True


def test_forced_refresh_reports_failure_for_non_refreshable_oauth():
    """A forced refresh of a qwen-oauth entry must return None (nothing to mint
    a new token with), not the entry unchanged."""
    pool = _pool()

    assert pool.try_refresh_matching(api_key_hint="qwen-key-1") is None


def test_proactive_refresh_still_leases_non_refreshable_oauth():
    """A select-time (non-forced) refresh keeps leasing the entry unchanged —
    only failure recovery treats the missing implementation as a failure."""
    pool = _pool()
    (entry,) = [e for e in pool.entries() if e.id == "qwen-1"]

    assert pool._refresh_entry(entry, force=False) is entry
    assert pool.select() is not None
    assert pool.select().id == "qwen-1"


def test_401_rotates_to_next_key_for_non_refreshable_oauth():
    """A 401 on the first qwen-oauth key must bench it and swap in the second
    key instead of retrying the dead one."""
    pool = _pool()
    agent = _Agent(pool)

    recovered, has_retried_429 = recover_with_credential_pool(
        agent, status_code=401, has_retried_429=False,
    )

    assert recovered is True
    assert has_retried_429 is False
    assert agent.api_key == "qwen-key-2", "the turn must continue on the next pool key, not the dead one"
    assert agent.swapped == ["qwen-2"]
    assert {e.id: e.last_status for e in pool.entries()} == {
        "qwen-1": "exhausted", "qwen-2": None,
    }


def test_chained_401_exhausts_pool_then_fails():
    """Failover walks every key: a 401 on the second key too leaves nothing to
    rotate to, so recovery reports failure instead of retrying a dead key."""
    pool = _pool()
    agent = _Agent(pool)

    first, _ = recover_with_credential_pool(agent, status_code=401, has_retried_429=False)
    assert first is True
    assert agent.api_key == "qwen-key-2"

    second, _ = recover_with_credential_pool(agent, status_code=401, has_retried_429=False)

    assert second is False
    assert {e.id: e.last_status for e in pool.entries()} == {
        "qwen-1": "exhausted", "qwen-2": "exhausted",
    }
