#!/usr/bin/env python3
"""Autonomous backbone, step 1 — per-model lanes + concurrency defaults.

Workers spread across ``delegation.worker_models`` round-robin instead of all
inheriting the parent model; a per-model live cap refuses over-cap workers
with a reason; spawns are staggered; worker model calls retry at most twice
with a backoff that honours Retry-After.

Run with:  HERMES_PYTHON=... scripts/run_tests.sh tests/tools/test_delegate_backbone_lanes.py
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from tools import delegate_tool_dispatch
from tools.delegate_tool_config import (
    _get_max_concurrent_children,
    _get_max_concurrent_per_model,
    _get_rate_limit_max_retries,
    _get_spawn_stagger_seconds,
    _get_worker_model_fallback,
    _get_worker_models,
)
from tools.delegate_tool_routes import TaskRoute


class _FakeParent:
    """Minimal parent agent for credential anchoring."""

    provider = "openrouter"
    model = "anthropic/claude-opus-4.8"
    base_url = "https://openrouter.ai/api/v1"
    api_key = "parent-key"


_BASE_CREDS = {
    "model": None,
    "provider": None,
    "base_url": None,
    "api_key": None,
    "api_mode": None,
    "command": None,
    "args": None,
}


def _route(model=None, provider=None):
    creds = dict(_BASE_CREDS)
    if model is not None:
        creds["model"] = model
    if provider is not None:
        creds["provider"] = provider
    return TaskRoute(creds=creds, routing_cfg={})


def _tasks(n, **extra):
    return [
        {"goal": f"research topic part {i} in depth", **extra} for i in range(n)
    ]


_CATALOG = ["anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.8"]


class TestNewKnobGetters:
    """The new delegation.* keys read with the documented defaults."""

    def test_worker_models_default_empty(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_worker_models() == []

    def test_worker_models_parses_list(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"worker_models": ["sonnet", "opus", 7, "  ", None]},
        ):
            assert _get_worker_models() == ["sonnet", "opus"]

    def test_worker_model_fallback_default_true(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_worker_model_fallback() is True

    def test_max_concurrent_per_model_default_two(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_max_concurrent_per_model() == 2

    def test_spawn_stagger_default_five_seconds(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_spawn_stagger_seconds() == 5.0

    def test_spawn_stagger_clamped(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"spawn_stagger_seconds": 600},
        ):
            assert _get_spawn_stagger_seconds() == 60.0

    def test_rate_limit_max_retries_default_two(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_rate_limit_max_retries() == 2

    def test_max_concurrent_children_default_four(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            assert _get_max_concurrent_children() == 4


class TestWorkerModelRouting:
    """Round-robin over delegation.worker_models, per-task model still wins."""

    def setUp(self):
        self._catalog_patch = patch(
            "hermes_cli.model_switch.list_provider_models",
            return_value=_CATALOG,
        )
        self._catalog_patch.start()

    def tearDown(self):
        self._catalog_patch.stop()

    def _routing(self, cfg):
        from tools.delegate_tool import _apply_worker_model_routing

        with patch("tools.delegate_tool._load_config", return_value=cfg):
            return _apply_worker_model_routing(
                _tasks(4), [_route() for _ in range(4)], _FakeParent()
            )

    def test_round_robin_spreads_workers(self):
        routes, err = self._routing(
            {"worker_models": ["anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.8"]}
        )
        assert err is None
        assert [r.creds["model"] for r in routes] == [
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-opus-4.8",
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-opus-4.8",
        ]

    def test_empty_list_inherits_parent(self):
        routes, err = self._routing({"worker_models": []})
        assert err is None
        assert all(r.creds["model"] is None for r in routes)

    def test_per_task_model_wins_over_the_list(self):
        import dataclasses

        from tools.delegate_tool import _apply_worker_model_routing

        tasks = _tasks(2)
        tasks[0]["model"] = "sonnet"
        # As if _apply_task_model_selection already resolved task 0's model.
        routes = [_route(), _route()]
        routes[0] = dataclasses.replace(
            routes[0],
            creds={**_BASE_CREDS, "model": "anthropic/claude-sonnet-4.5"},
        )
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"worker_models": ["anthropic/claude-opus-4.8"]},
        ):
            routes, err = _apply_worker_model_routing(tasks, routes, _FakeParent())
        assert err is None
        assert routes[0].creds["model"] == "anthropic/claude-sonnet-4.5"
        assert routes[1].creds["model"] == "anthropic/claude-opus-4.8"

    def test_unresolvable_list_entry_fails_the_call(self):
        routes, err = self._routing({"worker_models": ["no-such-model-xyz"]})
        assert routes is not None
        assert err is not None and "no-such-model-xyz" in err

    def test_fallback_chain_is_the_rest_of_the_list(self):
        routes, err = self._routing(
            {
                "worker_models": [
                    "anthropic/claude-sonnet-4.5",
                    "anthropic/claude-opus-4.8",
                ],
                "worker_model_fallback": True,
            }
        )
        assert err is None
        first_fallback = routes[0].routing_cfg.get("fallback_providers") or []
        assert {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"} in first_fallback
        second_fallback = routes[1].routing_cfg.get("fallback_providers") or []
        assert {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"} in second_fallback

    def test_fallback_off_leaves_routing_cfg_alone(self):
        routes, err = self._routing(
            {
                "worker_models": [
                    "anthropic/claude-sonnet-4.5",
                    "anthropic/claude-opus-4.8",
                ],
                "worker_model_fallback": False,
            }
        )
        assert err is None
        assert "fallback_providers" not in routes[0].routing_cfg
        assert "fallback_providers" not in routes[1].routing_cfg


class TestPerModelCap:
    """At most max_concurrent_per_model workers share one (provider, model)."""

    def test_third_worker_on_one_model_is_refused_with_a_reason(self):
        from tools.delegate_tool import _apply_per_model_cap

        # A spread batch: three workers on "m" plus one on "n" — the third
        # on "m" is refused, the "n" worker is untouched.
        routes = [_route(model="m", provider="p") for _ in range(3)] + [
            _route(model="n", provider="p")
        ]
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"max_concurrent_per_model": 2},
        ):
            out = _apply_per_model_cap(_tasks(4), routes, _FakeParent())
        assert out[0].error is None
        assert out[1].error is None
        assert out[2].error is not None
        assert "m" in out[2].error and "2" in out[2].error
        assert out[3].error is None
        refused = out[2].refused_entry(2)
        assert refused["status"] == "error"
        assert refused["route"]["refused"] is True

    def test_single_model_batch_is_not_capped(self):
        # The default: every worker inherits the parent's model, one lane.
        # The global max_concurrent_children bounds that burst; the
        # per-model cap must not refuse siblings of a single-model batch.
        from tools.delegate_tool import _apply_per_model_cap

        routes = [_route(model="m", provider="p") for _ in range(3)]
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"max_concurrent_per_model": 2},
        ):
            out = _apply_per_model_cap(_tasks(3), routes, _FakeParent())
        assert all(r.error is None for r in out)

    def test_different_models_do_not_share_the_cap(self):
        from tools.delegate_tool import _apply_per_model_cap

        routes = [
            _route(model="a", provider="p"),
            _route(model="a", provider="p"),
            _route(model="b", provider="p"),
            _route(model="b", provider="p"),
        ]
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"max_concurrent_per_model": 2},
        ):
            out = _apply_per_model_cap(_tasks(4), routes, _FakeParent())
        assert all(r.error is None for r in out)


class TestWorkerRetryPolicy:
    """Workers retry model calls at most twice, backoff base 5 s."""

    def test_policy_applied_to_a_child(self):
        from tools.delegate_tool import _apply_worker_retry_policy

        child = SimpleNamespace()
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"rate_limit_max_retries": 2},
        ):
            _apply_worker_retry_policy(child)
        assert child._api_max_retries == 2
        assert child._retry_backoff_base_seconds == 5.0

    def test_backoff_base_is_read_by_compute_error_backoff(self):
        from agent.retry_utils import retry_base_delay

        assert retry_base_delay(SimpleNamespace()) == 2.0
        assert (
            retry_base_delay(SimpleNamespace(_retry_backoff_base_seconds=5.0))
            == 5.0
        )
        assert retry_base_delay(SimpleNamespace(_retry_backoff_base_seconds=0)) == 2.0

    def test_invalid_response_retry_honours_retry_after(self):
        from agent.turn_response_check import _invalid_response_wait_seconds

        response = SimpleNamespace(headers={"Retry-After": "30"})
        assert _invalid_response_wait_seconds(response, retry_count=0) == 30.0
        assert _invalid_response_wait_seconds(
            SimpleNamespace(headers={}), retry_count=0
        ) >= 5.0


def _child(**kw):
    base = dict(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="anthropic/claude-opus-4.8",
        api_mode="chat_completions",
        _use_prompt_caching=True,
        session_api_calls=0,
        _last_activity_desc=None,
        _interrupt_requested=False,
    )
    return SimpleNamespace(**{**base, **kw})


def _batch(parent, kids, run_child):
    tasks = [{"goal": f"research topic part {i} in depth"} for i in range(len(kids))]
    return SimpleNamespace(
        parent_agent=parent,
        task_list=tasks,
        children=[(i, tasks[i], c) for i, c in enumerate(kids)],
        live_deleg_id=None,
        max_children=len(kids),
        unit_id=None,
        live_paths=[],
        live_writers=[],
        run_child=run_child,
    )


def _parent():
    return SimpleNamespace(
        _interrupt_requested=False,
        _delegate_spinner=None,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
    )


class TestSpawnStagger:
    """Spawns are spaced by spawn_stagger_seconds instead of firing at once."""

    def test_stagger_is_invoked_between_spawns(self):
        from tools.delegate_tool_lanes import reset_lanes

        reset_lanes()
        parent = _parent()
        kids = [_child() for _ in range(3)]
        calls = []

        def run_child(i, task, child):
            return {
                "task_index": i,
                "status": "completed",
                "summary": "x",
                "api_calls": 1,
                "duration_seconds": 0,
            }

        with (
            patch(
                "tools.delegate_tool._load_config",
                return_value={
                    "spawn_stagger_seconds": 5.0,
                    "max_concurrent_per_model": 2,
                },
            ),
            patch(
                "tools.delegate_tool_dispatch._wait_stagger",
                side_effect=lambda b, s, h, **kw: calls.append(s) or False,
            ),
        ):
            results = []
            delegate_tool_dispatch._run_children_parallel(
                _batch(parent, kids, run_child), results, honor_parent_interrupt=True
            )
        assert calls == [5.0, 5.0]
        assert sorted(r["task_index"] for r in results) == [0, 1, 2]
        reset_lanes()

    def test_wait_stagger_returns_quickly_when_interrupted(self):
        parent = _parent()
        parent._interrupt_requested = True
        t0 = time.monotonic()
        assert (
            delegate_tool_dispatch._wait_stagger(
                SimpleNamespace(parent_agent=parent), 30.0, True
            )
            is True
        )
        assert time.monotonic() - t0 < 2.0

    def test_wait_stagger_waits_the_delay(self):
        parent = _parent()
        t0 = time.monotonic()
        assert (
            delegate_tool_dispatch._wait_stagger(
                SimpleNamespace(parent_agent=parent), 0.2, True
            )
            is False
        )
        assert 0.15 <= time.monotonic() - t0 < 2.0


class TestPerModelLanes:
    """The live per-(provider, model) lane refuses a third concurrent worker."""

    def test_lane_acquire_release(self):
        from tools.delegate_tool_lanes import (
            release_lane,
            reset_lanes,
            try_acquire_lane,
        )

        reset_lanes()
        key = ("openrouter", "anthropic/claude-opus-4.8")
        assert try_acquire_lane(key, 2) is True
        assert try_acquire_lane(key, 2) is True
        assert try_acquire_lane(key, 2) is False
        release_lane(key)
        assert try_acquire_lane(key, 2) is True
        reset_lanes()

    def test_third_live_worker_on_one_model_is_refused_at_submit(self):
        from tools.delegate_tool_lanes import reset_lanes, try_acquire_lane

        reset_lanes()
        key = ("openrouter", "anthropic/claude-opus-4.8")
        assert try_acquire_lane(key, 2) is True
        assert try_acquire_lane(key, 2) is True
        parent = _parent()
        ran = []

        def run_child(i, task, child):
            ran.append(i)
            return {
                "task_index": i,
                "status": "completed",
                "summary": "x",
                "api_calls": 1,
                "duration_seconds": 0,
            }

        try:
            results = []
            with patch(
                "tools.delegate_tool._load_config",
                return_value={
                    "spawn_stagger_seconds": 0,
                    "max_concurrent_per_model": 2,
                },
            ):
                # Spread batch: the opus child hits the full lane and is
                # refused; the sonnet child runs.
                delegate_tool_dispatch._run_children_parallel(
                    _batch(
                        parent,
                        [
                            _child(),
                            _child(model="anthropic/claude-sonnet-4.5"),
                        ],
                        run_child,
                    ),
                    results,
                    honor_parent_interrupt=True,
                )
        finally:
            reset_lanes()
        assert ran == [1]
        assert len(results) == 2
        by_index = {r["task_index"]: r for r in results}
        assert by_index[0]["status"] == "error"
        assert "per-model" in by_index[0]["error"].lower()
        assert by_index[1]["status"] == "completed"

    def test_lanes_are_released_when_children_finish(self):
        from tools.delegate_tool_lanes import reset_lanes, try_acquire_lane

        reset_lanes()
        parent = _parent()

        def run_child(i, task, child):
            return {
                "task_index": i,
                "status": "completed",
                "summary": "x",
                "api_calls": 1,
                "duration_seconds": 0,
            }

        results = []
        with patch(
            "tools.delegate_tool._load_config",
            return_value={
                "spawn_stagger_seconds": 0,
                "max_concurrent_per_model": 2,
            },
        ):
            delegate_tool_dispatch._run_children_parallel(
                _batch(
                    parent,
                    [
                        _child(),
                        _child(model="anthropic/claude-sonnet-4.5"),
                    ],
                    run_child,
                ),
                results,
                honor_parent_interrupt=True,
            )
        assert sorted(r["task_index"] for r in results) == [0, 1]
        key = ("openrouter", "anthropic/claude-opus-4.8")
        assert try_acquire_lane(key, 2) is True
        assert try_acquire_lane(key, 2) is True
        other = ("openrouter", "anthropic/claude-sonnet-4.5")
        assert try_acquire_lane(other, 2) is True
        assert try_acquire_lane(other, 2) is True
        reset_lanes()


def test_lanes_module_does_not_leak_between_tests():
    from tools.delegate_tool_lanes import reset_lanes, try_acquire_lane

    reset_lanes()
    assert try_acquire_lane(("p", "m"), 2) is True
    reset_lanes()
    assert try_acquire_lane(("p", "m"), 2) is True
    reset_lanes()
