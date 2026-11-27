"""Behaviour contracts for /bstop: the hammer kills the turn, the talk
record, and the peer's side, and a killed talk never comes back.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.b2b_bstop import handle_bstop
from plugins.platforms.telegram.b2b_handshake import (
    CODE_LEAVE,
    live_talk_record,
    talk_session_key,
)
from plugins.platforms.telegram.bot_operations import BotOperationsStore
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

PEER = 42
GROUP = "-100123"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n")
    return home


def _adapter(home: Path) -> TelethonAdapter:
    adapter = TelethonAdapter(PlatformConfig())
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_id = 999
    adapter._authorized = lambda _source: True
    adapter._session_key_profile = lambda _source: None
    adapter.send = AsyncMock()
    return adapter


def _source(home: Path, *, user_id="100", chat_type="dm", chat_id="100"):
    return SimpleNamespace(
        home=home, user_id=str(user_id), chat_id=str(chat_id), chat_type=chat_type,
        thread_id=None, is_bot=False, username="owner", display_name="Owner",
        profile=None,
    )


def _add_peer(home: Path) -> BotOperationsStore:
    store = BotOperationsStore(home)
    store.add_peer({"id": PEER, "username": "peer42_bot"},
                   group_enabled=True, dm_enabled=True, group_chat_id=GROUP)
    return store


@pytest.mark.asyncio
async def test_bstop_kills_turn_talk_and_signals_the_peer(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    store.upsert_talk(PEER, live_talk_record(PEER))
    adapter = _adapter(home)
    interrupted = []
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: home,
        _interrupt_and_clear_session=AsyncMock(
            side_effect=lambda *a, **k: interrupted.append(a[0])),
    )
    text = await handle_bstop(adapter, _source(home), "")
    assert store.get_talk(PEER)["state"] == "killed"
    assert interrupted == [talk_session_key(None, PEER)]
    sent_texts = [call.args[1] for call in adapter.send.await_args_list]
    assert any(t.startswith(str(CODE_LEAVE)) and str(PEER) in t for t in sent_texts)
    assert "Stopped" in text


@pytest.mark.asyncio
async def test_bstop_with_no_live_talk_reports_nothing_to_stop(tmp_path):
    home = _home(tmp_path)
    _add_peer(home)
    adapter = _adapter(home)
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: home,
        _interrupt_and_clear_session=AsyncMock(),
    )
    text = await handle_bstop(adapter, _source(home), "")
    assert "No live talk" in text
    assert [call.args[1] for call in adapter.send.await_args_list] == [text]


@pytest.mark.asyncio
async def test_bstop_refuses_non_owners_and_kills_nothing(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    store.upsert_talk(PEER, live_talk_record(PEER))
    adapter = _adapter(home)
    adapter._is_owner_user = lambda *a, **k: False
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: home,
        _interrupt_and_clear_session=AsyncMock(),
    )
    text = await handle_bstop(adapter, _source(home, user_id="201"), "")
    assert "owner" in text.lower()
    assert store.get_talk(PEER)["state"] == "live"
    adapter.gateway_runner._interrupt_and_clear_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_bstop_by_peer_id_kills_that_peer_from_a_group(tmp_path):
    home = _home(tmp_path)
    store = _add_peer(home)
    store.upsert_talk(PEER, live_talk_record(PEER))
    adapter = _adapter(home)
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda src: home,
        _interrupt_and_clear_session=AsyncMock(),
    )
    text = await handle_bstop(adapter, _source(home, chat_type="group", chat_id=GROUP), str(PEER))
    assert store.get_talk(PEER)["state"] == "killed"
    sent_texts = [call.args[1] for call in adapter.send.await_args_list]
    assert any(GROUP in t and str(CODE_LEAVE) in t for t in sent_texts)
    assert "Stopped" in text


def test_bstop_is_registered_as_an_interrupt_command():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("bstop")
    assert cmd is not None
    assert cmd.busy_policy == "interrupt_then_dispatch"
