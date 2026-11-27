"""A native Telegram RichMessage and button showcase for ``/button``.

The rich body is parsed by Hermes' MTProto RichMessage parser, then composed with
native TL blocks that the parser does not currently author (inline RichText
buttons, Details, pull quotes, and a footer). Inline and single-use reply
keyboards are sent as separate MTProto messages. Every demo interaction is an
inert acknowledgement and never enters the agent/tool pipeline.
"""
from __future__ import annotations

import time
from typing import Any

CALLBACK_PREFIX = "hb:v1:"

_RICH_BODY = """## A block-based message

This is a native RichMessage block tree, not Markdown printed as plain text. It combines **bold**, ~~strikethrough~~, `inline code`, an [accent link](https://core.telegram.org/bots/api/rich-messages), and inline math $x^2 + y^2$.

---

### A real table

| Feature | Native block | Example |
|:---|:---:|---:|
| Heading | yes | 1 |
| Table | yes | 2 |
| RichText | yes | 3 |

### Lists and checklist

- A real unordered-list item
  - A nested item beneath it
- Another item

- [x] Completed task
- [ ] Pending task

### Quotation

> A regular block quotation, built as a PageBlockBlockquote. RichText inside the quote can still be styled.

### Code and display math

```python
from telethon import types
print("native RichMessage blocks")
```

$$
E = mc^2
$$
"""

_CALLBACK_LABELS = {
    "rich-inline": "RichMessage · inline callback button",
    "rich-row-neutral": "RichMessage · grouped neutral button",
    "rich-row-blue": "RichMessage · grouped blue button",
    "rich-nested-green": "RichMessage · nested green button",
    "rich-nested-red": "RichMessage · nested red button",
    "inline-neutral": "Inline keyboard · neutral callback button",
    "inline-blue": "Inline keyboard · blue callback button",
    "inline-green": "Inline keyboard · green callback button",
    "inline-red": "Inline keyboard · red callback button",
}

_REPLY_LABELS = {
    "Reply neutral": "Reply keyboard · neutral text button",
    "Reply blue": "Reply keyboard · blue text button",
    "Reply green": "Reply keyboard · green text button",
    "Reply red": "Reply keyboard · red text button",
}

_REPLY_KEYBOARD_TTL_SECONDS = 60 * 60
_MAX_ACTIVE_REPLY_KEYBOARDS = 256


def _plain(value: str) -> Any:
    from telethon import types

    return types.TextPlain(text=value)


def _rich_button(label: str, action: str, *, color: str | None = None, link_style: bool = False) -> Any:
    from telethon import types

    style = None
    if color is not None or link_style:
        style = types.RichButtonStyle(
            bg_primary=color == "primary" or None,
            bg_success=color == "success" or None,
            bg_danger=color == "danger" or None,
            link=True if link_style else None,
        )
    return types.PageButton(
        text=_plain(label),
        type=types.InlineButtonTypeCallback(data=f"{CALLBACK_PREFIX}{action}".encode("ascii")),
        style=style,
    )


def _inline_rich_text_button(label: str, action: str) -> Any:
    from telethon import types

    return types.TextButton(
        text=_plain(label),
        type=types.InlineButtonTypeCallback(data=f"{CALLBACK_PREFIX}{action}".encode("ascii")),
        style=types.RichButtonStyle(link=True),
    )


def _inline_button(label: str, action: str, *, color: str | None = None) -> Any:
    from telethon import types

    style = None
    if color is not None:
        style = types.KeyboardButtonStyle(**{f"bg_{color}": True})
    return types.KeyboardInlineButton(
        text=label,
        type=types.InlineButtonTypeCallback(data=f"{CALLBACK_PREFIX}{action}".encode("ascii")),
        style=style,
    )


def _reply_button(label: str, *, color: str | None = None) -> Any:
    from telethon import types

    style = None
    if color is not None:
        style = types.KeyboardButtonStyle(**{f"bg_{color}": True})
    return types.KeyboardButton(
        text=label,
        type=types.ButtonTypeDefault(),
        style=style,
    )


def _callback_payload(button: Any) -> str:
    return f"{CALLBACK_PREFIX}{button}".encode("ascii").decode("ascii")


def _walk_tl(nodes: Any):
    """Walk a Telethon block tree without depending on its repr formatting."""
    from telethon import types

    stack = list(nodes) if isinstance(nodes, (list, tuple)) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, types.TLObject):
            yield node
            for value in vars(node).values():
                if isinstance(value, (list, tuple)):
                    stack.extend(value)
                elif isinstance(value, types.TLObject):
                    stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def build_button_gallery() -> tuple[Any, Any, Any]:
    """Return the native RichMessage, ordinary inline keyboard, and reply keyboard."""
    from telethon import types
    from .mtproto_rich import input_rich_message

    parsed = input_rich_message(_RICH_BODY)
    if not isinstance(parsed, types.InputRichMessage):
        raise ValueError("The structured button showcase requires native RichMessage blocks")

    blocks = [
        types.PageBlockHeading1(text=_plain("Telegram RichMessage: beyond buttons")),
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            _plain("A structured MTProto message with a real inline RichText control: "),
            _inline_rich_text_button("Try inline rich button", "rich-inline"),
            _plain(". Its callback only acknowledges the tap."),
        ])),
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            _plain("Native styled text: "),
            types.TextBold(text=_plain("bold")),
            _plain(", "),
            types.TextItalic(text=_plain("italic")),
            _plain(", "),
            types.TextStrike(text=_plain("strikethrough")),
            _plain(", and "),
            types.TextButton(
                text=types.TextFixed(text=_plain("fixed-width code")),
                type=types.InlineButtonTypeCopy(copy_text="fixed-width code"),
                style=types.RichButtonStyle(link=True),
            ),
            _plain("."),
        ])),
        *parsed.blocks,
        types.PageBlockPullquote(
            text=_plain("A pull quote is a distinct native block, not a styled paragraph."),
            caption=_plain("Telegram RichMessage blocks"),
        ),
        types.PageBlockHeading2(text=_plain("Grouped and nested rich buttons")),
        types.PageBlockParagraph(text=_plain(
            "PageBlockButtonRow groups PageButton controls; the Details section below contains another real button row."
        )),
        types.PageBlockButtonRow(buttons=[
            _rich_button("Rich neutral", "rich-row-neutral"),
            _rich_button("Rich blue", "rich-row-blue", color="primary"),
        ]),
        types.PageBlockDetails(
            title=_plain("Expand for nested rich buttons"),
            open=True,
            blocks=[
                types.PageBlockParagraph(text=_plain(
                    "These are native PageButton objects nested inside PageBlockDetails."
                )),
                types.PageBlockButtonRow(buttons=[
                    _rich_button("Nested green", "rich-nested-green", color="success"),
                    _rich_button("Nested red", "rich-nested-red", color="danger"),
                ]),
            ],
        ),
        types.PageBlockDetails(
            title=_plain("Media, collage, and slideshow limitation"),
            open=False,
            blocks=[types.PageBlockParagraph(text=_plain(
                "Not included: native photo/video/collage/slideshow blocks need real Telegram media references (for example, an uploaded photo ID). This demo has no uploaded media assets, so it does not fabricate those blocks."
            ))],
        ),
        types.PageBlockFooter(text=_plain(
            "RichMessage content, button rows, and the keyboards below use distinct native MTProto structures."
        )),
    ]
    from .rich_blocks import _validate_native_blocks
    _validate_native_blocks(blocks)
    rich_message = types.InputRichMessage(blocks=blocks)

    inline_markup = types.ReplyInlineMarkup(rows=[
        types.KeyboardInlineButtonRow(buttons=[
            _inline_button("Inline neutral", "inline-neutral"),
            _inline_button("Inline blue", "inline-blue", color="primary"),
        ]),
        types.KeyboardInlineButtonRow(buttons=[
            _inline_button("Inline green", "inline-green", color="success"),
            _inline_button("Inline red", "inline-red", color="danger"),
        ]),
    ])

    reply_markup = types.ReplyKeyboardMarkup(rows=[
        types.KeyboardButtonRow(buttons=[_reply_button("Reply neutral")]),
        types.KeyboardButtonRow(buttons=[
            _reply_button("Reply blue", color="primary"),
            _reply_button("Reply green", color="success"),
            _reply_button("Reply red", color="danger"),
        ]),
    ], resize=True, single_use=True)
    return rich_message, inline_markup, reply_markup


def callback_acknowledgement(data: str) -> str | None:
    """Resolve one known callback to a bounded, non-sensitive toast."""
    value = str(data or "")
    if not value.startswith(CALLBACK_PREFIX):
        return None
    label = _CALLBACK_LABELS.get(value[len(CALLBACK_PREFIX):])
    return f"Button demo: {label}" if label else None


def reply_keyboard_acknowledgement(text: str) -> str | None:
    """Resolve one exact reply-keyboard label without treating it as a prompt."""
    label = _REPLY_LABELS.get(str(text or "").strip())
    return f"Button demo: {label}" if label else None


def _keyboard_state(adapter: Any) -> dict[tuple[str, str], float]:
    state = getattr(adapter, "_button_demo_reply_keyboards", None)
    if not isinstance(state, dict):
        state = {}
        adapter._button_demo_reply_keyboards = state
    now = time.monotonic()
    for key, created_at in list(state.items()):
        if now - float(created_at) > _REPLY_KEYBOARD_TTL_SECONDS:
            state.pop(key, None)
    while len(state) > _MAX_ACTIVE_REPLY_KEYBOARDS:
        state.pop(next(iter(state)))
    return state


def _keyboard_key(source: Any) -> tuple[str, str]:
    return str(getattr(source, "chat_id", "") or ""), str(getattr(source, "user_id", "") or "")


def _clear_chat_keyboards(state: dict[tuple[str, str], float], chat_id: Any) -> bool:
    chat = str(chat_id or "")
    keys = [key for key in state if key[0] == chat]
    for key in keys:
        state.pop(key, None)
    return bool(keys)


def _topic_id(source: Any) -> int | None:
    topic = str(getattr(source, "thread_id", "") or "")
    return int(topic) if topic.isdecimal() else None


async def _send_keyboard_notice(adapter: Any, source: Any, text: str, *, reply_to: Any = None) -> Any:
    from telethon import types
    from .mtproto_rich import send_request

    request = send_request(
        adapter._peer_for(source.chat_id),
        f"## Button demo\n\n{text}",
        reply_to=int(reply_to) if reply_to is not None else None,
        top_msg_id=_topic_id(source),
        reply_markup=types.ReplyKeyboardHide(),
        rich_format="blocks",
    )
    return await adapter._mtproto_rich_request(request)


async def handle_reply_keyboard_text(adapter: Any, source: Any, text: str, *, reply_to: Any = None) -> bool:
    """Consume demo text buttons before normal Telegram/agent message routing."""
    acknowledgement = reply_keyboard_acknowledgement(text)
    if acknowledgement is None:
        return False

    _clear_chat_keyboards(_keyboard_state(adapter), source.chat_id)
    if adapter._authorized(source):
        await _send_keyboard_notice(adapter, source, acknowledgement, reply_to=reply_to)
    return True


async def clear_reply_keyboard_for_command(
    adapter: Any, source: Any, *, reply_to: Any = None,
) -> bool:
    """Hide an active demo keyboard before routing a new command."""
    state = _keyboard_state(adapter)
    if not _clear_chat_keyboards(state, source.chat_id):
        return False
    if adapter._authorized(source):
        await _send_keyboard_notice(
            adapter, source,
            "The single-use reply keyboard was cleared because you started another command.",
            reply_to=reply_to,
        )
    return True


async def show_button_gallery(adapter: Any, source: Any, *, reply_to: Any = None) -> Any:
    """Send the native RichMessage, inline keyboard, then a separate single-use keyboard."""
    from telethon import functions, types
    from .mtproto_rich import send_request

    rich_message, inline_markup, reply_markup = build_button_gallery()
    peer = adapter._peer_for(source.chat_id)
    topic_id = _topic_id(source)
    reply_header = None
    if reply_to is not None:
        reply_header = types.InputReplyToMessage(
            reply_to_msg_id=int(reply_to),
            top_msg_id=topic_id,
        )
    rich_request = functions.messages.SendMessageRequest(
        peer=peer,
        # MTProto requires the ordinary message field alongside rich_message;
        # this companion string is not a fallback or separate send.
        message=_RICH_BODY,
        reply_to=reply_header,
        reply_markup=inline_markup,
        rich_message=rich_message,
    )
    result = await adapter._mtproto_rich_request(rich_request)

    # A regular reply keyboard is a separate message/ReplyMarkup, never mixed
    # into the inline keyboard or RichMessage block tree.
    keyboard_request = send_request(
        peer,
        "### Single-use reply keyboard\n\nTap a text button below. Its message is intercepted, acknowledged with RichMessage, and never sent to the agent.",
        reply_to=getattr(result, "id", None),
        top_msg_id=topic_id,
        reply_markup=reply_markup,
        rich_format="blocks",
    )
    await adapter._mtproto_rich_request(keyboard_request)
    _keyboard_state(adapter)[_keyboard_key(source)] = time.monotonic()
    return result
