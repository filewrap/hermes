"""Offline regression coverage for managed Telegram bot operations and confirmation UI."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.bot_operations import (
    BOT_OPERATION_PARAMETERS,
    BotOperationsError,
    BotOperationsStore,
    TelegramBotOperations,
)
from plugins.platforms.telegram.bot_operations_ui import TelegramBotOperationsUI
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class FakeClient:
    """Local MTProto double; records actual Telethon requests, never opens a network connection."""

    def __init__(self):
        self.requests = []
        self.entities = []
        self.sent = []
        self.send_result = SimpleNamespace(id=701)
        self.call_result = True
        self.settings_users = []

    async def get_entity(self, peer_id):
        if peer_id == "@peer42_bot":
            peer_id = 42
        peer_id = int(peer_id)
        self.entities.append(peer_id)
        is_bot = peer_id in {42, 77}
        return SimpleNamespace(
            id=peer_id, bot=is_bot, username=f"peer{peer_id}_bot" if is_bot else None,
            first_name="Peer" if is_bot else "Owner", last_name="Bot" if is_bot else "",
            access_hash=peer_id * 10,
        )

    async def get_input_entity(self, entity):
        if isinstance(entity, (int, str)):
            entity = await self.get_entity(entity)
        return types.InputUser(user_id=int(entity.id), access_hash=int(entity.access_hash))

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetAccessSettingsRequest":
            return SimpleNamespace(restricted=True, add_users=self.settings_users)
        if name == "SendMessageRequest":
            return SimpleNamespace(id=500)
        if name == "EditMessageRequest":
            return SimpleNamespace(id=getattr(request, "id", 500))
        return self.call_result

    async def send_message(self, destination, text, *, reply_to=None, parse_mode=None):
        assert parse_mode is None
        self.sent.append((destination, text, reply_to))
        return self.send_result


class CallbackEvent:
    def __init__(self, message_id=500, actor_id=100):
        self.query = SimpleNamespace(msg_id=message_id, user_id=actor_id)
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", alert))


def _profile(tmp_path: Path, name: str, owner: str):
    home = tmp_path / name
    home.mkdir()
    (home / ".env").write_text(f"OWNER_TELEGRAM_ID={owner}\nTELEGRAM_ALLOWED_USERS={owner}\n")
    return home


def _source(home, *, user_id="100", chat_id="100", chat_type="dm", thread_id=None, is_bot=False):
    return SimpleNamespace(home=home, user_id=str(user_id), chat_id=str(chat_id), chat_type=chat_type,
                           thread_id=thread_id, is_bot=is_bot, username="peer42_bot", display_name="Peer Bot")


def _adapter(tmp_path, homes=None):
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: source.home,
    )
    # Ownership is still verified from real per-profile .env owner files; only transport
    # authorization is isolated from secret/environment policy in these offline tests.
    adapter._authorized = lambda _source: True
    adapter._client = FakeClient()
    adapter._peer_cache["100"] = types.InputPeerUser(user_id=100, access_hash=1000)
    return adapter


def _callback(markup, *, suffix=""):
    for row in markup.rows:
        for wrapped in row.buttons:
            button = getattr(wrapped, "button", wrapped)
            data = button.type.data.decode()
            if data.startswith("tgb:") and (not suffix or suffix in button.text.lower()):
                return data
    raise AssertionError("expected operation callback was not rendered")


def _last_request(client, name):
    return next(request for request in reversed(client.requests) if type(request).__name__ == name)


def _assert_tl_serializes(request):
    encoded = bytes(request)
    assert isinstance(encoded, bytes) and encoded
    return encoded


@pytest.mark.asyncio
async def test_owner_profile_scope_cycles_a_b_a_using_real_store_and_owner_files(tmp_path):
    a = _profile(tmp_path, "profile-a", "100")
    b = _profile(tmp_path, "profile-b", "300")
    adapter = _adapter(tmp_path)
    operations = TelegramBotOperations(adapter)

    source_a = _source(a)
    source_b = _source(b, user_id="300", chat_id="300")
    assert adapter._settings_home(source_a) == a
    assert adapter._settings_home(source_b) == b
    assert adapter._is_owner_user("100", source_a)
    assert adapter._is_owner_user("300", source_b)
    assert not adapter._is_owner_user("100", source_b)

    BotOperationsStore(operations.profile_home(source_a)).add_peer(
        {"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    assert [peer["id"] for peer in (await operations.request(source_a, "peers.list"))["peers"]] == [42]
    assert (await operations.request(source_b, "peers.list"))["peers"] == []
    BotOperationsStore(operations.profile_home(source_b)).add_peer(
        {"id": 77, "username": "peer77_bot"}, group_enabled=False, dm_enabled=True)
    assert [peer["id"] for peer in (await operations.request(source_a, "peers.list"))["peers"]] == [42]
    assert [peer["id"] for peer in (await operations.request(source_b, "peers.list"))["peers"]] == [77]


@pytest.mark.asyncio
async def test_read_routes_are_offline_and_never_call_user_only_admined_bots_method(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ops = TelegramBotOperations(adapter)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot"})

    roster = await ops.request(_source(home), "managed.list")
    assert roster["ok"] and roster["bots"][0]["id"] == 42
    assert (await ops.request(_source(home), "peers.list"))["peers"] == []
    assert (await ops.request(_source(home), "inbox.read"))["messages"] == []
    assert adapter._client.requests == []
    assert "managed.list" in BOT_OPERATION_PARAMETERS


@pytest.mark.asyncio
async def test_request_confirmation_executes_once_and_rich_card_edits_with_real_receipt(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    pending = await ui.request(source, "peer.add", {
        "bot_id": "42", "group_enabled": False, "dm_enabled": True,
    })
    assert pending["confirmation_required"]
    card = next(iter(ui._cards.values()))
    assert card["message_id"] == "500"
    assert card["confirm_id"] and card["confirm_expires"] > 0
    sent = _last_request(adapter._client, "SendMessageRequest")
    _assert_tl_serializes(sent)
    token = _callback(sent.reply_markup, suffix="confirm")

    event = CallbackEvent()
    assert await ui.handle_callback(event, source, token)
    peer = BotOperationsStore(home).peer(42)
    assert peer["dm_enabled"] is True and peer["group_chat_ids"] == []
    assert len(adapter._client.entities) == 3  # lookup before approval, identity recheck, and test-message destination at confirmation
    edited = _last_request(adapter._client, "EditMessageRequest")
    _assert_tl_serializes(edited)
    assert isinstance(edited.rich_message, types.InputRichMessageHTML)
    assert edited.id == 500

    requests_before = len(adapter._client.requests)
    entities_before = len(adapter._client.entities)
    replay = CallbackEvent()
    await ui.handle_callback(replay, source, token)
    assert len(adapter._client.requests) == requests_before
    assert len(adapter._client.entities) == entities_before
    assert "expired" in replay.answers[-1][0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_source", ["actor", "chat", "thread", "profile"])
async def test_callbacks_are_bound_to_owner_actor_chat_thread_and_profile(tmp_path, wrong_source):
    home = _profile(tmp_path, "profile-a", "100")
    other_home = _profile(tmp_path, "profile-b", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    owner = _source(home)
    await ui.request(owner, "peer.add", {"bot_id": "42", "group_enabled": False, "dm_enabled": True})
    send = _last_request(adapter._client, "SendMessageRequest")
    token = _callback(send.reply_markup, suffix="confirm")

    if wrong_source == "actor":
        wrong = _source(home, user_id="999")
    elif wrong_source == "chat":
        wrong = _source(home, chat_id="200")
    elif wrong_source == "thread":
        wrong = _source(home, thread_id="900")
    else:
        wrong = _source(other_home)
    before = len(adapter._client.entities)
    event = CallbackEvent()
    await ui.handle_callback(event, wrong, token)
    assert BotOperationsStore(home).peer(42) is None
    assert len(adapter._client.entities) == before
    # A wrong actor fails the owner gate first; a wrong chat/thread/profile
    # reaches the binding check and mismatches instead of expiring.
    expected = ("Owner authorization required" if wrong_source == "actor"
                else "This control does not match this message.")
    assert event.answers[-1][0] == expected


@pytest.mark.asyncio
async def test_confirmation_expiry_and_revoked_peer_fail_without_outbound_send(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)

    # An expired peer.remove confirmation changes nothing (peer.send no longer
    # stages a confirmation card, so expiry is exercised on a removal instead).
    await ui.request(source, "peer.remove", {"bot_id": "42"})
    card = next(iter(ui._cards.values()))
    remove = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup, suffix="confirm")
    card["confirm_expires"] = 0
    await ui.handle_callback(CallbackEvent(), source, remove)
    assert store.peer(42) is not None and adapter._client.sent == []

    # A peer.send validated before the peer is revoked fails at confirm with no send.
    ops = TelegramBotOperations(adapter)
    pending = await ops.request(source, "peer.send", bot_id="42", text="hello again")
    assert pending["confirmation_required"]
    store.remove_peer(42)
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert not result["ok"] and adapter._client.sent == []


@pytest.mark.asyncio
async def test_group_permission_grants_and_sends_are_exact_current_chat_and_never_promote_dm(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    source = _source(home, chat_id="-200", chat_type="group")
    ops = TelegramBotOperations(adapter)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=True,
                   dm_enabled=False, group_chat_id="-200")

    wrong_grant = await ops.request(source, "peer.set_permissions", bot_id="42",
                                    group_enabled=True, dm_enabled=True, group_chat_id="-201")
    assert not wrong_grant["ok"]
    wrong_destination = await ops.request(source, "peer.send", bot_id="42", scope="group",
                                          group_chat_id="-201", text="hello")
    assert not wrong_destination["ok"]
    assert adapter._client.sent == []

    pending = await ops.request(source, "peer.send", bot_id="42", text="hello")
    assert pending["confirmation_required"] and pending["chat_id"] == "-200"
    result = await ops.confirm(source, pending["operation"], **{
        key: value for key, value in pending.items()
        if key not in {"ok", "confirmation_required", "message", "operation"}
    })
    assert result["ok"] is True
    assert adapter._client.sent[0][1] == "@peer42_bot hello"
    assert adapter._client.sent[0][2] is None

    duplicate = await ops.confirm(source, pending["operation"], **{
        key: value for key, value in pending.items()
        if key not in {"ok", "confirmation_required", "message", "operation"}
    })
    assert not duplicate["ok"] and "duplicate" in duplicate["error"].lower()
    assert len(adapter._client.sent) == 1
    assert store.peer(42)["dm_enabled"] is False


@pytest.mark.asyncio
async def test_peer_send_uses_connected_manager_client_and_false_result_is_not_success(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    source = _source(home)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)

    pending = await ops.request(source, "peer.send", bot_id="42", text="exact payload")
    assert pending["chat_id"] == "42"  # current DM by default; no chat_id override accepted
    adapter._client.send_result = False
    payload = {key: value for key, value in pending.items()
               if key not in {"ok", "confirmation_required", "message", "operation"}}
    result = await ops.confirm(source, pending["operation"], **payload)
    assert result["ok"] is False
    assert "did not confirm" in result["error"].lower()
    assert adapter._client.sent[0][1] == "exact payload"
    assert adapter._client.sent[0][0].id == 42


@pytest.mark.asyncio
async def test_access_settings_get_and_confirm_serialize_actual_telethon_requests(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    source = _source(home)
    ops = TelegramBotOperations(adapter)
    store = BotOperationsStore(home)
    store.record_managed_bot({"id": 42, "username": "peer42_bot"})
    human = SimpleNamespace(id=88, bot=False, username="owner", first_name="Human", last_name="", access_hash=880)
    adapter._client.settings_users = [human]

    current = await ops.request(source, "managed.access.get", bot_id="42")
    assert current["ok"] and current["restricted"] is True
    read_request = _last_request(adapter._client, "GetAccessSettingsRequest")
    _assert_tl_serializes(read_request)
    assert read_request.bot.user_id == 42

    ui = TelegramBotOperationsUI(adapter)
    pending = await ui.request(source, "managed.access.set", {
        "bot_id": "42", "restricted": True, "add_users": [88],
    })
    assert pending["confirmation_required"]
    token = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup, suffix="confirm")
    await ui.handle_callback(CallbackEvent(), source, token)
    edit = _last_request(adapter._client, "EditAccessSettingsRequest")
    _assert_tl_serializes(edit)
    assert edit.bot.user_id == 42 and edit.restricted is True
    assert [user.user_id for user in edit.add_users] == [88]
    assert len([request for request in adapter._client.requests
                if type(request).__name__ == "EditAccessSettingsRequest"]) == 1


@pytest.mark.asyncio
async def test_incoming_approved_bot_messages_are_inbox_only_and_rate_deduplicated(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)
    source = _source(home, user_id="42", chat_id="100", is_bot=True)
    token = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    event = SimpleNamespace(message=SimpleNamespace(id=50, message=f"bot asks for help {token}"), raw_text="")

    assert await ops.handle_incoming(event, source)
    assert await ops.handle_incoming(event, source)
    inbox = (await ops.request(_source(home), "inbox.read"))["messages"]
    assert len(inbox) == 1 and token not in inbox[0]["text"]
    assert "[REDACTED TELEGRAM BOT TOKEN]" in inbox[0]["text"]
    assert adapter._client.sent == []
    assert adapter._client.requests == []

    # Legacy or manually seeded data is sanitized at render time too.
    BotOperationsStore(home).push_inbox({"bot_id": 42, "chat_id": "100", "message_id": "51",
                                         "text": f"legacy {token}", "chat_type": "dm"})
    ui = TelegramBotOperationsUI(adapter)
    await ui.open(_source(home), "inbox")
    rendered = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert token not in rendered
    assert "[REDACTED TELEGRAM BOT TOKEN]" in rendered


@pytest.mark.asyncio
async def test_managed_create_link_is_validated_url_only_and_rendered_as_rich_link(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._bot_username = "HermesManagerBot"
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    missing_manager = await TelegramBotOperations(adapter).request(source, "managed.create_link",
                                                                    username="wonder_bot")
    assert missing_manager["ok"] is False and "manager-mode" in missing_manager["error"].lower()
    adapter._bot_can_manage_bots = True
    invalid = await TelegramBotOperations(ui.adapter).request(source, "managed.create_link",
                                                               username="bad-name")
    assert invalid["ok"] is False
    result = await ui.request(source, "managed.create_link", {"username": "wonder_bot", "name": "My Bot"})
    assert result["ok"] and result["url"].startswith("https://t.me/newbot/HermesManagerBot/wonder_bot?")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert 'url="https://t.me/newbot/HermesManagerBot/wonder_bot?name=My+Bot"' in request.rich_message.html
    assert 'text="@wonder_bot"' in request.rich_message.html
    assert adapter._client.entities == []  # link generation performs no Telegram lookup/API call


def test_managed_create_command_routes_to_link_and_does_not_claim_creation(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._bot_can_manage_bots = True
    adapter._bot_username = "HermesManagerBot"
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    async def run():
        assert await ui.handle_command(None, source, "/managedbots", "create newthing_bot")
        request = _last_request(adapter._client, "SendMessageRequest")
        assert "Open Telegram bot creation" in request.rich_message.html
        assert "has been created" not in request.rich_message.html
        assert adapter._client.entities == []

    import asyncio
    asyncio.run(run())


@pytest.mark.asyncio
async def test_forum_peer_send_sends_mention_text_immediately_with_exact_topic(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._peer_cache["-200"] = types.InputPeerChat(chat_id=200)
    BotOperationsStore(home).add_peer({"id": 42, "username": "peer42_bot"},
        group_enabled=True, dm_enabled=False, group_chat_id="-200")
    ui = TelegramBotOperationsUI(adapter)
    src = _source(home, chat_id="-200", chat_type="forum", thread_id="77")
    # Guard-free: the tool path sends at once, no confirmation card is staged.
    result = await ui.request(src, "peer.send", {"bot_id": 42, "text": "literal **text**"})
    assert result["ok"] is True and result["message_id"] == 701
    assert result["thread_id"] == "77"
    assert adapter._client.sent == [(await adapter._client.get_entity(-200), "@peer42_bot literal **text**", 77)]


@pytest.mark.asyncio
async def test_dm_permission_change_from_owner_dm_does_not_change_any_group_grant(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=True,
                   dm_enabled=False, group_chat_id="-200")
    ops = TelegramBotOperations(adapter)
    pending = await ops.request(_source(home), "peer.set_permissions", bot_id=42,
                                group_enabled=False, dm_enabled=True)
    assert pending["confirmation_required"] and pending["group_chat_id"] is None
    result = await ops.confirm(_source(home), pending["operation"], **{
        key: value for key, value in pending.items() if key not in {"operation", "ok", "confirmation_required"}})
    assert result["ok"] and store.peer(42)["dm_enabled"] is True
    assert store.peer(42)["group_chat_ids"] == ["-200"]
    assert not (await ops.request(_source(home), "peer.set_permissions", bot_id=42,
                                 group_enabled=True, dm_enabled=True, group_chat_id="-201"))["ok"]


@pytest.mark.asyncio
async def test_peer_reply_cannot_borrow_an_inbox_message_from_another_forum_topic(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=True,
                   dm_enabled=False, group_chat_id="-200")
    store.push_inbox({"bot_id": 42, "chat_id": "-200", "message_id": "70",
                      "thread_id": "77", "text": "untrusted peer text"})
    result = await TelegramBotOperations(adapter).request(
        _source(home, chat_id="-200", chat_type="forum", thread_id="88"),
        "peer.send", bot_id=42, text="no cross-topic reply", reply_to=70)
    assert not result["ok"] and adapter._client.sent == []


@pytest.mark.asyncio
async def test_native_managed_unrestrict_previews_and_clears_additional_users(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot"})
    adapter._client.settings_users = [SimpleNamespace(id=88, username=None, first_name="Human", last_name="")]
    ui = TelegramBotOperationsUI(adapter)
    await ui.handle_command(None, _source(home), "managedbots", "unrestrict 42")
    state = next(iter(ui._cards.values()))
    assert state["pending"]["restricted"] is False and state["pending"]["add_users"] == []
    token = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup, suffix="confirm")
    await ui.handle_callback(CallbackEvent(), _source(home), token)
    request = _last_request(adapter._client, "EditAccessSettingsRequest")
    _assert_tl_serializes(request)
    assert request.restricted is False and request.add_users == []


@pytest.mark.asyncio
async def test_exact_username_peer_add_resolves_verified_bot_and_requires_explicit_scopes(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ops = TelegramBotOperations(adapter)
    result = await ops.request(_source(home), "peer.add", bot_id="@peer42_bot",
                               group_enabled=False, dm_enabled=True)
    assert result["confirmation_required"] and result["bot"]["id"] == 42
    assert BotOperationsStore(home).peer(42) is None  # no grant before confirmation
    assert not (await ops.request(_source(home), "peer.add", bot_id="+911234567890",
                                 group_enabled=False, dm_enabled=True))["ok"]
    assert not (await ops.request(_source(home), "peer.add", bot_id=42,
                                 group_enabled="true", dm_enabled=True))["ok"]


def test_bot_operation_store_files_are_private_and_profile_local(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    assert store.path == home / "telegram" / "managed_bot_operations.json"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.with_suffix(".json.lock").stat().st_mode & 0o777 == 0o600


def _button_labels(markup) -> list[str]:
    return [getattr(wrapped, "button", wrapped).text
            for row in markup.rows for wrapped in row.buttons]


@pytest.mark.asyncio
async def test_managed_home_has_create_and_manage_only_and_no_function_names(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "managed")
    request = _last_request(adapter._client, "SendMessageRequest")
    html = request.rich_message.html
    assert "<h1>Managed bots</h1>" in html
    assert "No managed bots in this profile yet." in html
    assert "getAdminedBots" not in html and "bots." not in html
    # Peers and inbox live on /b2b now; the managed home offers only its two actions.
    assert _button_labels(request.reply_markup) == ["Create", "Manage"]


@pytest.mark.asyncio
async def test_manage_menu_shows_bot_details_and_nested_actions(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot", "name": "Peer Bot"})

    await ui.open(source, "manage")
    request = _last_request(adapter._client, "SendMessageRequest")
    html = request.rich_message.html
    assert "<h1>Manage managed bots</h1>" in html
    assert 'url="tg://user?id=42"' in html and "Peer Bot" in html
    assert 'text="@peer42_bot"' in html
    assert 'text="42"' in html
    assert "observed:" in html
    # The inbox is B2B content: no message count and no Messages action here.
    assert "messages:" not in html and "Messages" not in html
    assert "Access settings" in html and "Open in Telegram" in html
    assert 'url="https://t.me/peer42_bot"' in html
    assert "getAdminedBots" not in html and "bots." not in html


@pytest.mark.asyncio
async def test_b2b_home_holds_peers_inbox_and_add_and_botpeers_aliases(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.handle_command(None, source, "/b2b", "")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert "<h1>Bot-to-bot</h1>" in request.rich_message.html
    assert _button_labels(request.reply_markup) == ["Peers", "Inbox", "Add peer", "Banned"]

    await ui.handle_command(None, source, "/botpeers", "")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert "<h1>Bot-to-bot</h1>" in request.rich_message.html

    await ui.open(source, "add_peer")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert "<h1>Add a bot peer</h1>" in request.rich_message.html
    assert 'text="/b2b add &lt;bot&gt; &lt;group|dm|both&gt;"' in request.rich_message.html


@pytest.mark.asyncio
async def test_managed_update_refreshes_open_card_with_new_bot(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "managed")
    assert "No managed bots in this profile yet." in _last_request(
        adapter._client, "SendMessageRequest").rich_message.html

    update = types.UpdateManagedBot(bot_id=42, user_id=100, qts=1)
    result = await ui.operations.handle_managed_update(update, profile_home=str(home))
    assert result["ok"] and result["bot_id"] == 42
    assert await ui.refresh_managed_cards(str(home), 42) == 1
    edited = _last_request(adapter._client, "EditMessageRequest")
    assert isinstance(edited.rich_message, types.InputRichMessageHTML)
    assert "Peer Bot" in edited.rich_message.html
    assert 'text="@peer42_bot"' in edited.rich_message.html


@pytest.mark.asyncio
async def test_create_page_shows_created_bot_after_update(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._bot_can_manage_bots = True
    adapter._bot_username = "HermesManagerBot"
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "create", create_username="peer42_bot")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "Open Telegram bot creation" in html
    assert "now in the roster" not in html

    update = types.UpdateManagedBot(bot_id=42, user_id=100, qts=1)
    await ui.operations.handle_managed_update(update, profile_home=str(home))
    assert await ui.refresh_managed_cards(str(home), 42) == 1
    edited = _last_request(adapter._client, "EditMessageRequest")
    assert "now in the roster" in edited.rich_message.html
    assert "Peer Bot" in edited.rich_message.html
    assert "has been created" not in edited.rich_message.html


@pytest.mark.asyncio
async def test_managedbots_non_owner_bad_args_get_owner_only_refusal(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)

    assert await ui.handle_command(None, _source(home, user_id="999"), "/managedbots", "nonsense")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "<h1>Owner only</h1>" in html
    assert "Managed-bot commands" not in html


@pytest.mark.asyncio
async def test_managed_roster_single_page_has_no_footer_and_note_is_collapsible(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)

    await ui.open(_source(home), "managed")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "Page 1 of 1" not in html
    assert "<details><summary>About this list</summary>" in html
    assert "what this profile has seen" in html


@pytest.mark.asyncio
async def test_managed_roster_multi_page_shows_italic_footer(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    store = BotOperationsStore(home)
    for i in range(7):
        store.record_managed_bot({"id": 1000 + i, "username": f"bot{i}_bot", "name": f"Bot {i}"})

    await ui.open(_source(home), "managed")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "<i>Page 1 of 2" in html


@pytest.mark.asyncio
async def test_managed_roster_name_is_profile_pill(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot", "name": "Peer Bot"})

    await ui.open(_source(home), "managed")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert 'url="tg://user?id=42"' in html
    assert "Peer Bot" in html


@pytest.mark.asyncio
async def test_managed_result_card_nav_has_no_b2b_button(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)

    await ui.handle_command(None, _source(home), "/managedbots", "nonsense")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert _button_labels(request.reply_markup) == ["Managed bots"]
    assert "Page 1 of 1" not in request.rich_message.html


@pytest.mark.asyncio
async def test_b2b_result_card_nav_has_no_managed_button(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)

    await ui.handle_command(None, _source(home), "/b2b", "nonsense")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert _button_labels(request.reply_markup) == ["Bot-to-bot"]
    assert "Page 1 of 1" not in request.rich_message.html


@pytest.mark.asyncio
async def test_access_view_shows_restricted_badge_not_text(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot"})
    ui = TelegramBotOperationsUI(adapter)

    await ui.open(_source(home), "access:42")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert 'data="badge:restricted"' in html
    assert "restricted:" not in html


@pytest.mark.asyncio
async def test_create_view_shows_manager_mode_badge(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._bot_can_manage_bots = True
    adapter._bot_username = "HermesManagerBot"
    ui = TelegramBotOperationsUI(adapter)

    await ui.open(_source(home), "create", create_username="peer42_bot")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "Manager mode" in html
    assert 'data="badge:enabled"' in html


@pytest.mark.asyncio
async def test_create_view_without_manager_mode_shows_off_badge(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)

    await ui.open(_source(home), "create", create_username="peer42_bot")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "Manager mode" in html
    assert 'data="badge:disabled"' in html
    assert "manager-mode" in html.lower()


@pytest.mark.asyncio
async def test_managed_confirmation_binding_is_collapsible_and_footerless(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    BotOperationsStore(home).record_managed_bot({"id": 42, "username": "peer42_bot"})
    adapter._client.settings_users = [SimpleNamespace(id=88, username=None, first_name="Human", last_name="")]
    ui = TelegramBotOperationsUI(adapter)

    await ui.handle_command(None, _source(home), "managedbots", "unrestrict 42")
    html = _last_request(adapter._client, "SendMessageRequest").rich_message.html
    assert "<h1>Owner confirmation required</h1>" in html
    assert "<details><summary>Details</summary>" in html
    assert "Page 1 of 1" not in html
# --- B2B bridge piece 1: schema 2, peer settings, test-on-add, ban list, talks ---

_B2B_DEFAULT_SETTINGS = {
    "intake": "turns",
    "trigger": "mention",
    "model": None,
    "acceptance": "full",
    "acceptance_text": "accepted ok",
    "secure": "allowed",
    "notify": "on",
}
_B2B_TEST_TEXT = "B2B test message from Hermes."


def _write_store_doc(store, doc):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(doc), encoding="utf-8")


def _schema1_doc():
    return {"schema": 1,
            "peers": {"42": {"id": 42, "username": "peer42_bot", "name": "Peer Bot",
                             "group_chat_ids": [], "dm_enabled": True, "added_at": 123}},
            "managed_bots": {}, "inbox": [], "sends": []}


def _confirm_payload(pending):
    return {key: value for key, value in pending.items()
            if key not in {"ok", "confirmation_required", "message", "operation"}}


@pytest.mark.asyncio
async def test_schema1_document_migrates_to_schema2_with_peer_defaults(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    store = BotOperationsStore(home)
    _write_store_doc(store, _schema1_doc())

    peer = store.peer(42)
    assert peer["settings"] == _B2B_DEFAULT_SETTINGS
    assert peer["last_test_at"] is None and peer["last_test_ok"] is None
    assert peer["last_in_at"] is None and peer["last_in_text"] is None
    assert peer["last_out_at"] is None and peer["last_out_text"] is None
    assert store.list_talks() == []
    assert not store.is_banned(42)

    store.add_peer({"id": 77, "username": "peer77_bot"}, group_enabled=False, dm_enabled=True)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["schema"] == 2
    assert raw["banned"] == [] and raw["talks"] == {}
    assert raw["peers"]["42"]["settings"] == _B2B_DEFAULT_SETTINGS


def test_unknown_schema_is_still_refused(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    store = BotOperationsStore(home)
    doc = _schema1_doc()
    doc["schema"] = 3
    _write_store_doc(store, doc)
    with pytest.raises(BotOperationsError):
        store.peers()


@pytest.mark.asyncio
async def test_peer_set_settings_round_trip_and_strict_validation(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    assert store.peer(42)["settings"] == _B2B_DEFAULT_SETTINGS
    assert "peer.set_settings" in BOT_OPERATION_PARAMETERS

    pending = await ops.request(source, "peer.set_settings", bot_id=42,
                                settings={"intake": "inbox", "notify": "off", "model": "some-model"})
    assert pending["confirmation_required"]
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True
    settings = result["peer"]["settings"]
    assert settings["intake"] == "inbox" and settings["notify"] == "off"
    assert settings["model"] == "some-model" and settings["trigger"] == "mention"

    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings={"intake": "bogus"}))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings={"unknown_key": "inbox"}))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings={"model": ""}))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings={"acceptance_text": "x" * 300}))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings="inbox"))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=42,
                                 settings={}))["ok"]
    assert not (await ops.request(source, "peer.set_settings", bot_id=77,
                                 settings={"intake": "inbox"}))["ok"]


@pytest.mark.asyncio
async def test_peer_add_dm_sends_test_message_and_records_pass(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    pending = await ops.request(source, "peer.add", bot_id="42",
                                group_enabled=False, dm_enabled=True)
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True
    assert result["test"]["ok"] is True and result["test"]["error"] is None
    assert adapter._client.sent[0][1] == _B2B_TEST_TEXT
    assert adapter._client.sent[0][0].id == 42
    peer = BotOperationsStore(home).peer(42)
    assert peer["last_test_ok"] is True and peer["last_test_at"] is not None
    assert peer["settings"] == _B2B_DEFAULT_SETTINGS


@pytest.mark.asyncio
async def test_peer_add_group_only_test_prepends_mention(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ops = TelegramBotOperations(adapter)
    source = _source(home, chat_id="-200", chat_type="group")

    pending = await ops.request(source, "peer.add", bot_id="42",
                                group_enabled=True, dm_enabled=False)
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True
    assert result["test"]["ok"] is True
    assert adapter._client.sent[0][1] == f"@peer42_bot {_B2B_TEST_TEXT}"
    assert adapter._client.sent[0][0].id == -200


@pytest.mark.asyncio
async def test_peer_add_test_failure_still_adds_peer_with_reason(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._client.send_result = False
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    pending = await ops.request(source, "peer.add", bot_id="42",
                                group_enabled=False, dm_enabled=True)
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True  # the add succeeds; only the reachability probe failed
    assert result["test"]["ok"] is False
    assert result["test"]["error"]
    peer = BotOperationsStore(home).peer(42)
    assert peer is not None and peer["last_test_ok"] is False
    assert peer["last_test_at"] is not None


@pytest.mark.asyncio
async def test_ban_blocks_readd_until_unbanned(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    pending = await ops.request(source, "peer.remove", bot_id=42, ban=True)
    assert pending["confirmation_required"] and pending["ban"] is True
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True and result["banned"] is True
    assert store.peer(42) is None and store.is_banned(42)

    refused = await ops.request(source, "peer.add", bot_id="42",
                                group_enabled=False, dm_enabled=True)
    assert not refused["ok"] and "banned" in refused["error"].lower()

    assert not (await ops.request(source, "peer.unban", bot_id=77))["ok"]
    pending = await ops.request(source, "peer.unban", bot_id=42)
    assert pending["confirmation_required"]
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True and not store.is_banned(42)

    pending = await ops.request(source, "peer.add", bot_id="42",
                                group_enabled=False, dm_enabled=True)
    assert pending["confirmation_required"]


@pytest.mark.asyncio
async def test_plain_peer_remove_does_not_ban(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    pending = await ops.request(source, "peer.remove", bot_id=42)
    assert pending["ban"] is False
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True and result["banned"] is False
    assert not store.is_banned(42)
    assert (await ops.request(source, "peer.add", bot_id="42",
                              group_enabled=False, dm_enabled=True))["confirmation_required"]


@pytest.mark.asyncio
async def test_last_incoming_and_outgoing_are_recorded_on_the_peer(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)
    ops = TelegramBotOperations(adapter)
    source = _source(home)

    event = SimpleNamespace(message=SimpleNamespace(id=50, message="hello back"), raw_text="")
    assert await ops.handle_incoming(event, _source(home, user_id="42", chat_id="100", is_bot=True))
    peer = store.peer(42)
    assert peer["last_in_text"] == "hello back" and peer["last_in_at"] is not None

    pending = await ops.request(source, "peer.send", bot_id="42", text="exact payload")
    result = await ops.confirm(source, pending["operation"], **_confirm_payload(pending))
    assert result["ok"] is True
    peer = store.peer(42)
    assert peer["last_out_text"] == "exact payload" and peer["last_out_at"] is not None


def test_talk_helpers_and_killed_talk_is_never_resurrected(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    store = BotOperationsStore(home)
    record = {"peer_id": 42, "state": "live", "started_at": 1000, "last_at": 1001,
              "last_message": "hi", "model": None, "killed": False}

    assert store.get_talk(42) is None and store.list_talks() == []
    assert store.mark_talk_killed(42) is None
    store.upsert_talk(42, record)
    assert store.get_talk(42)["state"] == "live"
    assert [talk["peer_id"] for talk in store.list_talks()] == [42]

    with pytest.raises(BotOperationsError):
        store.upsert_talk(42, {"peer_id": 42, "state": "bogus", "started_at": 1000,
                               "last_at": 1001, "last_message": "", "model": None,
                               "killed": False})

    killed = store.mark_talk_killed(42)
    assert killed["state"] == "killed" and killed["killed"] is True
    assert store.get_talk(42)["last_message"] == "hi"  # the record is kept, not dropped
    with pytest.raises(BotOperationsError):
        store.upsert_talk(42, record)  # a killed talk is never resurrected
    assert store.get_talk(42)["state"] == "killed"

# --- B2B bridge piece 3: /b2b UI (peer list, settings, Manage, badges, guard-free sends) ---

import re
from datetime import datetime


def _add_b2b_peer(store, bot_id=42, *, username="peer42_bot", name="Peer Bot",
                  group=False, dm=True, group_chat_id="-200"):
    return store.add_peer({"id": bot_id, "username": username, "name": name},
                          group_enabled=group, dm_enabled=dm,
                          group_chat_id=group_chat_id if group else None)


def _inbody_token(html, label):
    """Single-use tgb: token of the in-body callback button carrying *label*."""
    for data, text in re.findall(r'data="(tgb:[^"]+)"[^>]*>([^<]+)</tg-button>', html):
        if label.lower() in text.lower():
            return data
    raise AssertionError(f"expected in-body control {label!r} in card")


def _html_of(client, name="SendMessageRequest"):
    return _last_request(client, name).rich_message.html


@pytest.mark.asyncio
async def test_b2b_hub_lists_peers_with_pill_scopes_last_response_and_botfather_note(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    _add_b2b_peer(store, 42)
    _add_b2b_peer(store, 77, username="peer77_bot", name="Peer Seventy Seven",
                  group=True, dm=False)
    store.note_peer_incoming(42, "hello back")
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "b2b")
    html = _html_of(adapter._client)
    assert "<h1>Bot-to-bot</h1>" in html
    # Name is the profile pill itself, as on /start — never a separate Profile label.
    assert 'url="tg://user?id=42"' in html and "Peer Bot" in html
    assert 'url="tg://user?id=77"' in html
    assert ">DM</tg-button>" in html and ">group</tg-button>" in html
    assert "hello back" in html
    assert datetime.now().strftime("%d %b %Y") in html
    assert "BotFather" in html and "bot-to-bot mode" in html
    assert "<details><summary>Settings</summary>" in html
    for label in ("Intake", "Trigger", "Model", "Acceptance", "Secure", "Notify"):
        assert label in html
    assert "Switch to inbox" in html
    # Single page carries no indexation footer (same rule as the managed roster).
    assert "Page 1 of 1" not in html


@pytest.mark.asyncio
async def test_settings_control_toggles_intake_through_confirmation_and_rerenders(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    _add_b2b_peer(store, 42)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "manage_peer:42")
    html = _html_of(adapter._client)
    token = _inbody_token(html, "Switch to inbox")
    assert await ui.handle_callback(CallbackEvent(), source, token)
    edited = _html_of(adapter._client, "EditMessageRequest")
    assert "Owner confirmation required" in edited
    assert "Set intake to inbox for peer 42?" in edited

    confirm = _callback(_last_request(adapter._client, "EditMessageRequest").reply_markup,
                        suffix="confirm")
    assert await ui.handle_callback(CallbackEvent(), source, confirm)
    assert store.peer(42)["settings"]["intake"] == "inbox"
    rendered = _html_of(adapter._client, "EditMessageRequest")
    assert "Change applied." in rendered
    assert "Switch to turns" in rendered


@pytest.mark.asyncio
async def test_manage_view_shows_peer_whole_with_drop_ban_and_live_talk(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    _add_b2b_peer(store, 42, group=True, dm=True)
    store.note_peer_incoming(42, "hello back")
    store.note_peer_outgoing(42, "exact payload")
    store.upsert_talk(42, {"peer_id": 42, "state": "live", "started_at": 1000,
                           "last_at": 1001, "last_message": "hi", "model": None,
                           "killed": False})
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.open(source, "manage_peer:42")
    request = _last_request(adapter._client, "SendMessageRequest")
    html = request.rich_message.html
    assert 'url="tg://user?id=42"' in html
    assert 'text="@peer42_bot"' in html and 'text="42"' in html
    assert "Last response" in html and "hello back" in html
    assert "Last sent" in html and "exact payload" in html
    assert "Talk: live" in html and "hi" in html
    assert "<details><summary>Settings</summary>" in html
    assert _button_labels(request.reply_markup) == ["Drop", "Ban", "Peers", "Bot-to-bot"]


@pytest.mark.asyncio
async def test_peer_add_result_card_shows_passed_test_badge(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.handle_command(None, source, "/b2b", "add 42 dm")
    token = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup,
                      suffix="confirm")
    assert await ui.handle_callback(CallbackEvent(), source, token)
    html = _html_of(adapter._client, "EditMessageRequest")
    assert BotOperationsStore(home).peer(42) is not None
    assert ">passed</tg-button>" in html
    assert "Test message passed." in html


@pytest.mark.asyncio
async def test_peer_add_result_card_shows_failed_test_badge_with_reason(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._client.send_result = False
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)

    await ui.handle_command(None, source, "/b2b", "add 42 dm")
    token = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup,
                      suffix="confirm")
    assert await ui.handle_callback(CallbackEvent(), source, token)
    html = _html_of(adapter._client, "EditMessageRequest")
    assert BotOperationsStore(home).peer(42) is not None  # the add succeeds either way
    assert ">failed</tg-button>" in html
    assert "did not confirm" in html


@pytest.mark.asyncio
async def test_peer_send_executes_immediately_for_admitted_nonowner_without_confirmation(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    _add_b2b_peer(store, 42)
    ui = TelegramBotOperationsUI(adapter)
    ops = TelegramBotOperations(adapter)
    nonowner = _source(home, user_id="200")

    # The agent-tool path sends at once: no confirmation card is ever staged.
    result = await ui.request(nonowner, "peer.send", {"bot_id": "42", "text": "hi from member"})
    assert result["ok"] is True and result["message_id"] == 701
    assert adapter._client.sent[0][1] == "hi from member"
    assert not any(type(item).__name__ == "SendMessageRequest"
                   for item in adapter._client.requests)

    # The command path does the same and returns a result card with the sent id.
    await ui.handle_command(None, nonowner, "/b2b", "send 42 dm hello again")
    assert adapter._client.sent[-1][1] == "hello again"
    assert "701" in _html_of(adapter._client)

    # Configuration stays owner-only: the same admitted non-owner cannot add.
    refused = await ops.request(nonowner, "peer.add", bot_id="77",
                                group_enabled=False, dm_enabled=True)
    assert not refused["ok"] and "Owner authorization" in refused["error"]


@pytest.mark.asyncio
async def test_banned_view_lists_banned_ids_with_unban_button(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    store = BotOperationsStore(home)
    _add_b2b_peer(store, 42)
    ops = TelegramBotOperations(adapter)
    source = _source(home)
    pending = await ops.request(source, "peer.remove", bot_id=42, ban=True)
    assert (await ops.confirm(source, pending["operation"], **_confirm_payload(pending)))["ok"]
    assert store.is_banned(42)
    ui = TelegramBotOperationsUI(adapter)

    await ui.open(source, "banned")
    request = _last_request(adapter._client, "SendMessageRequest")
    assert "42" in request.rich_message.html
    assert "Unban" in _button_labels(request.reply_markup)

    token = _callback(request.reply_markup, suffix="unban")
    assert await ui.handle_callback(CallbackEvent(), source, token)
    assert "Unban bot 42" in _html_of(adapter._client, "EditMessageRequest")
    confirm = _callback(_last_request(adapter._client, "EditMessageRequest").reply_markup,
                        suffix="confirm")
    assert await ui.handle_callback(CallbackEvent(), source, confirm)
    assert not store.is_banned(42)
    assert "No bots are banned." in _html_of(adapter._client, "EditMessageRequest")
