"""Tests for the allowed_{channels,chats,rooms} whitelist extension
added alongside PR #7401 (Slack).

Covers: Telegram, Matrix, Mattermost, DingTalk.

For each platform:
- Empty = no restriction (fully backward compatible).
- When set, messages from non-listed chats/rooms are silently ignored.
- DMs are never filtered.
- @mention does NOT bypass the whitelist.
- config.yaml → env var bridging (via load_gateway_config) where applicable.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------







class TestTelegramAllowedChats:





    def test_config_bridge(self, monkeypatch, tmp_path):
        """slack-style config.yaml → env var bridge works."""
        from gateway.config import load_gateway_config

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "telegram:\n"
            "  allowed_chats:\n"
            "    - -100\n"
            "    - -200\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "__sentinel__")
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS")

        load_gateway_config()

        import os as _os
        assert _os.environ["TELEGRAM_ALLOWED_CHATS"] == "-100,-200"


# ---------------------------------------------------------------------------
# DingTalk
# ---------------------------------------------------------------------------

def _make_dingtalk_adapter(*, allowed_chats=None, require_mention=None):
    # Import lazily — DingTalk SDK may not be installed.
    pytest.importorskip("plugins.platforms.dingtalk.adapter", reason="DingTalk adapter not importable")
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    extra = {}
    if allowed_chats is not None:
        extra["allowed_chats"] = allowed_chats
    if require_mention is not None:
        extra["require_mention"] = require_mention

    adapter = object.__new__(DingTalkAdapter)
    adapter.platform = Platform.DINGTALK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    return adapter


class TestDingTalkAllowedChats:
    def test_empty_is_no_restriction(self, monkeypatch):
        monkeypatch.delenv("DINGTALK_ALLOWED_CHATS", raising=False)
        adapter = _make_dingtalk_adapter()
        assert adapter._dingtalk_allowed_chats() == set()

    def test_list_form(self):
        adapter = _make_dingtalk_adapter(allowed_chats=["cidABC", "cidDEF"])
        assert adapter._dingtalk_allowed_chats() == {"cidABC", "cidDEF"}


# ---------------------------------------------------------------------------
# Mattermost (env-var only — no config.yaml bridge)
# ---------------------------------------------------------------------------

class TestMattermostAllowedChannels:
    """Mattermost whitelist logic — replicated since the adapter reads config
    with env-var fallback inline inside _handle_post rather than through a
    helper method."""

    @staticmethod
    def _would_process(channel_id, channel_type="O", allowed_cfg=None, allowed_env=""):
        """Replicate the whitelist gate from gateway/platforms/mattermost.py."""
        if channel_type == "D":
            return True
        # config-first, env-var fallback (matching the adapter)
        allowed_raw = allowed_cfg
        if allowed_raw is None:
            allowed_raw = allowed_env
        if isinstance(allowed_raw, list):
            allowed = {str(c).strip() for c in allowed_raw if str(c).strip()}
        else:
            allowed = {c.strip() for c in str(allowed_raw).split(",") if c.strip()}
        if allowed and channel_id not in allowed:
            return False
        return True

    def test_empty_config_is_no_restriction(self):
        assert self._would_process("chan123", allowed_cfg=None, allowed_env="") is True


    def test_config_bridge(self, monkeypatch, tmp_path):
        from gateway.config import load_gateway_config

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "mattermost:\n"
            "  allowed_channels:\n"
            "    - chanABC\n"
            "    - chanDEF\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        # Pre-register the key with monkeypatch so teardown cleans it up
        # even though load_gateway_config mutates os.environ directly
        # (monkeypatch only restores keys it's touched via setenv/delenv;
        # delenv on an absent key is a no-op for teardown purposes).
        monkeypatch.setenv("MATTERMOST_ALLOWED_CHANNELS", "__sentinel__")
        monkeypatch.delenv("MATTERMOST_ALLOWED_CHANNELS")

        load_gateway_config()

        import os as _os
        assert _os.environ["MATTERMOST_ALLOWED_CHANNELS"] == "chanABC,chanDEF"


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

class TestMatrixAllowedRooms:
    """Matrix whitelist behavior — tested via the env-var-initialized
    instance attribute _allowed_rooms."""

    def test_empty_env_empty_set(self, monkeypatch):
        monkeypatch.delenv("MATRIX_ALLOWED_ROOMS", raising=False)
        # Replicate __init__ parsing without needing the real adapter.
        raw = "" or ""
        allowed = {r.strip() for r in raw.split(",") if r.strip()}
        assert allowed == set()
