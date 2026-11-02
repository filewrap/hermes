"""Offline regression coverage for managed Telegram bot operations and confirmation UI."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.bot_operations import (
    BOT_OPERATION_PARAMETERS,
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
    assert len(adapter._client.entities) == 2  # lookup before approval and identity recheck at confirmation
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
    await ui.handle_callback(CallbackEvent(), wrong, token)
    assert BotOperationsStore(home).peer(42) is None
    assert len(adapter._client.entities) == before


@pytest.mark.asyncio
async def test_confirmation_expiry_and_revoked_peer_fail_without_outbound_send(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    ui = TelegramBotOperationsUI(adapter)
    source = _source(home)
    store = BotOperationsStore(home)
    store.add_peer({"id": 42, "username": "peer42_bot"}, group_enabled=False, dm_enabled=True)

    await ui.request(source, "peer.send", {"bot_id": "42", "text": "hello"})
    card = next(iter(ui._cards.values()))
    confirm = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup, suffix="confirm")
    card["confirm_expires"] = 0
    await ui.handle_callback(CallbackEvent(), source, confirm)
    assert adapter._client.sent == []

    await ui.request(source, "peer.send", {"bot_id": "42", "text": "hello again"})
    state = list(ui._cards.values())[-1]
    callback = _callback(_last_request(adapter._client, "SendMessageRequest").reply_markup, suffix="confirm")
    store.remove_peer(42)
    await ui.handle_callback(CallbackEvent(), source, callback)
    assert adapter._client.sent == []
    assert "error" in state["notice"].lower()


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
    assert "href=\"https://t.me/newbot/HermesManagerBot/wonder_bot?name=My+Bot\"" in request.rich_message.html
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
async def test_forum_peer_send_previews_final_mention_and_restores_exact_card_topic(tmp_path):
    home = _profile(tmp_path, "profile", "100")
    adapter = _adapter(tmp_path)
    adapter._peer_cache["-200"] = types.InputPeerChat(chat_id=200)
    BotOperationsStore(home).add_peer({"id": 42, "username": "peer42_bot"},
        group_enabled=True, dm_enabled=False, group_chat_id="-200")
    ui = TelegramBotOperationsUI(adapter)
    src = _source(home, chat_id="-200", chat_type="forum", thread_id="77")
    result = await ui.request(src, "peer.send", {"bot_id": 42, "text": "literal **text**"})
    assert result["text"] == "@peer42_bot literal **text**"
    assert result["thread_id"] == "77"
    rendered = _last_request(adapter._client, "SendMessageRequest")
    assert result["text"] in rendered.rich_message.html
    token = _callback(rendered.reply_markup, suffix="confirm")
    # Callback MTProto envelope lacks message/topic; its exact bound card restores it.
    await ui.handle_callback(CallbackEvent(), _source(home, chat_id="-200", chat_type="forum"), token)
    assert adapter._client.sent == [(await adapter._client.get_entity(-200), result["text"], 77)]


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
