"""Access messages are presentation only; grants remain in the access controls."""
from __future__ import annotations

from html import escape
import json

from .mtproto_rich import inline_markup
from .start_ui import CAPABILITIES


ALLOWLIST_PAGE_SIZE = 20


def allowlist_view(
    users: list[dict], page_index: int, *, default_policy: dict,
    user_policies: dict[str, dict], owner_id: str, note: str = "",
) -> tuple[str, object]:
    """Render an owner-private, stable numeric-ID page and its visible policy state."""
    numeric_users = [
        user for user in users
        if str(user.get("user_id") or user.get("id") or "").isdecimal()
    ]
    by_id = {str(user.get("user_id") or user.get("id")): user for user in numeric_users}
    by_id.setdefault(str(owner_id), {"user_id": str(owner_id), "name": "Configured owner", "owner": True})
    ordered = sorted(by_id.values(), key=lambda user: int(user.get("user_id") or user.get("id")))
    page_count = max(1, (len(ordered) + ALLOWLIST_PAGE_SIZE - 1) // ALLOWLIST_PAGE_SIZE)
    if type(page_index) is not int or not 1 <= page_index <= page_count:
        raise ValueError("Allowlist page index is out of range")
    page = ordered[(page_index - 1) * ALLOWLIST_PAGE_SIZE:page_index * ALLOWLIST_PAGE_SIZE]

    def policy_html(policy: dict) -> str:
        return "<pre>" + escape(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2)) + "</pre>"

    parts = [
        "<h1>Access allowlist</h1>",
        f"<p>Page {page_index} of {page_count} · {len(ordered)} numeric account(s).</p>",
        "<p>Global grants are separate from group-only Allow all. Owner controls policy; the configured owner bypasses these policy rules.</p>",
        "<h2>Default policy · <code>allowed</code></h2>",
        policy_html(default_policy),
    ]
    if note:
        parts.append(f"<p>{escape(note)}</p>")
    button_rows = []
    if page_index > 1:
        button_rows.append([("Previous", f"hm:allowlist:page:{page_index - 1}", "primary")])
    if page_index < page_count:
        button_rows.append([("Next", f"hm:allowlist:page:{page_index + 1}", "primary")])
    if page:
        parts.append("<table><tr><th>Telegram ID</th><th>Account</th><th>Global state</th><th>Policy</th></tr>")
        for user in page:
            user_id = str(user.get("user_id") or user.get("id"))
            is_owner = user_id == str(owner_id)
            name = escape(str(user.get("name") or user_id))
            status = "Configured owner" if is_owner else (
                "Banned" if user.get("banned") is True else "Globally allowed"
            )
            policy = user_policies.get(user_id, {})
            summary = "owner bypass" if is_owner else (
                "same as default" if policy == default_policy else "user-specific override"
            )
            parts.append(
                f"<tr><td><code>{escape(user_id)}</code></td><td>{name}</td>"
                f"<td>{status}</td><td>{summary}{policy_html(policy) if not is_owner and policy != default_policy else ''}</td></tr>"
            )
            if not is_owner:
                button_rows.append([("Revoke", f"hm:allowlist:revoke:{user_id}:{page_index}", "danger")])
        parts.append("</table>")
    else:
        parts.append("<p>No global grants are recorded.</p>")
    parts.extend([
        "<p>Grant a started user across this profile with <code>/allow numeric_id</code>; the user must first send <code>/start</code> privately. "
        "This opens the existing one-card grant confirmation. Revoke an individual global grant with the button beside that account; revocation does not ban them or change group guest access.</p>",
        "<p>Use <code>/allowlist policy [allowed|numeric_id] [JSON]</code> to view or save a policy. "
        "Supported keys: <code>allowed_tools</code> (terminal, read_file, write_file), "
        "<code>denied_tools</code>, <code>denied_actions</code>, <code>denied_paths</code>, "
        "<code>allowed_paths</code> (only canonical paths inside <code>/workspace</code>), and "
        "<code>permitted_effects</code> (read, write, shell). Shell is disabled by path restrictions, "
        "any denied action, or denied read/write effects; bounded file tools may remain available. "
        "These are not arbitrary host-tool or browser/publish/upload/delegate grants.</p>",
    ])
    from .mtproto_rich import inline_markup
    return "\n".join(parts), inline_markup(button_rows)


def policy_view(subject: str, policy: dict, *, saved: bool = False) -> str:
    """Show a policy in a rich message, without implying it gates the owner."""
    title = "Policy saved" if saved else "Policy state"
    rendered = escape(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2))
    return (
        f"<h1>{title} · <code>{escape(subject)}</code></h1>"
        f"<pre>{rendered}</pre>"
        "<p>The configured owner bypasses this policy. It controls only the documented bounded "
        "tool/action/path rules; it does not grant arbitrary host tools, browser sessions, publishing, "
        "uploads, or delegation.</p>"
        "<p>Supported keys: <code>allowed_tools</code> (terminal, read_file, write_file), "
        "<code>denied_tools</code>, <code>denied_actions</code>, <code>denied_paths</code>, "
        "<code>allowed_paths</code> (canonical paths inside <code>/workspace</code>), "
        "<code>permitted_effects</code> (read, write, shell). Shell is disabled whenever path/action "
        "restrictions or read/write effect restrictions require it; file tools remain bounded.</p>"
    )


def _name(user: dict) -> str:
    return escape(str(user.get("name") or user.get("id") or "there"))


def pending_start_rich_message(user: dict, *, bot_name: str = "Naina"):
    """Owner-private pending-start notice as native RichMessage blocks.

    Same content as :func:`pending_start_view`, but the Ban/Allow controls
    are a real ``PageBlockButtonRow`` of callback buttons instead of HTML
    text: the adapter's ``_send_rich_ui`` string branch would only carry
    them as unparsed ``<tg-button-row>`` markup, and the owner notice must
    arrive as buttons the owner can tap. The display name stays raw here —
    blocks carry no HTML to inject into — while the HTML view escapes it.
    """
    from telethon import types

    user_id = str(user["id"])
    if not user_id.isdecimal():
        raise ValueError("A numeric Telegram user ID is required")
    name = str(user.get("name") or user_id)
    bot = str(bot_name or "Naina")
    return types.InputRichMessage(blocks=[
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            types.TextPlain(text=name),
            types.TextPlain(text=" ("),
            types.TextFixed(text=types.TextPlain(text=user_id)),
            types.TextPlain(text=f") started {bot}."),
        ])),
        types.PageBlockParagraph(text=types.TextPlain(text="Actions you can take:")),
        types.PageBlockButtonRow(buttons=[
            types.PageButton(
                text=types.TextPlain(text="Ban"),
                type=types.InlineButtonTypeCallback(data=f"hm:admission:ban:{user_id}".encode("ascii")),
                style=types.RichButtonStyle(bg_danger=True),
            ),
            types.PageButton(
                text=types.TextPlain(text="Allow"),
                type=types.InlineButtonTypeCallback(data=f"hm:admission:allow:{user_id}".encode("ascii")),
                style=types.RichButtonStyle(bg_success=True),
            ),
        ]),
    ])


def pending_start_view(user: dict, *, bot_name: str = "Naina") -> tuple[str, None]:
    """Owner-private presentation only; buttons delegate to existing policy paths."""
    user_id = str(user["id"])
    if not user_id.isdecimal():
        raise ValueError("A numeric Telegram user ID is required")
    from .mtproto_rich import rich_button, rich_button_row
    actions = rich_button_row([
        rich_button("Ban", kind="callback", value=f"hm:admission:ban:{user_id}", style="danger"),
        rich_button("Allow", kind="callback", value=f"hm:admission:allow:{user_id}", style="success"),
    ])
    return (
        f"<p><b>{escape(str(user.get('name') or user_id))}</b> "
        f"(<code>{escape(user_id)}</code>) started {escape(bot_name or 'Naina')}.</p>"
        "<p>Actions you can take:</p>"
        f"{actions}",
        None,
    )


def allow_success_view(user: dict, *, isolated: bool, chat_label: str) -> str:
    """Explicit HTML paragraph boundaries prevent names becoming Markdown headings."""
    if isolated:
        description = "can now message Naina directly. Their conversations and workspace stay separate from yours."
    else:
        description = f"can now contact Naina in {escape(chat_label)}. Access elsewhere is unchanged."
    return (
        "<h2>Access granted</h2>"
        f"<p><b>{_name(user)}</b> {description}</p>"
    )


def disallow_success_view(user: dict) -> str:
    """One revoked account, without a redundant one-page footer."""
    return (
        "<h2>Global access revoked</h2>"
        f"<p><b>{_name(user)}</b> no longer has global access to this profile.</p>"
        "<p>Group guest access, when enabled by that group's Allow all setting, is separate.</p>"
    )


def access_granted_view(user: dict, *, direct: bool, chat_label: str) -> tuple[str, object]:
    """A future grant notice, never an invitation to bypass scoped authorization."""
    access = (
        "You've been added to this profile, so you can now message me directly."
        if direct else
        f"You've been given access to chat with me in {escape(chat_label)}. Access elsewhere is unchanged."
    )
    bullets = "".join(f"<li>{escape(text)}</li>" for text in CAPABILITIES)
    return (
        f"<p>Hey <b>{_name(user)}</b>! You're in.</p>"
        f"<p>{access} I'm Naina, your AI assistant.</p>"
        f"<ul>{bullets}</ul>"
        "<p>Bring a question, a half-formed idea, or something you'd like to get done. "
        "Send /help whenever you'd like a look around.</p>",
        inline_markup([[('Show me around', 'hm:help:menu:0', 'primary')]]) if direct else None,
    )
