"""Regression: bare /footer reports the footer state; it never toggles it.

Live incident 2026-11-24 ~15:21 IST: the footer was ON (the 3:21:46 IST reply
showed the footer line), the user ran bare /footer to CHECK whether it was on
or off, and the command toggled it OFF — the card reported the new state OFF
and the replies after ("T", "Hey") had no footer. Bare /footer is a check,
not a switch: only /footer on|off change the setting.
"""
from __future__ import annotations

import json

import pytest

from gateway import run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.config import read_user_config_raw


def _runner():
    runner = object.__new__(run.GatewayRunner)
    runner.adapters = {}
    return runner


def _event(text: str) -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    return MessageEvent(text=text, source=source)


def _seed_config(tmp_path, enabled: bool) -> None:
    (tmp_path / "config.yaml").write_text(
        json.dumps({"display": {"runtime_footer": {"enabled": enabled}}}))


def _stored_enabled(tmp_path) -> bool:
    return bool(read_user_config_raw(tmp_path / "config.yaml")["display"]["runtime_footer"]["enabled"])


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_gateway_config_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_bare_footer_reports_on_without_changing_it(home):
    """Footer ON: bare /footer shows the ON status card and leaves the setting on."""
    _seed_config(home, True)
    reply = await _runner()._handle_footer_command(_event("/footer"))
    assert "**ON**" in reply, reply
    assert "saved globally" not in reply, reply
    assert _stored_enabled(home) is True


@pytest.mark.asyncio
async def test_bare_footer_reports_off_without_changing_it(home):
    """Footer OFF: bare /footer shows the OFF status card and leaves the setting off."""
    _seed_config(home, False)
    reply = await _runner()._handle_footer_command(_event("/footer"))
    assert "**OFF**" in reply, reply
    assert "saved globally" not in reply, reply
    assert _stored_enabled(home) is False


@pytest.mark.asyncio
async def test_bare_footer_card_matches_status_card(home):
    """Bare /footer shows exactly the card /footer status shows."""
    _seed_config(home, True)
    runner = _runner()
    bare = await runner._handle_footer_command(_event("/footer"))
    status = await runner._handle_footer_command(_event("/footer status"))
    assert bare == status, (bare, status)
    assert _stored_enabled(home) is True


@pytest.mark.asyncio
async def test_footer_on_still_enables(home):
    """/footer on still sets the footer on (the only way to turn it on)."""
    _seed_config(home, False)
    reply = await _runner()._handle_footer_command(_event("/footer on"))
    assert "saved globally" in reply, reply
    assert _stored_enabled(home) is True


@pytest.mark.asyncio
async def test_footer_off_still_disables(home):
    """/footer off still sets the footer off."""
    _seed_config(home, True)
    reply = await _runner()._handle_footer_command(_event("/footer off"))
    assert "saved globally" in reply, reply
    assert _stored_enabled(home) is False


@pytest.mark.asyncio
async def test_footer_status_still_reports_without_changing_it(home):
    """/footer status keeps reporting without changing anything."""
    _seed_config(home, True)
    reply = await _runner()._handle_footer_command(_event("/footer status"))
    assert "**ON**" in reply, reply
    assert _stored_enabled(home) is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
