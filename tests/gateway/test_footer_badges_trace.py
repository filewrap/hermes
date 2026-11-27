"""Footer-badge trace: every badge decision must be provable from host logs.

Live 2026-11-24: footer ON + beta ON, the reply shows the footer line and
no badges, ever — and the host logs carry no badge payload at all,
so nothing proves where the badges went. The trace found no code-level drop
point between the badge build and the wire (each link is covered by the
sibling repro tests), so these tests pin the logging that discriminates the
two remaining hypotheses on the next live turn: badges never built, or the
request carried the rich row and the server did not render it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin


def _source():
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id="6802929470",
                           chat_type="dm", user_id="6802929470", is_bot=False)


def _agent_result():
    """Live shape: gpt-6-luna on openai-codex, 14% context, cwd under home."""
    return {"model": "gpt-6-luna", "provider": "openai-codex",
            "last_prompt_tokens": 38080, "context_length": 272000,
            "requested_model": None, "served_model": None}


def _live_config(*, footer=True, beta=True, link_style=False):
    return {
        "display": {"runtime_footer": {"enabled": footer,
                                       "fields": ["model", "context_pct", "cwd"]}},
        "platforms": {"telegram": {"footer_badges": beta,
                                   "footer_badges_link_style": link_style}},
    }


@pytest.fixture
def _mixin(monkeypatch):
    monkeypatch.setattr("gateway.run._terminal_scope_cwd", lambda _default: "/home/test")
    return GatewayTurnPersistenceMixin()


def test_badge_build_logs_info_with_count_and_fields(_mixin, monkeypatch, caplog):
    """Footer ON + beta ON builds the badges and says so at INFO."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config())
    caplog.set_level(logging.INFO, logger="gateway.run")
    badges = _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0)
    assert badges[0] == ("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna")
    assert ("14%", "fb:c:38080|272000") in badges
    assert ("/home/test", "fb:p:/home/test") in badges
    assert "footer badges built: 3 badges (fields=model,context_pct,cwd)" in caplog.text


def test_badge_build_failure_logs_warning_and_line_still_rides(
        _mixin, monkeypatch, caplog):
    """A badge build that raises must not take the footer line with it."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config())
    def _raise(**kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr("gateway.runtime_footer.build_footer_badges", _raise)
    caplog.set_level(logging.INFO, logger="gateway.run")
    badges = _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0)
    assert badges == []
    assert "footer badges build failed: boom" in caplog.text
    event = SimpleNamespace()
    out = _mixin._hmwa_attach_footer(
        event, _agent_result(), "Heya! \U0001f44b", "gpt-6-luna · 14% · /home/test",
        badges, False)
    assert str(out) == "Heya! \U0001f44b\n\ngpt-6-luna · 14% · /home/test"


def test_beta_off_logs_info_reason(_mixin, monkeypatch, caplog):
    """Footer ON + beta OFF: no badges, and the log says why."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config(beta=False))
    caplog.set_level(logging.INFO, logger="gateway.run")
    assert _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0) == []
    assert "footer badges: none (footer badges beta off)" in caplog.text


def test_footer_off_logs_info_reason(_mixin, monkeypatch, caplog):
    """Footer OFF: no badges, and the log says why."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config(footer=False))
    caplog.set_level(logging.INFO, logger="gateway.run")
    assert _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0) == []
    assert "footer badges: none (footer disabled)" in caplog.text


def test_attach_logs_info_when_badges_attached(_mixin, monkeypatch, caplog):
    """Badges ride the footer text itself: the attach returns HTML carrying
    the badges nested inline in the footer paragraph, logs the send, and
    sets nothing on the event."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config())
    caplog.set_level(logging.INFO, logger="gateway.run")
    badges = _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0)
    event = SimpleNamespace()
    out = _mixin._hmwa_attach_footer(
        event, _agent_result(), "Heya! \U0001f44b", "gpt-6-luna · 14% · /home/test",
        badges, False)
    assert not hasattr(event, "_footer_badges")
    assert "<tg-button-row" not in str(out)
    assert str(out).endswith('</p>')
    assert 'data="fb:m:openai-codex|gpt-6-luna"' in str(out)
    assert "footer badges sent: html form, 3 badges" in caplog.text


def test_attach_renders_link_style_badges_when_new_style_on(_mixin, monkeypatch):
    """New style on: the attached footer renders the same badges as
    link-style buttons, and no primary button remains."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config(link_style=True))
    badges = _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0)
    event = SimpleNamespace(source=_source())
    out = _mixin._hmwa_attach_footer(
        event, _agent_result(), "Heya! \U0001f44b", "gpt-6-luna · 14% · /home/test",
        badges, False)
    assert 'style="link"' in str(out)
    assert 'style="primary"' not in str(out)
    assert 'data="fb:m:openai-codex|gpt-6-luna"' in str(out)


def test_attach_stays_primary_when_new_style_off(_mixin, monkeypatch):
    """New style off (the default): the attached footer renders primary badges."""
    monkeypatch.setattr("gateway.run._load_gateway_config",
                        lambda *a, **k: _live_config())
    badges = _mixin._hmwa_runtime_footer_badges(_agent_result(), _source(), 2.0)
    event = SimpleNamespace(source=_source())
    out = _mixin._hmwa_attach_footer(
        event, _agent_result(), "Heya! \U0001f44b", "gpt-6-luna · 14% · /home/test",
        badges, False)
    assert 'style="primary"' in str(out)
    assert 'style="link"' not in str(out)
