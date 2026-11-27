"""Message-edit protocol: what an edit turn carries and how it is answered.

The turn recovers the old text and the previous reply from the loaded
history (the corrected text is appended as a new row, never mutating past
context), gets a one-shot note telling it what changed, and the final send
decides whether to edit the previous reply in place, send nothing (the
correction changed nothing), or send a new message.
"""
from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.message_edits import (
    EDIT_METADATA_CORRECTS_REPLY_ID,
    EDIT_METADATA_IS_EDIT,
    EDIT_METADATA_MESSAGE_ID,
    EDIT_UNCHANGED_SENTINEL,
    build_edit_note,
    decide_edit_correction,
    find_edit_context,
)
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_envelope import snapshot_native
from persistence.core.errors import AuthorityRefusal


def _history():
    return [
        {"role": "user", "content": "the ghost is down", "message_id": "400"},
        {"role": "assistant", "content": "The ghost server is unreachable."},
        {"role": "user", "content": "and the db too", "message_id": "401"},
        {"role": "assistant", "content": "Checking the database as well."},
    ]


def test_find_edit_context_recovers_old_text_and_previous_reply():
    """The edited user row gives the old text; the assistant row after it is the reply."""
    old_text, previous_reply = find_edit_context(_history(), "400")
    assert old_text == "the ghost is down"
    assert previous_reply == "The ghost server is unreachable."


def test_find_edit_context_takes_the_latest_row_for_a_twice_edited_message():
    """A message edited twice: the old text is the most recent row before this edit."""
    history = _history() + [
        {"role": "user", "content": "the host is down", "message_id": "400"},
        {"role": "assistant", "content": "The host server is unreachable."},
    ]
    old_text, previous_reply = find_edit_context(history, "400")
    assert old_text == "the host is down"
    assert previous_reply == "The host server is unreachable."


def test_find_edit_context_returns_nothing_when_the_message_is_unknown():
    """No user row with that id: no old text, no previous reply, no note."""
    assert find_edit_context(_history(), "999") == (None, None)


def test_find_edit_context_returns_no_reply_when_the_agent_had_not_replied():
    """The edit landed before any reply: the old text is known, the reply is not."""
    history = [{"role": "user", "content": "the ghost is down", "message_id": "400"}]
    assert find_edit_context(history, "400") == ("the ghost is down", None)


def test_find_edit_context_ignores_non_text_content():
    """A media-only row has no text to compare against."""
    history = [{"role": "user", "content": [{"type": "image"}], "message_id": "400"}]
    assert find_edit_context(history, "400") == (None, None)


def test_build_edit_note_is_none_without_old_text():
    """No old text means no note: the turn just answers the new text."""
    assert build_edit_note(old_text=None) is None
    assert build_edit_note(old_text="   ") is None


def test_build_edit_note_before_any_reply_names_the_original():
    """The agent had not replied: the note says so and quotes the original."""
    note = build_edit_note(old_text="the ghost is down")
    assert note is not None
    assert "the ghost is down" in note
    assert "before you replied" in note
    assert EDIT_UNCHANGED_SENTINEL not in note


def test_build_edit_note_with_a_correctable_reply_gives_the_unchanged_protocol():
    """A previous reply that can be edited in place: the note carries the
    UNCHANGED protocol and the previous reply for comparison."""
    note = build_edit_note(
        old_text="the ghost is down",
        previous_reply_text="The ghost server is unreachable.",
        can_edit_reply=True,
    )
    assert note is not None
    assert "the ghost is down" in note
    assert "The ghost server is unreachable." in note
    assert "replace your previous reply" in note
    assert EDIT_UNCHANGED_SENTINEL in note


def test_build_edit_note_with_an_uncorrectable_reply_has_no_unchanged_protocol():
    """A previous reply with no id to edit: the answer goes out as a new reply,
    so the model is never told the UNCHANGED sentinel."""
    note = build_edit_note(
        old_text="the ghost is down",
        previous_reply_text="The ghost server is unreachable.",
        can_edit_reply=False,
    )
    assert note is not None
    assert "as a new reply" in note
    assert EDIT_UNCHANGED_SENTINEL not in note


def test_decide_edit_correction_edits_the_previous_reply():
    """A changed answer with a previous reply id: edit it in place."""
    assert decide_edit_correction(final_text="The host is down.", previous_reply_id="900") == "edit"


def test_decide_edit_correction_sends_nothing_on_unchanged():
    """The exact UNCHANGED sentinel: the previous reply stands, send nothing."""
    assert decide_edit_correction(final_text="UNCHANGED", previous_reply_id="900") == "unchanged"
    assert decide_edit_correction(final_text="  UNCHANGED  ", previous_reply_id="900") == "unchanged"


def test_decide_edit_correction_sends_a_new_message_without_a_reply_id():
    """No previous reply id (cache cold, restart): a normal new message."""
    assert decide_edit_correction(final_text="The host is down.", previous_reply_id=None) == "send"


def test_decide_edit_correction_treats_a_near_sentinel_as_an_answer():
    """Only the exact sentinel means unchanged; a lookalike is a real answer
    that replaces the previous reply."""
    assert decide_edit_correction(final_text="unchanged", previous_reply_id="900") == "edit"
    assert decide_edit_correction(final_text="UNCHANGED!", previous_reply_id="900") == "edit"


# ── Native admission: the adapter-stamped edit metadata ──────────────────


def _telegram_runner(tmp_path, monkeypatch):
    """Real runner + unconnected TelethonAdapter for a group reply. Only the
    authorization seam is stubbed (covered elsewhere) so the test isolates
    the metadata clause of admission."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    adapter = TelethonAdapter(PlatformConfig())
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda _source, **_kw: True)
    source = adapter.build_source(chat_id="-1002397157264", chat_type="group",
                                  user_id="42", user_name="owner")
    return runner, source


def test_edit_metadata_crosses_native_admission_and_is_projected(tmp_path, monkeypatch):
    """An edit turn's metadata is admitted, not refused: live, every edit
    died at admission with invalid_params (metadata), so the message-edits
    feature could never complete a turn. The keys also survive the durable
    projection, so a queued edit still knows it is an edit when drained."""
    runner, source = _telegram_runner(tmp_path, monkeypatch)
    event = MessageEvent(
        text="Emilia", source=source, message_id="290168",
        metadata={EDIT_METADATA_IS_EDIT: True, EDIT_METADATA_MESSAGE_ID: "290168",
                  EDIT_METADATA_CORRECTS_REPLY_ID: "900"},
        edit_revision="290168:1793000000",
    )
    payload = snapshot_native(runner, event)
    stored = payload["native_text_v1"]["event"]["metadata"]
    assert stored[EDIT_METADATA_IS_EDIT] is True
    assert stored[EDIT_METADATA_MESSAGE_ID] == "290168"
    assert stored[EDIT_METADATA_CORRECTS_REPLY_ID] == "900"


def test_unknown_telegram_metadata_still_refuses(tmp_path, monkeypatch):
    """Admitting the edit keys does not open the gate: a key no adapter
    stamps still refuses with invalid_params (metadata)."""
    runner, source = _telegram_runner(tmp_path, monkeypatch)
    event = MessageEvent(text="Emilia", source=source, message_id="290169",
                         metadata={"forged_key": True})
    with pytest.raises(AuthorityRefusal) as raised:
        snapshot_native(runner, event)
    assert raised.value.reason == "invalid_params" and raised.value.field == "metadata"


@pytest.mark.asyncio
async def test_refused_event_is_marked_so_its_notice_records_no_reply():
    """A refused event is marked at admission: the final send then skips the
    threaded-reply marker, so the refusal card never arms the edit tracker
    (a refused message was never answered)."""
    from types import SimpleNamespace

    from gateway.session import SessionSource
    from gateway.session_ingress import admit_message

    class _Authority:
        runner = SimpleNamespace(_draining=False)

        async def admit_native(self, _event):
            raise AuthorityRefusal("invalid_params", field="metadata")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1002397157264",
                           chat_type="group", user_id="42")
    event = MessageEvent(text="Emilia", source=source, message_id="290168")
    reply = await admit_message(_Authority(), event)
    assert "invalid_params" in reply
    assert getattr(event, "_hermes_admission_refused", False) is True
