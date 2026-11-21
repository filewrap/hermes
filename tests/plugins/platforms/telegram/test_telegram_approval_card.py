"""Telegram exec-approval card invariants (Phase 4, F3)."""

from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def test_approval_card_header_has_no_emoji():
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    text = adapter._format_exec_approval("rm -rf /tmp/x", "dangerous command")
    assert "⚠️" not in text
    assert "Hermes needs your ok • (approval pending)" in text
    assert "If you don't answer within" in text
