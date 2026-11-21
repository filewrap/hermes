"""Scam/fake-flagged recipients get a button-free /settings card.

The sender's Telethon ``User`` carries ``scam``/``fake`` flags. The adapter
copies them onto the source at inbound time; the settings UI renders without
buttons for flagged recipients and retries a rejected buttoned send without
buttons before reporting failure.
"""

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _inbound_event(*, sender):
    message = SimpleNamespace(chat_id=42, id=7, reply_to=None, action=None)
    chat = SimpleNamespace(
        first_name="Test", title=None, broadcast=False,
        megagroup=False, gigagroup=False, forum=False,
    )
    return SimpleNamespace(
        message=message, sender=sender, chat=chat, chat_id=42, sender_id=42,
    )


@pytest.mark.asyncio
async def test_inbound_sender_scam_flag_reaches_source():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    sender = SimpleNamespace(
        id=42, first_name="Scam", last_name=None, username=None,
        bot=False, scam=True, fake=False,
    )

    source = await adapter._source(_inbound_event(sender=sender))

    assert source.sender_scam is True
    assert source.sender_fake is False


@pytest.mark.asyncio
async def test_inbound_sender_without_flags_is_known_unflagged():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    sender = SimpleNamespace(
        id=42, first_name="Plain", last_name=None, username=None,
        bot=False, scam=None, fake=None,
    )

    source = await adapter._source(_inbound_event(sender=sender))

    assert source.sender_scam is False
    assert source.sender_fake is False


@pytest.mark.asyncio
async def test_inbound_sender_unknown_when_no_sender_object():
    adapter = TelethonAdapter(PlatformConfig(extra={}))

    source = await adapter._source(_inbound_event(sender=None))

    assert source.sender_scam is None
    assert source.sender_fake is None


def test_scam_flags_stay_off_the_wire():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    source = adapter.build_source(
        "42", user_id="42", sender_scam=True, sender_fake=False,
    )

    assert source.sender_scam is True
    assert "sender_scam" not in source.to_dict()
    assert "sender_fake" not in source.to_dict()


# --- Commit B: button-free rendering for flagged recipients ---------------

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingsSource:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None
    sender_scam: bool | None = None
    sender_fake: bool | None = None


@dataclass(frozen=True)
class UnknownFlagSource:
    """A source built before the flags existed: no scam/fake attributes."""

    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None


class FakeSettingsAdapter:
    def __init__(self, home):
        self._client = object()
        self.requests = []
        self.sends = []
        self._next_message_id = 100
        self.fail_buttoned_once = False
        self.fail_always = False
        self.gateway_runner = SimpleNamespace(
            _resolve_profile_home_for_source=lambda _source: home,
        )

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        if self.fail_always:
            raise RuntimeError("send rejected")
        if self.fail_buttoned_once and request.reply_markup is not None:
            self.fail_buttoned_once = False
            raise RuntimeError("send rejected")
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)

    async def send(self, chat_id, text, **kwargs):
        self.sends.append((chat_id, text))


class FakeClient:
    """Stands in for the Telethon client; answers get_entity from a fixed user."""

    def __init__(self, *, scam=False, fake=False, fail=False):
        self._scam = scam
        self._fake = fake
        self._fail = fail
        self.get_entity_calls = []

    async def get_entity(self, user_id):
        self.get_entity_calls.append(user_id)
        if self._fail:
            raise RuntimeError("lookup unavailable")
        return SimpleNamespace(id=user_id, scam=self._scam, fake=self._fake)


def _settings_ui(adapter):
    from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

    return TelegramPersonalSettingsUI(adapter)


@pytest.mark.asyncio
async def test_flagged_user_home_view_has_no_buttons_but_keeps_settings(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    ui = _settings_ui(adapter)
    source = SettingsSource()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    state["no_buttons"] = True

    text, markup = ui._home_view(state)

    assert markup is None
    assert "tg-button" not in text
    assert "ps:" not in text
    assert "Personal settings" in text
    assert "Memory" in text


@pytest.mark.asyncio
async def test_unflagged_user_home_view_keeps_buttons(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    ui = _settings_ui(adapter)
    source = SettingsSource()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    text, markup = ui._home_view(state)

    assert markup is not None
    assert "tg-button" in text


@pytest.mark.asyncio
async def test_flagged_user_open_sends_button_free_card(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    ui = _settings_ui(adapter)

    await ui.open(SettingsSource(sender_scam=True), reply_to=200)

    assert len(adapter.requests) == 1
    assert adapter.requests[0].reply_markup is None
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_unflagged_user_open_sends_buttoned_card(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    ui = _settings_ui(adapter)

    await ui.open(SettingsSource(sender_scam=False, sender_fake=False), reply_to=200)

    assert len(adapter.requests) == 1
    assert adapter.requests[0].reply_markup is not None
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_unknown_flag_fetches_sender_once_and_caches(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    client = FakeClient(scam=True)
    adapter._client = client
    ui = _settings_ui(adapter)

    await ui.open(UnknownFlagSource(), reply_to=200)
    await ui.open(UnknownFlagSource(), reply_to=201)

    assert client.get_entity_calls == [123]
    assert len(adapter.requests) == 2
    assert all(request.reply_markup is None for request in adapter.requests)


@pytest.mark.asyncio
async def test_flag_lookup_failure_fails_open_with_buttons(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    adapter._client = FakeClient(fail=True)
    ui = _settings_ui(adapter)

    await ui.open(UnknownFlagSource(), reply_to=200)

    assert len(adapter.requests) == 1
    assert adapter.requests[0].reply_markup is not None
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_truthy_but_not_true_flag_does_not_block_buttons(tmp_path):
    """Only an explicit True flag blocks buttons; anything else fails open.

    Guards the lookup against entities whose flag attributes are truthy
    without being True (a mock client, a future non-bool shape).
    """
    adapter = FakeSettingsAdapter(tmp_path)

    class TruthyFlagClient:
        async def get_entity(self, user_id):
            return SimpleNamespace(id=user_id, scam=object(), fake="no")

    adapter._client = TruthyFlagClient()
    ui = _settings_ui(adapter)

    await ui.open(UnknownFlagSource(), reply_to=200)

    assert len(adapter.requests) == 1
    assert adapter.requests[0].reply_markup is not None
    assert adapter.sends == []


# --- Commit C: fail-open retry -------------------------------------------

@pytest.mark.asyncio
async def test_buttoned_send_failure_retries_without_buttons(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    adapter.fail_buttoned_once = True
    ui = _settings_ui(adapter)

    await ui.open(SettingsSource(sender_scam=False, sender_fake=False), reply_to=200)

    assert len(adapter.requests) == 2
    assert adapter.requests[0].reply_markup is not None
    assert adapter.requests[1].reply_markup is None
    assert adapter.sends == []
    assert next(iter(ui._states.values()))["message_id"] is not None


@pytest.mark.asyncio
async def test_button_free_retry_failure_shows_error_message(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    adapter.fail_always = True
    ui = _settings_ui(adapter)

    await ui.open(SettingsSource(sender_scam=False, sender_fake=False), reply_to=200)

    assert len(adapter.requests) == 2
    assert adapter.requests[0].reply_markup is not None
    assert adapter.requests[1].reply_markup is None
    assert adapter.sends == [("123", "Could not open personal settings. Please try again.")]


@pytest.mark.asyncio
async def test_flagged_user_send_failure_does_not_retry_pointlessly(tmp_path):
    adapter = FakeSettingsAdapter(tmp_path)
    adapter.fail_always = True
    ui = _settings_ui(adapter)

    await ui.open(SettingsSource(sender_scam=True), reply_to=200)

    assert len(adapter.requests) == 1
    assert adapter.requests[0].reply_markup is None
    assert adapter.sends == [("123", "Could not open personal settings. Please try again.")]
