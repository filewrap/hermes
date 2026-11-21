"""Rich-card builders for the /draft showcase.

Split out of ``gateway/slash_commands_model.py`` (code-health offset: that
file is over its line cap and may only shrink). The mixin imports these
lazily inside ``_handle_draft_command``. The /draft owner check stays with
the mixin — it is authorization, not a card builder.
"""

from __future__ import annotations

from agent.i18n import t


# (member, few-word description) — public surface first, internals after.
_DRAFT_STREAM_MEMBERS: tuple[tuple[str, str], ...] = (
    ("__init__", "Creates the sink for one DM turn."),
    ("random_id", "One fixed id animating the generation."),
    ("stop_requested", "True once a Stop press arrives."),
    ("partial_text", "Streamed content so far; the final if stopped."),
    ("request_stop", "Halts the draft and the generation."),
    ("prime_thinking_placeholder", "Static \"Thinking\" placeholder draft first."),
    ("on_reasoning", "Appends a reasoning delta."),
    ("on_content", "Appends a content delta."),
    ("on_delta", "Stream-callback interface for deltas."),
    ("finish_sync_draft", "Sync draft, then stops the pump."),
    ("cancel", "Stops the pump; fires the teardown hooks."),
    ("clear_draft", "Clears the live draft after the final lands."),
    ("_frame", "Current draft frame, or None."),
    ("_maybe_send", "Sends if the gate is open, else pending."),
    ("_send_now", "Dispatches one frame at once."),
    ("_ensure_pump", "Starts the keepalive pump once."),
    ("_pump", "Re-pushes the frame inside the TTL."),
    ("_send_sync", "Sync-draft frame; blocks until it lands."),
    ("_cancel_pump", "Cancels the pump task, if any."),
    ("_resolve_input_peer", "Resolves the chat for SetTyping."),
    ("_build_action", "Builds the draft action for a frame."),
    ("_send_frame", "Sends one frame; False on failure."),
    ("_note_failure", "Counts failures; gives up after three."),
)


def _draft_card_usage_html() -> str:
    """The Usage line — the ONLY copy pill in any /draft response."""
    from plugins.platforms.telegram.mtproto_rich import rich_button

    command = t("gateway.draft.card_usage_command")
    return (
        f"<p><i>{t('gateway.draft.card_usage_label')}</i> "
        f"{rich_button(command, kind='copy', value=command)}</p>"
    )


def _draft_card_html(title_key: str, body: str, *, with_usage: bool = False) -> str:
    """``<h1>`` title over the card body. Only the final beat carries the
    Usage line (the ONLY copy pill in any /draft response)."""
    import html

    usage = f"\n{_draft_card_usage_html()}" if with_usage else ""
    return f"<h1>{html.escape(t(title_key), quote=False)}</h1>\n{body}{usage}"


def _draft_member_row(name: str, description: str) -> str:
    """One ``<li><code>name</code>: description</li>`` row — indented under the card heading."""
    import html

    return (f"<li><code>{html.escape(name, quote=False)}</code>: "
            f"{html.escape(description, quote=False)}</li>")


def _draft_members_html(title_key: str, names: tuple[str, ...],
                        *, with_usage: bool = False) -> str:
    """One short beat listing a few ResponseStreamDraft members by name."""
    descriptions = dict(_DRAFT_STREAM_MEMBERS)
    rows = "\n".join(
        ["<ul>", *(_draft_member_row(name, descriptions[name]) for name in names), "</ul>"])
    return _draft_card_html(title_key, rows, with_usage=with_usage)


def _draft_lines_html(title_key: str, lines: tuple[str, ...],
                      *, with_usage: bool = False) -> str:
    """One short beat: a title plus at most three short lines."""
    rows = "\n".join(["<ul>", *(f"<li>{line}</li>" for line in lines), "</ul>"])
    return _draft_card_html(title_key, rows, with_usage=with_usage)


def _draft_html_to_text(html: str) -> str:
    """Plain-text fallback for a draft card (card send failed): strip tags, unescape."""
    import html as html_lib
    import re

    text = re.sub(r"<[^>]+>", "", html or "")
    lines = [html_lib.unescape(line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
