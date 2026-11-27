"""Access messages are presentation only; grants remain in the access controls."""
from __future__ import annotations

from html import escape
import json

from .mtproto_rich import inline_markup, rich_button, rich_button_row, rich_page_footer
from .start_ui import CAPABILITIES


ALLOWLIST_PAGE_SIZE = 20


def allowlist_view(
    users: list[dict], page_index: int, *, default_policy: dict,
    user_policies: dict[str, dict], owner_id: str, note: str = "",
) -> tuple[str, None]:
    """Render an owner-private, stable numeric-ID page and its visible policy state.

    Row actions are in-body rich pills inside the table: Revoke and Ban per
    account, disabled for the configured owner, who always sorts first.
    Pagination is an in-body rich button row; the screen carries no reply
    keyboard.
    """
    owner = str(owner_id or "")
    numeric_users = [
        user for user in users
        if str(user.get("user_id") or user.get("id") or "").isdecimal()
    ]
    by_id = {str(user.get("user_id") or user.get("id")): user for user in numeric_users}
    if owner.isdecimal():
        by_id.setdefault(owner, {"user_id": owner, "name": "Configured owner", "owner": True})
    ordered = sorted(
        by_id.values(),
        key=lambda user: (
            str(user.get("user_id") or user.get("id")) != owner,
            int(str(user.get("user_id") or user.get("id"))),
        ),
    )
    page_count = max(1, (len(ordered) + ALLOWLIST_PAGE_SIZE - 1) // ALLOWLIST_PAGE_SIZE)
    if type(page_index) is not int or not 1 <= page_index <= page_count:
        raise ValueError("Allowlist page index is out of range")
    page = ordered[(page_index - 1) * ALLOWLIST_PAGE_SIZE:page_index * ALLOWLIST_PAGE_SIZE]

    def policy_html(policy: dict) -> str:
        return "<pre>" + escape(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2)) + "</pre>"

    def actions_html(user_id: str, is_owner: bool) -> str:
        if is_owner:
            return " ".join([
                rich_button("Revoke", kind="disabled", style="danger"),
                rich_button("Ban", kind="disabled", style="danger"),
            ])
        return " ".join([
            rich_button("Revoke", kind="callback",
                        value=f"hm:allowlist:revoke:{user_id}:{page_index}", style="danger"),
            rich_button("Ban", kind="callback",
                        value=f"hm:allowlist:ban:{user_id}:{page_index}", style="danger"),
        ])

    parts = [
        "<h1>Access allowlist</h1>",
        "<p>Global grants are separate from group-only Allow all. Owner controls policy; the configured owner bypasses these policy rules.</p>",
        "<h2>Default policy · <code>allowed</code></h2>",
        policy_html(default_policy),
    ]
    if note:
        parts.append(f"<p>{escape(note)}</p>")
    if page:
        parts.append("<table><tr><th>Telegram ID</th><th>Account</th><th>Global state</th><th>Policy</th><th>Actions</th></tr>")
        for user in page:
            user_id = str(user.get("user_id") or user.get("id"))
            is_owner = user_id == owner
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
                f"<td>{status}</td><td>{summary}{policy_html(policy) if not is_owner and policy != default_policy else ''}</td>"
                f"<td>{actions_html(user_id, is_owner)}</td></tr>"
            )
        parts.append("</table>")
        nav = []
        if page_index > 1:
            nav.append(rich_button("Previous", kind="callback",
                                   value=f"hm:allowlist:page:{page_index - 1}", style="primary"))
        if page_index < page_count:
            nav.append(rich_button("Next", kind="callback",
                                   value=f"hm:allowlist:page:{page_index + 1}", style="primary"))
        if nav:
            parts.append(rich_button_row(nav))
    else:
        parts.append("<p>No global grants are recorded.</p>")
    parts.extend([
        "<p>Grant a started user across this profile with <code>/allow numeric_id</code>; the user must first send <code>/start</code> privately. "
        "This opens the existing one-card grant confirmation. Use Revoke in an account's row to remove only its global grant; revocation does not ban the account or change group guest access. "
        "Use Ban in the same row to ban the account; a banned account stays denied even while its global grant is recorded.</p>",
        "<p>Use <code>/allowlist policy [allowed|numeric_id] [JSON]</code> to view or save a policy. "
        "Supported keys: <code>allowed_tools</code> (terminal, read_file, write_file), "
        "<code>denied_tools</code>, <code>denied_actions</code>, <code>denied_paths</code>, "
        "<code>allowed_paths</code> (only canonical paths inside <code>/workspace</code>), and "
        "<code>permitted_effects</code> (read, write, shell). Shell is disabled by path restrictions, "
        "any denied action, or denied read/write effects; bounded file tools may remain available. "
        "These are not arbitrary host-tool or browser/publish/upload/delegate grants.</p>",
    ])
    parts.append(rich_page_footer(page_index, page_count, len(ordered)))
    return "\n".join(parts), None


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


def _bot_display_name(bot_name: str | None) -> str:
    """The bot's own display name; "Hermes" when the adapter has none yet."""
    return str(bot_name or "").strip() or "Hermes"


# --- /allowgc views -------------------------------------------------------
# Plain language, no emojis, no hype, no functions exposed. Explanations live
# inside <details> collapsibles. Toggle buttons are real inline-keyboard
# rows: style "success" (green) when ON, None when OFF.

_ALLOWGC_TOGGLE_LABELS = (
    ("tools", "Tools"),
    ("web", "Web"),
    ("shell", "Shell"),
    ("trigger", "Trigger"),
    ("cmd_restrict", "Commands restricted"),
)


def _allowgc_toggle_rows(toggles: dict | None, token: str) -> list:
    """Inline-keyboard rows of two access toggles each, coloured by state.

    A toggle missing from the dict falls back to its default in
    ``DEFAULT_TOGGLES`` — not a blanket True — so a toggle that ships
    off (commands restricted) never renders On for a stale card.
    """
    from gateway.allowgc import DEFAULT_TOGGLES
    buttons = []
    for name, label in _ALLOWGC_TOGGLE_LABELS:
        on = bool((toggles or {}).get(name, DEFAULT_TOGGLES.get(name, True)))
        buttons.append((
            f"{label}: {'On' if on else 'Off'}",
            f"hm:allowgc:toggle:{name}:{token}",
            "success" if on else None,
        ))
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _allowgc_heading() -> str:
    """The feature name with its beta badge right after it."""
    from .rich_ui import _settings_badge

    return f"<p><b>Allowgc</b> {_settings_badge('beta', 'badge:beta')}</p>"


def _allowgc_state_badge(word: str, on: bool) -> str:
    """Inert word badge for an allowgc state word.

    Green (success) for "enabled"/"on", red (danger) for "disabled"/"off",
    so the state reads at a glance. The beta badge in the heading stays
    blue (primary); only these state words use green/red.
    """
    return rich_button(word, kind="disabled", style="success" if on else "danger")


def allowgc_confirmation_view(chat_name: str, chat_id, toggles: dict | None,
                              token: str) -> tuple[str, object]:
    """Owner confirmation card for turning /allowgc on in a group."""
    name = escape(str(chat_name or "this group"))
    ident = escape(str(chat_id))
    lines = [
        _allowgc_heading(),
        f"<p>Allow every member of <b>{name}</b> (<code>{ident}</code>) to run Hermes in this group?</p>",
        "<p>Everyone in the group shares one profile and one conversation. "
        "The configured owner stays the owner: only the owner can change access or turn this off.</p>",
        "<p><i>Click confirm to enable it in this group.</i></p>",
    ]
    markup = inline_markup(_allowgc_toggle_rows(toggles, token) + [[
        ("Confirm", f"hm:allowgc:confirm:{token}", "success"),
        ("Cancel", f"hm:allowgc:cancel:{token}", "danger"),
    ]])
    return "\n".join(lines), markup


def allowgc_enabled_view(chat_name: str, chat_id, toggles: dict | None,
                         token: str, *, show_save: bool = False) -> tuple[str, object]:
    """The same card, edited to say the group can now run Hermes.

    The state is always shown: a "Status:" line with an inert badge on the
    word "enabled", and one bullet per access with an inert badge on its
    on/off word. With show_save=True (a staged change is waiting) the card
    also carries a Save button and a "Click save..." line; otherwise the
    buttons are the toggles plus Turn off, and the footer is the default
    "Click the buttons..." line.
    """
    from gateway.allowgc import DEFAULT_TOGGLES
    name = escape(str(chat_name or "this group"))
    ident = escape(str(chat_id))
    lines = [
        _allowgc_heading(),
        f"<p>All users in <b>{name}</b> (<code>{ident}</code>) can now run Hermes in this group.</p>",
        "<p>Everyone shares one profile and one conversation here.</p>",
        f"<p>Status: {_allowgc_state_badge('enabled', True)}</p>",
    ]
    for toggle_name, label in _ALLOWGC_TOGGLE_LABELS:
        on = bool((toggles or {}).get(toggle_name, DEFAULT_TOGGLES.get(toggle_name, True)))
        lines.append(
            f"<p>• {label}: {_allowgc_state_badge('on' if on else 'off', on)}</p>"
        )
    lines.append("<hr />")
    lines.append("<p><i>Click the buttons to turn access on or off.</i></p>")
    if show_save:
        lines.append("<p><i>Click save to save the current changes.</i></p>")
    rows = _allowgc_toggle_rows(toggles, token)
    if show_save:
        rows.append([
            ("Save", f"hm:allowgc:save:{token}", "success"),
            ("Turn off", f"hm:allowgc:turnoff:{token}", "danger"),
        ])
    else:
        rows.append([("Turn off", f"hm:allowgc:turnoff:{token}", "danger")])
    markup = inline_markup(rows)
    return "\n".join(lines), markup


def allowgc_expired_view() -> str:
    """Expired confirmation card: same message, never a new one."""
    return (
        "<h2>This confirmation expired.</h2>"
        "<p>This confirmation expired. Send /allowgc again to start over.</p>"
    )


def allowgc_turned_off_view(chat_name: str, chat_id, token: str) -> tuple[str, object]:
    """Turned-off card: the same Allowgc + beta heading, badge on "off".

    The 7-day preservation line stays, with a divided italic hint and a
    green Turn on button that re-enables with all access on.
    """
    name = escape(str(chat_name or "this group"))
    ident = escape(str(chat_id))
    text = "\n".join([
        _allowgc_heading(),
        f"<p>Allowgc is {_allowgc_state_badge('off', False)} for <b>{name}</b> (<code>{ident}</code>).</p>",
        "<p>Everything is preserved for 7 days — send /allowgc again within 7 days "
        "to turn it back on without losing anything.</p>",
        "<hr />",
        "<p><i>Click turn on to enable it again.</i></p>",
    ])
    markup = inline_markup([[("Turn on", f"hm:allowgc:turnon:{token}", "success")]])
    return text, markup


def allowgc_imout_confirm_view(chat_id, chat_name: str | None = None) -> tuple[str, object]:
    """Ephemeral /imout opt-out confirmation (DM only, one-way)."""
    name = escape(str(chat_name or "this group"))
    ident = escape(str(chat_id))
    lines = [
        "<h2>Leave this group?</h2>",
        f"<p>You asked to leave Hermes in <b>{name}</b> (<code>{ident}</code>).</p>",
        "<p>This is one-way: once you leave, you leave. "
        "You will not be able to use Hermes in this group again unless the owner approves you again in a private chat.</p>",
    ]
    markup = inline_markup([[
        ("Confirm leaving", f"hm:imout:confirm:{ident}", "danger"),
        ("Cancel", f"hm:imout:cancel:{ident}", "primary"),
    ]])
    return "\n".join(lines), markup


def pending_start_rich_message(user: dict, *, bot_name: str | None = None):
    """Owner-private pending-start notice as native RichMessage blocks.

    Same content as :func:`pending_start_view`, but the Ban/Allow controls
    are a real ``PageBlockButtonRow`` of callback buttons instead of HTML
    text: the adapter's ``_send_rich_ui`` string branch would only carry
    them as unparsed ``<tg-button-row>`` markup, and the owner notice must
    arrive as buttons the owner can tap. The display name stays raw here —
    blocks carry no HTML to inject into — while the HTML view escapes it.
    Profile is the display name itself as a pill, exactly as /start
    builds it (``rich_button(name, kind="profile", value=user_id)``): a
    native ``TextButton`` with an ``InlineButtonTypeUserProfile`` carrying
    just the numeric id — never an ``InlineButtonTypeUrl`` to
    ``tg://user?id=<id>`` (that renders as a URL button with an
    external-link arrow, not a profile pill), never an href
    (``TextUrl``), never a separate "Profile" label, and never a URL button
    in the row. The ``Input*`` profile variant is not usable here: it needs
    a resolved ``InputUser`` with an access hash, and this builder has only
    the bare numeric id. The pill is omitted when the id is not a usable
    numeric id — the name then stays plain text — and the notice otherwise
    renders exactly as before.
    """
    from telethon import types

    user_id = str(user["id"])
    name = str(user.get("name") or user_id)
    bot = _bot_display_name(bot_name)
    buttons = [
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
    ]
    # The display name IS the profile pill, exactly as /start builds it:
    # rich_button(name, kind="profile", value=user_id). When the id is not
    # a usable numeric id there is no pill and the name stays plain text;
    # a missing name already fell back to the id above, and the parenthesised
    # id below renders exactly as before either way.
    name_node = (
        types.TextButton(
            text=types.TextPlain(text=name),
            type=types.InlineButtonTypeUserProfile(user_id=int(user_id)),
        )
        if user_id.isdecimal()
        else types.TextPlain(text=name)
    )
    first_texts = [
        name_node,
        types.TextPlain(text=" ("),
        types.TextFixed(text=types.TextPlain(text=user_id)),
        types.TextPlain(text=f") started {bot}."),
    ]
    return types.InputRichMessage(blocks=[
        types.PageBlockParagraph(text=types.TextConcat(texts=first_texts)),
        types.PageBlockParagraph(text=types.TextPlain(text="Actions you can take:")),
        types.PageBlockButtonRow(buttons=buttons),
    ])


def pending_start_view(user: dict, *, bot_name: str | None = None) -> tuple[str, None]:
    """Owner-private presentation only; buttons delegate to existing policy paths.

    A Profile button (``tg://user?id=<id>``) opens the starter's Telegram
    profile; it is omitted when the id is not a usable numeric id, and the
    view otherwise renders exactly as before.
    """
    user_id = str(user["id"])
    from .mtproto_rich import rich_button, rich_button_row
    buttons = [
        rich_button("Ban", kind="callback", value=f"hm:admission:ban:{user_id}", style="danger"),
        rich_button("Allow", kind="callback", value=f"hm:admission:allow:{user_id}", style="success"),
    ]
    if user_id.isdecimal():
        buttons.append(rich_button("Profile", kind="profile", value=user_id))
    actions = rich_button_row(buttons)
    return (
        f"<p><b>{escape(str(user.get('name') or user_id))}</b> "
        f"(<code>{escape(user_id)}</code>) started {escape(_bot_display_name(bot_name))}.</p>"
        "<p>Actions you can take:</p>"
        f"{actions}",
        None,
    )


def _granted_name_button(user: dict) -> str:
    """The granted user's name as their profile button when the id is numeric;
    bold plain text otherwise, exactly as before."""
    user_id = str(user.get("id") or "")
    if user_id.isdecimal():
        return rich_button(str(user.get("name") or user_id), kind="profile", value=user_id)
    return f"<b>{_name(user)}</b>"


def allow_success_view(user: dict, *, isolated: bool, chat_label: str, bot_name: str | None = None) -> str:
    """Explicit HTML paragraph boundaries prevent names becoming Markdown headings."""
    bot = _bot_display_name(bot_name)
    if isolated:
        description = f"can now message {bot} directly. Their conversations and workspace stay separate from yours."
    else:
        description = f"can now contact {bot} in {escape(chat_label)}. Access elsewhere is unchanged."
    return (
        "<h2>Access granted</h2>"
        f"<p>{_granted_name_button(user)} {description}</p>"
    )


def disallow_success_view(user: dict) -> str:
    """One revoked account, without a redundant one-page footer."""
    return (
        "<h2>Global access revoked</h2>"
        f"<p><b>{_name(user)}</b> no longer has global access to this profile.</p>"
        "<p>Group guest access, when enabled by that group's Allow all setting, is separate.</p>"
    )


def access_granted_view(user: dict, *, direct: bool, chat_label: str, bot_name: str | None = None) -> tuple[str, object]:
    """A future grant notice, never an invitation to bypass scoped authorization."""
    access = (
        "You've been added to this profile, so you can now message me directly."
        if direct else
        f"You've been given access to chat with me in {escape(chat_label)}. Access elsewhere is unchanged."
    )
    bullets = "".join(f"<li>{escape(text)}</li>" for text in CAPABILITIES)
    return (
        f"<p>Hey <b>{_name(user)}</b>! You're in.</p>"
        f"<p>{access} I'm {_bot_display_name(bot_name)}, your AI assistant.</p>"
        f"<ul>{bullets}</ul>"
        "<p>Bring a question, a half-formed idea, or something you'd like to get done. "
        "Send /help whenever you'd like a look around.</p>",
        inline_markup([[('Show me around', 'hm:help:menu:0', 'primary')]]) if direct else None,
    )
