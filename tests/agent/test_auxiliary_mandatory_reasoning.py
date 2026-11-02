"""Mandatory-reasoning recovery must stay on the strict zero-cost route.

A route that refuses to switch reasoning off is retried on the SAME free
route at the reasoning floor (``agent/auxiliary_reasoning_floor.py``: the
disable is stepped up to the lowest effort, never stripped and never
rerouted to another provider/model) — the floor keeps the model, the
provider guard and the zero-price cap identical.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from agent import auxiliary_client as aux
from agent import strict_free_policy as policy
from agent.auxiliary_reasoning_floor import REASONING_FLOOR_EFFORT, _FLOORED_ROUTES
from agent.title_generator import (
    TITLE_MAX_TOKENS,
    _TITLE_RESPONSE_FORMAT,
    generate_title,
)


@pytest.mark.parametrize("async_mode", [False, True], ids=["title-sync", "aux-async"])
def test_mandatory_reasoning_retries_same_free_route(monkeypatch, tmp_path, async_mode):
    """Real config/projection/relay/retry path; only the SDK boundary is synthetic."""
    # The floor memo is process-global: a previous param's successful floored
    # retry would make this run start at the floor instead of sending the
    # disable first. Each param asserts the cold-route contract, so reset it.
    _FLOORED_ROUTES.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(policy.STRICT_FREE_ONLY_ENV, "1")
    route = {
        "provider": "openrouter", "model": policy.OPENROUTER_MODEL,
        "base_url": policy.OPENROUTER_BASE_URL,
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {**route, "default": policy.OPENROUTER_MODEL},
        "auxiliary": {"free_only": True, "title_generation": route},
    }))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"title": "Investigate database deadlocks"}', tool_calls=None,
        ), finish_reason="stop")],
        model=policy.OPENROUTER_MODEL, usage=None,
    )
    error = RuntimeError(
        "Error code: 400 - Reasoning is mandatory for this endpoint and cannot be disabled"
    )
    create_type = AsyncMock if async_mode else Mock
    create = create_type(side_effect=[error, response])
    client = SimpleNamespace(
        base_url=policy.OPENROUTER_BASE_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(aux, "_get_cached_client", lambda *_a, **_kw: (client, policy.OPENROUTER_MODEL))
    fallback = Mock(side_effect=AssertionError("must not attempt a provider/model fallback"))
    for name in ("_try_payment_fallback", "_try_configured_fallback_chain", "_try_main_fallback_chain"):
        monkeypatch.setattr(aux, name, fallback)

    if async_mode:
        result = asyncio.run(aux.async_call_llm(
            task="title_generation", messages=[{"role": "user", "content": "Investigate database deadlocks"}],
            max_tokens=TITLE_MAX_TOKENS, reasoning_config={"enabled": False},
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
        ))
        assert result is response
    else:
        assert generate_title("Investigate database deadlocks") == "Investigate database deadlocks"

    assert create.call_count == 2
    first, retry = [call.kwargs for call in create.call_args_list]
    assert first["extra_body"]["reasoning"]["enabled"] is False
    # The retry steps UP to the floor instead of dropping the reasoning field:
    # same route, same price guards, lowest effort the endpoint accepts.
    assert retry["extra_body"]["reasoning"] == {"enabled": True, "effort": REASONING_FLOOR_EFFORT}
    assert "reasoning_effort" not in retry
    assert retry == {**first, "extra_body": {
        **first["extra_body"], "reasoning": {"enabled": True, "effort": REASONING_FLOOR_EFFORT},
    }}
    for request in (first, retry):
        assert request["model"] == policy.OPENROUTER_MODEL
        assert request["extra_body"]["provider"] == {
            "allow_fallbacks": False,
            "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
        }
        assert request["extra_body"]["response_format"] == _TITLE_RESPONSE_FORMAT
        assert request["max_tokens"] == TITLE_MAX_TOKENS
    fallback.assert_not_called()
