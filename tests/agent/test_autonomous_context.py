"""Behaviour contracts for the autonomous-turn ContextVar.

The runner sets the flag around each turn of an autonomous session and
clears it after; these tests pin the default, the set/clear round-trip,
and isolation between contexts and asyncio tasks.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest

from agent.autonomous_context import (
    AUTONOMOUS_TURN,
    consume_autonomous_end,
    is_autonomous_turn,
    request_autonomous_end,
    set_autonomous_turn,
)


@pytest.fixture(autouse=True)
def _reset_autonomous_turn():
    token = AUTONOMOUS_TURN.set(False)
    try:
        yield
    finally:
        AUTONOMOUS_TURN.reset(token)


def test_default_is_not_autonomous():
    assert is_autonomous_turn() is False


def test_set_and_clear_round_trip():
    set_autonomous_turn(True)
    assert is_autonomous_turn() is True
    set_autonomous_turn(False)
    assert is_autonomous_turn() is False


def test_set_in_copied_context_does_not_leak():
    set_autonomous_turn(True)
    seen: list[bool] = []

    def _read_and_clear():
        seen.append(is_autonomous_turn())
        set_autonomous_turn(False)
        seen.append(is_autonomous_turn())

    contextvars.copy_context().run(_read_and_clear)
    assert seen == [True, False]
    assert is_autonomous_turn() is True


@pytest.mark.asyncio
async def test_asyncio_tasks_are_isolated():
    async def _task(value: bool) -> bool:
        set_autonomous_turn(value)
        await asyncio.sleep(0)  # yield once; a zero sleep is a scheduling point, not a wait
        return is_autonomous_turn()

    assert await asyncio.gather(_task(True), _task(False)) == [True, False]
    assert is_autonomous_turn() is False


def test_done_end_consume_returns_none_when_nothing_armed():
    from types import SimpleNamespace

    assert consume_autonomous_end(SimpleNamespace(_autonomous_done_requested=None)) is None


def test_done_end_arm_then_consume_round_trips_and_pops():
    from types import SimpleNamespace

    agent = SimpleNamespace(_autonomous_done_requested=None)
    request_autonomous_end(agent, "done", "Done — tidied.")
    assert consume_autonomous_end(agent) == {
        "action": "done", "acknowledgment": "Done — tidied.",
    }
    assert consume_autonomous_end(agent) is None
    assert agent._autonomous_done_requested is None


def test_done_end_consume_drops_an_unknown_action():
    from types import SimpleNamespace

    agent = SimpleNamespace(
        _autonomous_done_requested={"action": "explode", "acknowledgment": "boom"})
    assert consume_autonomous_end(agent) is None
    assert agent._autonomous_done_requested is None
