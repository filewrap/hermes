"""Gateway regression tests for Telegram's user-private skill slash commands."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.telegram_personal_settings import (
    current_personal_memory_context,
    reset_personal_memory_context,
    set_personal_memory_context,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _install_skill(home: Path, user_id: str, name: str, body: str) -> Path:
    from gateway.telegram_skills import install_skill, prepare_skill

    text = f"---\nname: {name}\ndescription: {name} private procedure\n---\n\n{body}\n"
    return install_skill(home, user_id, prepare_skill(text))


@contextmanager
def _personal_scope(home: Path, user_id: str):
    home_token = set_hermes_home_override(home)
    personal_token = set_personal_memory_context(home, user_id)
    try:
        yield
    finally:
        reset_personal_memory_context(personal_token)
        reset_hermes_home_override(home_token)


def _source(user_id: str, *, chat_type: str = "dm", profile: str | None = None) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=user_id,
        chat_type=chat_type,
        profile=profile,
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_key_for_source = lambda source: f"telegram:{source.chat_type}:{source.chat_id}"
    return runner


def _shutdown_runner(runner) -> None:
    executor = getattr(runner, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_owner_dm_skill_slash_loads_personal_full_body_and_instruction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "profile"
    body_marker = "PRIVATE_FULL_BODY_MARKER"
    _install_skill(home, "101", "private-runbook", f"# Procedure\n{body_marker}: follow every step.")
    runner = _runner()
    source = _source("101", profile="default")
    event = MessageEvent(text="/private-runbook apply the task carefully", source=source)

    try:
        with _personal_scope(home, "101"):
            reply = await runner._run_in_executor_with_context(
                runner._hm_skill_slash_rewrite, event, source, "task-private-101", "private-runbook"
            )
            assert reply is None
            assert body_marker in event.text
            assert "apply the task carefully" in event.text
            assert event.text != "/private-runbook apply the task carefully"
    finally:
        _shutdown_runner(runner)
    assert current_personal_memory_context() is None


@pytest.mark.asyncio
async def test_reload_discovers_new_private_skill_without_revealing_another_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "profile"
    _install_skill(home, "101", "alice-existing-private", "Alice's private instructions.")
    _install_skill(home, "202", "bob-existing-private", "Bob's private instructions.")
    runner = _runner()

    try:
        with _personal_scope(home, "101"):
            from agent.skill_commands import get_skill_commands

            assert "/alice-existing-private" in get_skill_commands()
        # A newly added personal skill must be reported as added for Alice, even
        # though the cache's personal file-signature tag has changed.
        _install_skill(home, "101", "alice-added-private", "New Alice-only content.")
        alice_source = _source("101")
        alice_event = MessageEvent(text="/reload-skills", source=alice_source)
        with _personal_scope(home, "101"):
            alice_reply = await runner._handle_reload_skills_command(alice_event)
        assert "alice-added-private" in alice_reply
        assert "bob-existing-private" not in alice_reply

        # Switching user identity must not make the previous cache owner appear
        # as a "removed" skill in the next user's reload response.
        bob_event = MessageEvent(text="/reload-skills", source=_source("202"))
        with _personal_scope(home, "202"):
            bob_reply = await runner._handle_reload_skills_command(bob_event)
        assert "bob-existing-private" in bob_reply
        assert "alice-existing-private" not in bob_reply
        assert "alice-added-private" not in bob_reply
    finally:
        _shutdown_runner(runner)
    assert current_personal_memory_context() is None


def test_personal_skill_root_is_not_discoverable_without_authenticated_dm_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "profile"
    skill_dir = _install_skill(home, "101", "private-only", "Private body.")
    token = set_hermes_home_override(home)
    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import normalize_skill_lookup_name

        # No personal context is the group/non-Telegram/default path. The
        # private root is neither registered nor trusted as an absolute lookup.
        assert "/private-only" not in get_skill_commands()
        assert normalize_skill_lookup_name(str(skill_dir)) == str(skill_dir)
    finally:
        reset_hermes_home_override(token)
    assert current_personal_memory_context() is None


@pytest.mark.asyncio
async def test_parallel_personal_slash_expansion_and_exception_cleanup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    _install_skill(home_a, "111", "alpha-private", "ALPHA_PRIVATE_BODY")
    _install_skill(home_b, "222", "beta-private", "BETA_PRIVATE_BODY")
    runner = _runner()

    async def invoke(home: Path, user_id: str, command: str):
        source = _source(user_id)
        event = MessageEvent(text=f"/{command} do this", source=source)
        with _personal_scope(home, user_id):
            result = await runner._run_in_executor_with_context(
                runner._hm_skill_slash_rewrite, event, source, f"task-{user_id}", command
            )
            return result, event.text

    try:
        results = await asyncio.gather(
            invoke(home_a, "111", "alpha-private"),
            invoke(home_b, "222", "beta-private"),
        )
        assert results[0][0] is None and "ALPHA_PRIVATE_BODY" in results[0][1]
        assert "BETA_PRIVATE_BODY" not in results[0][1]
        assert results[1][0] is None and "BETA_PRIVATE_BODY" in results[1][1]
        assert "ALPHA_PRIVATE_BODY" not in results[1][1]
        assert current_personal_memory_context() is None

        async def failing_scope():
            with _personal_scope(home_a, "111"):
                await runner._run_in_executor_with_context(
                    lambda: (_ for _ in ()).throw(RuntimeError("expected test failure"))
                )

        with pytest.raises(RuntimeError, match="expected test failure"):
            await failing_scope()
        assert current_personal_memory_context() is None
    finally:
        _shutdown_runner(runner)


@pytest.mark.asyncio
async def test_registered_skill_that_cannot_load_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "profile"
    _install_skill(home, "101", "known-but-unavailable", "Body.")
    runner = _runner()
    source = _source("101")
    event = MessageEvent(text="/known-but-unavailable do not send as plain text", source=source)

    try:
        with _personal_scope(home, "101"):
            from unittest.mock import patch

            with patch("agent.skill_commands.build_skill_invocation_message", return_value=None):
                reply = await runner._run_in_executor_with_context(
                    runner._hm_skill_slash_rewrite, event, source, "task-101", "known-but-unavailable"
                )
            raised_event = MessageEvent(text=event.text, source=source)
            with patch("agent.skill_commands.build_skill_invocation_message", side_effect=RuntimeError("read failed")):
                raised_reply = await runner._run_in_executor_with_context(
                    runner._hm_skill_slash_rewrite, raised_event, source, "task-101", "known-but-unavailable"
                )
        assert reply == "Failed to load skill /known-but-unavailable."
        assert event.text == "/known-but-unavailable do not send as plain text"
        assert raised_reply == "Failed to load skill /known-but-unavailable."
        assert raised_event.text == event.text
    finally:
        _shutdown_runner(runner)
    assert current_personal_memory_context() is None
