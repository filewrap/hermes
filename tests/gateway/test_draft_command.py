"""Behaviour contracts for the gateway /draft showcase command.

/draft sends the showcase as a sequence of short BEATS, in order: every
beat first shows a draft preview (``preview_next`` — "Next: <label>…",
ephemeral, never a sent message), then optionally runs one live demo
step, then sends ONE short message (a title plus at most three lines) —
and, when the beat ran a live animation, ONE persist message carrying
that animation's content first (label on its own first line, then the
content), so every animation stays visible after its draft expires.
The 22 ``ResponseStreamDraft`` members are split across eight short
messages; topics 3, 4, 5 and 7 are split into short beats the same way.
Topic 8 demos every other ``SendMessage*Action`` the installed
Telethon wheel knows (typing, uploads, records, geo/contact/sticker/
game, history import, cancel, stop-draft), one beat per action, each
with its own persist message naming the action. Exactly ONE copy pill
exists in the whole output, on the Usage line of the final beat. The
live steps run through ``gateway.draft_demo`` (stubbed here in
``sys.modules``); a preview, demo step or persist send returning False
aborts the rest of the sequence. Gating is owner-only + DM-only +
Telegram-only, with silent denial: nothing is sent to anyone else.
"""

import importlib.abc
import re
import sys
import types
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.telegram.streaming_response import ResponseStreamDraft

OWNER_ID = "8495104954"
OTHER_ID = "12345"

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2B00-\u2BFF\u26A0\u2713\u2714\u274C\uFE0F]"
)

# Preview labels, in beat order. The beat plan (handler) is: topic 1 in
# eight messages, topic 2 in four beats, topic 3 in three, topic 4 in
# four, topic 5 in two, topic 6 in one, topic 8 in sixteen, topic 7 in
# three — 41 beats. Topic 8 runs before the closing topic 7 so the
# Usage-bearing final beat stays last.
PREVIEW_LABELS = [
    "the draft sink",
    "feeding the draft",
    "deltas and partial text",
    "stopping and finishing",
    "inside: frames",
    "inside: the pump",
    "inside: sending",
    "inside: failures",
    "the thinking placeholder",
    "the reasoning draft",
    "the content draft",
    "the final message",
    "rich drafts",
    "throttle and keepalive",
    "sync draft and fail-open",
    "the placeholder action",
    "the reasoning action",
    "the content action",
    "the sync action",
    "how drafts travel",
    "adoption and caps",
    "temporary reasoning",
    "typing",
    "uploading a photo",
    "uploading a video",
    "uploading a voice message",
    "uploading a file",
    "uploading a round video",
    "recording a video",
    "recording a voice message",
    "recording a round video",
    "choosing a location",
    "choosing a contact",
    "choosing a sticker",
    "playing a game",
    "importing chat history",
    "cancelling the indicator",
    "stopping the draft",
    "Stop",
    "shown once",
    "fail-open",
]

# Topic 8 beats (indices 22-36): (preview label, TL action name, persist
# message) — the 15 raw typing actions, each demoed by ``show_action``
# and persisted by naming the action. Beat 37 is the stop-draft beat.
_TOPIC8_ACTIONS = [
    ("typing", "SendMessageTypingAction",
     "Typing\nSendMessageTypingAction — user is typing."),
    ("uploading a photo", "SendMessageUploadPhotoAction",
     "Uploading a photo\nSendMessageUploadPhotoAction — user is uploading a photo."),
    ("uploading a video", "SendMessageUploadVideoAction",
     "Uploading a video\nSendMessageUploadVideoAction — user is uploading a video."),
    ("uploading a voice message", "SendMessageUploadAudioAction",
     "Uploading a voice message\nSendMessageUploadAudioAction — user is uploading a voice message."),
    ("uploading a file", "SendMessageUploadDocumentAction",
     "Uploading a file\nSendMessageUploadDocumentAction — user is uploading a file."),
    ("uploading a round video", "SendMessageUploadRoundAction",
     "Uploading a round video\nSendMessageUploadRoundAction — user is uploading a round video."),
    ("recording a video", "SendMessageRecordVideoAction",
     "Recording a video\nSendMessageRecordVideoAction — user is recording a video."),
    ("recording a voice message", "SendMessageRecordAudioAction",
     "Recording a voice message\nSendMessageRecordAudioAction — user is recording a voice message."),
    ("recording a round video", "SendMessageRecordRoundAction",
     "Recording a round video\nSendMessageRecordRoundAction — user is recording a round video to share."),
    ("choosing a location", "SendMessageGeoLocationAction",
     "Choosing a location\nSendMessageGeoLocationAction — user is selecting a location to share."),
    ("choosing a contact", "SendMessageChooseContactAction",
     "Choosing a contact\nSendMessageChooseContactAction — user is selecting a contact to share."),
    ("choosing a sticker", "SendMessageChooseStickerAction",
     "Choosing a sticker\nSendMessageChooseStickerAction — user is choosing a sticker."),
    ("playing a game", "SendMessageGamePlayAction",
     "Playing a game\nSendMessageGamePlayAction — user is playing a game."),
    ("importing chat history", "SendMessageHistoryImportAction",
     "Importing chat history\nSendMessageHistoryImportAction — chat history is being imported."),
    ("cancelling the indicator", "SendMessageCancelAction",
     "Cancelling the indicator\nSendMessageCancelAction — invalidates all previous action updates; the indicator clears."),
]

# Beat index (0-based into PREVIEW_LABELS) -> demo step recorded for it.
_DEMO_BEATS = {
    8: "placeholder",
    9: "reasoning",
    10: "content",
    11: "sync_final",
    21: "reasoning",
    **{22 + i: "action" for i in range(len(_TOPIC8_ACTIONS))},
    37: "stop_draft",
}

# Every beat sends one short card except beat 11 (index 11): its final
# message is sent by the demo step itself, not by the handler.
_CARD_BEATS = [i for i in range(len(PREVIEW_LABELS)) if i != 11]


def _draft_member_names():
    """Member names of ResponseStreamDraft, dunder noise excluded (``__init__`` kept)."""
    return [
        name for name in vars(ResponseStreamDraft)
        if name == "__init__" or not (name.startswith("__") and name.endswith("__"))
    ]


def _make_event(text="/draft", platform=Platform.TELEGRAM, user_id=OWNER_ID,
                chat_id="67890", chat_type="dm"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        chat_type=chat_type,
    )
    return MessageEvent(text=text, source=source)


class _CardAdapter:
    """Adapter with the Telegram card method; records every card and send."""

    def __init__(self, events):
        self.events = events
        self.cards = []
        self.sends = []

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == OWNER_ID

    async def send_command_card(self, *, source, html, links=(), copies=(),
                                buttons=(), reply_to=None):
        self.cards.append(html)
        self.events.append(("card", len(self.cards) - 1))
        return True

    async def send(self, chat_id, text, **kwargs):
        self.sends.append(text)
        self.events.append(("send", text))
        return MagicMock()


class _NoCardAdapter(_CardAdapter):
    """Adapter with no card method (plain-text fallback path)."""

    send_command_card = None


class _NoOwnerCheckAdapter(_CardAdapter):
    """Adapter with a card method but no owner checker (fail closed)."""

    _is_owner_user = None


def _make_runner(adapter, home=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    if home is not None:
        runner._authorization_home_for_source = lambda source: home
    return runner


def _owner_home(tmp_path):
    """A multiuser home: OWNER_TELEGRAM_ID set, so the adapter check decides."""
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    return home


def _install_fake_demo(monkeypatch, events, *, preview_fail_at=None,
                       reasoning_fail_at=None, placeholder=True,
                       content=True, sync_final=True, persist_fail_at=None,
                       action_fail_at=None, stop_draft_ok=True):
    """Stub ``gateway.draft_demo`` in sys.modules with a recording fake.

    The fake implements the step API the handler uses: ``preview_next``
    plus one function per live demo step, the persist-message sender,
    and the ``DEMO_*`` sample texts. ``preview_fail_at`` /
    ``reasoning_fail_at`` / ``persist_fail_at`` / ``action_fail_at``
    are 1-based call counts at which that step returns False (abort
    drill); the other flags force that step to fail on every call.
    ``send_animation_message`` records the persist event and delivers
    through the adapter's plain ``send`` (the delivery record), without
    double-logging it as a send event.
    """
    module = types.ModuleType("gateway.draft_demo")
    module.DEMO_REASONING_TOPIC2 = "demo reasoning topic 2"
    module.DEMO_CONTENT_TOPIC2 = "demo content topic 2"
    module.DEMO_FINAL_TOPIC2 = "demo final topic 2"
    module.DEMO_REASONING_TOPIC6 = "demo reasoning topic 6"
    counts = {"preview": 0, "reasoning": 0, "persist": 0, "action": 0}

    async def preview_next(adapter, source, text, *, loop=None, sleep=None,
                           hold_delay=1.5):
        counts["preview"] += 1
        events.append(("preview", text))
        if preview_fail_at is not None and counts["preview"] == preview_fail_at:
            return False
        return True

    async def show_placeholder(adapter, source, *, loop=None, sleep=None,
                               hold_delay=2.5):
        events.append(("placeholder",))
        return placeholder

    async def show_reasoning(adapter, source, text, *, loop=None, sleep=None,
                             hold_delay=2.5):
        counts["reasoning"] += 1
        events.append(("reasoning", text))
        if reasoning_fail_at is not None and counts["reasoning"] == reasoning_fail_at:
            return False
        return True

    async def show_content(adapter, source, text, *, loop=None, sleep=None,
                           hold_delay=2.5):
        events.append(("content", text))
        return content

    async def show_sync_and_final(adapter, source, final_text, *, loop=None,
                                  sleep=None):
        events.append(("sync_final", final_text))
        if not sync_final:
            return False
        await adapter.send(source.chat_id, final_text)
        return True

    async def show_action(adapter, source, action_name, *, loop=None,
                          sleep=None, hold_delay=10.0, resend_interval=4.0):
        counts["action"] += 1
        events.append(("action", action_name))
        if action_fail_at is not None and counts["action"] == action_fail_at:
            return False
        return True

    async def show_stop_draft(adapter, source, *, loop=None, sleep=None,
                              hold_delay=10.0, resend_interval=4.0):
        events.append(("stop_draft",))
        return stop_draft_ok

    async def send_animation_message(adapter, source, text):
        counts["persist"] += 1
        events.append(("persist", text))
        if persist_fail_at is not None and counts["persist"] == persist_fail_at:
            return False
        adapter.sends.append(text)
        return True

    module.preview_next = preview_next
    module.show_placeholder = show_placeholder
    module.show_reasoning = show_reasoning
    module.show_content = show_content
    module.show_sync_and_final = show_sync_and_final
    module.show_action = show_action
    module.show_stop_draft = show_stop_draft
    module.send_animation_message = send_animation_message
    monkeypatch.setitem(sys.modules, "gateway.draft_demo", module)
    return module


class _BlockDemoImport(importlib.abc.MetaPathFinder):
    """Simulate an absent ``gateway.draft_demo`` module (import fails)."""

    def find_spec(self, fullname, path, target=None):
        if fullname == "gateway.draft_demo":
            raise ImportError("No module named 'gateway.draft_demo'")
        # Anything else: not found here (implicit None), try the next finder.


def _assert_no_emoji(text):
    assert not _EMOJI_RE.search(text), f"emoji found in: {text!r}"


def _expected_events(demo):
    """The full event order for a successful /draft run."""
    events = []
    card_index = 0
    for beat_index, label in enumerate(PREVIEW_LABELS):
        events.append(("preview", f"Next: {label}…"))
        step = _DEMO_BEATS.get(beat_index)
        if step == "reasoning":
            text = (demo.DEMO_REASONING_TOPIC2 if beat_index == 9
                    else demo.DEMO_REASONING_TOPIC6)
            events.append(("reasoning", text))
            persist = (f"Reasoning\n{text}" if beat_index == 9
                       else f"Temporary reasoning\n{text}")
            events.append(("persist", persist))
        elif step == "placeholder":
            events.append(("placeholder",))
            events.append(("persist", "Placeholder\nThinking"))
        elif step == "content":
            events.append(("content", demo.DEMO_CONTENT_TOPIC2))
            events.append(("persist", f"Content\n{demo.DEMO_CONTENT_TOPIC2}"))
        elif step == "sync_final":
            events.append(("sync_final", demo.DEMO_FINAL_TOPIC2))
            events.append(("send", demo.DEMO_FINAL_TOPIC2))
        elif step == "action":
            _, action_name, persist = _TOPIC8_ACTIONS[beat_index - 22]
            events.append(("action", action_name))
            events.append(("persist", persist))
        elif step == "stop_draft":
            events.append(("stop_draft",))
            events.append(("persist", f"Stop draft\n{demo.DEMO_REASONING_TOPIC2}"))
        if beat_index in _CARD_BEATS:
            events.append(("card", card_index))
            card_index += 1
    return events


def _expected_sends(demo):
    """Every plain message /draft sends, in order: 4 draft persists, the
    sync final, the temporary-reasoning persist, then the 16 topic-8
    persists — 21 in total."""
    return [
        "Placeholder\nThinking",
        f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}",
        f"Content\n{demo.DEMO_CONTENT_TOPIC2}",
        demo.DEMO_FINAL_TOPIC2,
        f"Temporary reasoning\n{demo.DEMO_REASONING_TOPIC6}",
        *(persist for _, _, persist in _TOPIC8_ACTIONS),
        f"Stop draft\n{demo.DEMO_REASONING_TOPIC2}",
    ]


class TestDraftRegistration:
    def test_draft_is_registered_gateway_only(self):
        from hermes_cli.commands import COMMAND_REGISTRY, resolve_command

        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert "draft" in names
        definition = resolve_command("draft")
        assert definition is not None
        assert definition.gateway_only is True
        assert definition.category == "Configuration"

    def test_draft_is_a_tenant_and_idle_command(self):
        from gateway.run_busy import GatewayBusySessionMixin
        from gateway.tenant_commands import _TENANT_COMMANDS

        assert "draft" in _TENANT_COMMANDS
        assert "draft" in GatewayBusySessionMixin._IDLE_COMMANDS

    def test_draft_is_gateway_known_but_not_forced_into_the_menu(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        from hermes_cli.commands_platforms import _TELEGRAM_MENU_PRIORITY

        assert "draft" in GATEWAY_KNOWN_COMMANDS
        assert "draft" not in _TELEGRAM_MENU_PRIORITY


class TestDraftSequence:
    @pytest.mark.asyncio
    async def test_handler_sends_short_beats_in_order_with_preview_first(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None  # all cards sent — runner sends nothing else
        assert events == _expected_events(demo)
        assert len(adapter.cards) == 40  # 41 beats; the sync beat is the demo's own send
        assert adapter.sends == _expected_sends(demo)

    @pytest.mark.asyncio
    async def test_every_preview_carries_a_next_label(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        previews = [e[1] for e in events if len(e) == 2 and e[0] == "preview"]
        assert len(previews) == len(PREVIEW_LABELS)
        for text, label in zip(previews, PREVIEW_LABELS):
            assert text == f"Next: {label}…"

    @pytest.mark.asyncio
    async def test_live_demo_steps_run_with_the_demo_texts(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        demo_events = [e for e in events if e[0] in
                       ("placeholder", "reasoning", "content", "sync_final",
                        "action", "stop_draft")]
        assert demo_events == [
            ("placeholder",),
            ("reasoning", demo.DEMO_REASONING_TOPIC2),
            ("content", demo.DEMO_CONTENT_TOPIC2),
            ("sync_final", demo.DEMO_FINAL_TOPIC2),
            ("reasoning", demo.DEMO_REASONING_TOPIC6),
            *(("action", name) for _, name, _ in _TOPIC8_ACTIONS),
            ("stop_draft",),
        ]

    @pytest.mark.asyncio
    async def test_topic1_lists_every_response_stream_draft_member_exactly_once(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        members = _draft_member_names()
        assert len(members) == 23
        topic1 = "\n".join(adapter.cards[:8])  # the eight topic-1 beats
        for name in members:
            assert topic1.count(f"<code>{name}</code>") == 1, (
                f"member {name} must appear exactly once across the topic-1 beats")

    @pytest.mark.asyncio
    async def test_preview_false_aborts_before_any_message(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events, preview_fail_at=1)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        assert events == [("preview", "Next: the draft sink…")]
        assert adapter.cards == []

    @pytest.mark.asyncio
    async def test_placeholder_step_false_aborts_the_sequence(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events, placeholder=False)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        assert events == _expected_events(demo)[:2 * 8 + 2]  # 8 beats, then preview + failed step
        assert len(adapter.cards) == 8  # topic 1 only; nothing after

    @pytest.mark.asyncio
    async def test_sync_final_step_false_aborts_the_sequence(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events, sync_final=False)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        assert len(adapter.cards) == 11  # topic 1 (8) + topic 2 beats 1-3
        # The three topic-2 persists went out; the demo's final never did.
        assert adapter.sends == [
            "Placeholder\nThinking",
            f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}",
            f"Content\n{demo.DEMO_CONTENT_TOPIC2}",
        ]
        assert events[-1] == ("sync_final", "demo final topic 2")

    @pytest.mark.asyncio
    async def test_topic6_reasoning_false_aborts_the_sequence(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events, reasoning_fail_at=2)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        # Topic 1 (8) + topic 2 (3 cards) + topic 3 (3) + topic 4 (4) +
        # topic 5 (2); no topic-6 card, no topic-7 beats.
        assert len(adapter.cards) == 20
        # The topic-2 persists and the sync final went out before topic 6.
        assert adapter.sends == [
            "Placeholder\nThinking",
            f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}",
            f"Content\n{demo.DEMO_CONTENT_TOPIC2}",
            demo.DEMO_FINAL_TOPIC2,
        ]
        assert events[-1] == ("reasoning", "demo reasoning topic 6")

    @pytest.mark.asyncio
    async def test_every_animation_gets_exactly_one_persist_message(
            self, tmp_path, monkeypatch):
        """Every live animation gets its own persist message, in order:
        the 4 draft persists (placeholder, reasoning, content, temporary
        reasoning) and the 16 topic-8 persists — 20 in total, plus the
        sync final makes 21 sends."""
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        persists = [e[1] for e in events if e[0] == "persist"]
        assert persists == [
            "Placeholder\nThinking",
            f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}",
            f"Content\n{demo.DEMO_CONTENT_TOPIC2}",
            f"Temporary reasoning\n{demo.DEMO_REASONING_TOPIC6}",
            *(persist for _, _, persist in _TOPIC8_ACTIONS),
            f"Stop draft\n{demo.DEMO_REASONING_TOPIC2}",
        ]
        assert adapter.sends == _expected_sends(demo)
        # The sync step still sends exactly one final message — the
        # persist messages never duplicate it.
        assert adapter.sends.count(demo.DEMO_FINAL_TOPIC2) == 1

    @pytest.mark.asyncio
    async def test_topic6_now_sends_exactly_one_message(
            self, tmp_path, monkeypatch):
        """Topic 6 used to send no message; it now sends exactly one —
        the temporary-reasoning persist — after its draft."""
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        persist = f"Temporary reasoning\n{demo.DEMO_REASONING_TOPIC6}"
        assert adapter.sends.count(persist) == 1
        index = events.index(("persist", persist))
        assert events[index - 1] == ("reasoning", demo.DEMO_REASONING_TOPIC6)

    @pytest.mark.asyncio
    async def test_failed_action_step_sends_no_persist_message(
            self, tmp_path, monkeypatch):
        """A failed topic-8 demo sends no persist message for that
        animation, and aborts the rest of the sequence."""
        events = []
        adapter = _CardAdapter(events)
        demo = _install_fake_demo(monkeypatch, events, action_fail_at=1)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        # Topic 1 (8) + topic 2 (3) + topic 3 (3) + topic 4 (4) +
        # topic 5 (2) + topic 6 (1); no topic-8 card, no topic-7 beats.
        assert len(adapter.cards) == 21
        assert events[-1] == ("action", "SendMessageTypingAction")
        assert [e for e in events if e[0] == "persist"] == [
            ("persist", "Placeholder\nThinking"),
            ("persist", f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}"),
            ("persist", f"Content\n{demo.DEMO_CONTENT_TOPIC2}"),
            ("persist", f"Temporary reasoning\n{demo.DEMO_REASONING_TOPIC6}"),
        ]
        assert adapter.sends == [
            "Placeholder\nThinking",
            f"Reasoning\n{demo.DEMO_REASONING_TOPIC2}",
            f"Content\n{demo.DEMO_CONTENT_TOPIC2}",
            demo.DEMO_FINAL_TOPIC2,
            f"Temporary reasoning\n{demo.DEMO_REASONING_TOPIC6}",
        ]

    @pytest.mark.asyncio
    async def test_persist_send_false_aborts_the_sequence(
            self, tmp_path, monkeypatch):
        """A persist send returning False aborts the rest of the sequence
        (fail-fast, like a failed demo step): nothing after it goes out."""
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events, persist_fail_at=1)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        topic1 = []
        for i, label in enumerate(PREVIEW_LABELS[:8]):
            topic1.append(("preview", f"Next: {label}…"))
            topic1.append(("card", i))
        assert events == [
            *topic1,
            ("preview", "Next: the thinking placeholder…"),
            ("placeholder",),
            ("persist", "Placeholder\nThinking"),
        ]
        assert len(adapter.cards) == 8  # topic 1 only; nothing after
        assert adapter.sends == []  # the failed persist never went out

    @pytest.mark.asyncio
    async def test_missing_demo_module_aborts_silently(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        monkeypatch.delitem(sys.modules, "gateway.draft_demo", raising=False)
        monkeypatch.setattr(sys, "meta_path", [_BlockDemoImport(), *sys.meta_path])
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        assert events == []  # nothing sent at all; the handler never breaks


class TestDraftGating:
    @pytest.mark.asyncio
    async def test_non_owner_is_silently_denied(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event(user_id=OTHER_ID))

        assert result is None
        assert events == []
        assert adapter.cards == []

    @pytest.mark.asyncio
    async def test_non_dm_is_silently_denied(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event(chat_type="group"))

        assert result is None
        assert events == []
        assert adapter.cards == []

    @pytest.mark.asyncio
    async def test_non_telegram_is_silently_denied(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(
            _make_event(platform=Platform.DISCORD))

        assert result is None
        assert events == []
        assert adapter.cards == []

    @pytest.mark.asyncio
    async def test_adapter_without_owner_checker_fails_closed(self, tmp_path, monkeypatch):
        events = []
        adapter = _NoOwnerCheckAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert result is None
        assert events == []
        assert adapter.cards == []


class TestDraftStyleAndFallback:
    @pytest.mark.asyncio
    async def test_no_card_exceeds_the_short_bound(self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        assert len(adapter.cards) == 40
        for html in adapter.cards:
            assert html.count("<h1>") == 1  # one title per message
            assert html.count("<li>") <= 3  # title plus at most three lines

    @pytest.mark.asyncio
    async def test_cards_have_no_emoji_and_exactly_one_copy_pill(
            self, tmp_path, monkeypatch):
        events = []
        adapter = _CardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        await runner._handle_draft_command(_make_event())

        assert len(adapter.cards) == 40
        for html in adapter.cards:
            _assert_no_emoji(html)
        assert sum(html.count('type="copy_text"') for html in adapter.cards) == 1
        assert 'type="copy_text"' in adapter.cards[-1]  # the final beat only
        assert 'type="copy_text"' not in "\n".join(adapter.cards[:-1])
        usage_line = next(
            line for line in adapter.cards[-1].splitlines()
            if 'type="copy_text"' in line)
        assert "Usage:" in usage_line

    @pytest.mark.asyncio
    async def test_card_failure_falls_back_to_plain_text(self, tmp_path, monkeypatch):
        events = []
        adapter = _NoCardAdapter(events)
        _install_fake_demo(monkeypatch, events)
        runner = _make_runner(adapter, _owner_home(tmp_path))

        result = await runner._handle_draft_command(_make_event())

        assert isinstance(result, str)
        assert "__init__" in result  # topic 1 fell back to text
        assert "Sync draft and fail-open" in result  # topic 3 fell back to text
        assert "SendMessageTextDraftAction" in result  # topic 4 fell back to text
        assert "SetTypingRequest" in result  # topic 5 fell back to text
        assert "Temporary reasoning" in result  # topic 6 fell back to text
        assert "Shown once" in result  # topic 7 fell back to text
        assert result.count("Usage:") == 1  # the final beat only
        assert adapter.cards == []  # no card method on this adapter
        assert len(adapter.sends) == 21  # persists still go out when cards fall back
