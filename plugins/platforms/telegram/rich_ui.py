"""Rich interactive Telegram help, settings, learning and GuestBot views."""
from __future__ import annotations

import math
import re
from html import escape
from typing import Any

from .mtproto_rich import inline_markup, rich_button, rich_page_footer


PAGE_SIZE = 5
MCP_PAGE_SIZE = 8


def _plain(value: Any) -> str:
    text = " ".join(str(value or "").split())
    # Dynamic values are data, not authored Markdown.
    text = re.sub(r"[`*_]", "", text)
    return text.replace("[", "(").replace("]", ")")


def _table_cell(value: Any) -> str:
    """Escape data for a GitHub-flavoured Markdown table cell."""
    return _plain(value).replace("\\", "\\\\").replace("|", r"\|").replace("\n", " ")


def _code_cell(value: Any) -> str:
    """Escape command usage for inline code without changing its arguments."""
    text = " ".join(str(value or "").split()).replace("`", "")
    return text.replace("\\", "\\\\").replace("|", r"\|")


def rich_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Render a compact rich table with bold headers and first-column labels."""
    head = "| " + " | ".join(f"**{_table_cell(cell)}**" for cell in headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = []
    for row in rows:
        cells = [_table_cell(cell) for cell in row[:len(headers)]]
        cells.extend("" for _ in range(len(headers) - len(cells)))
        if cells:
            cells[0] = f"**{cells[0]}**"
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, divider, *body])


def rich_html_table(headers: tuple[str, ...], rows: list[tuple[str, str, str]]) -> str:
    """Render a native Rich HTML table; the third column carries raw button HTML.

    Labels and values are data and are escaped; the actions cell is authored
    HTML (``<tg-button>`` pills built with :func:`rich_button`) and is
    inserted verbatim.
    """
    head = "".join(f"<th>{escape(cell)}</th>" for cell in headers)
    body = []
    for label, value, actions in rows:
        body.append(
            f"<tr><td><b>{escape(label)}</b></td><td>{escape(value)}</td>"
            f"<td>{actions}</td></tr>"
        )
    return f"<table><tr>{head}</tr>{''.join(body)}</table>"


def settings_page_html(title: str, intro: str, rows: list[tuple[str, str, str]],
                       *, page: int = 1, pages: int = 1) -> str:
    """Render a settings page: heading, intro, button table, italic footer."""
    parts = [f"<h1>{escape(title)}</h1>"]
    if intro:
        parts.append(f"<p>{escape(intro)}</p>")
    parts.append(rich_html_table(("Setting", "Value", "Actions"), rows))
    parts.append("<hr />")
    parts.append(rich_page_footer(page, pages, len(rows)))
    return "".join(parts)


def _settings_pill(action: str, label: str, style: str | None = None) -> str:
    """One in-body callback pill for a settings table cell."""
    return rich_button(label, kind="callback", value=action, style=style)


def help_sections(*, skill_commands: dict | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return all gateway command categories and dynamically discovered skills."""
    from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates

    overrides = _resolve_config_gates()
    grouped: dict[str, list[tuple[str, str]]] = {}
    search_entries: list[tuple[str, str]] = []
    for command in COMMAND_REGISTRY:
        if not _is_gateway_available(command, overrides):
            continue
        args = f" {command.args_hint}" if command.args_hint else ""
        name = f"/{command.name}{args}"
        aliases = [f"/{alias}" for alias in command.aliases
                   if not (alias.replace("-", "_") == command.name.replace("-", "_") and alias != command.name)]
        description = _plain(command.description)
        if aliases:
            description += f" (aliases: {', '.join(aliases)})"
        if command.name in {"search", "deepresearch"}:
            search_entries.append((name, description))
        elif command.name == "thread":
            grouped.setdefault("Threads", []).append((name, description))
        else:
            grouped.setdefault(command.category, []).append((name, description))

    from hermes_cli.commands_platforms import TELEGRAM_CONTROL_COMMANDS
    for name, description, args_hint, category in TELEGRAM_CONTROL_COMMANDS:
        usage = f"/{name}" + (f" {args_hint}" if args_hint else "")
        grouped.setdefault(category, []).append((usage, description))

    grouped.setdefault("Search", []).extend(search_entries)

    from agent.skill_commands import get_skill_commands
    skills = get_skill_commands() if skill_commands is None else skill_commands
    if skills:
        grouped["Skills"] = [
            (str(name), _plain((info or {}).get("description", "")))
            for name, info in sorted(skills.items(), key=lambda pair: pair[0].casefold())
        ]
    return list(grouped.items())


def _page_count(entries: list[tuple[str, str]]) -> int:
    return max(1, math.ceil(len(entries) / PAGE_SIZE))


def _total_entries(sections: list[tuple[str, list[tuple[str, str]]]]) -> int:
    return sum(len(entries) for _, entries in sections)


def _entry_lines(entries: list[tuple[str, str]]) -> str:
    if not entries:
        return "<p>No commands are available in this section.</p>"
    lines = []
    for name, description in entries:
        line = f"<li><b><code>{escape(name)}</code></b>"
        if description:
            line += f" — {escape(description)}"
        lines.append(line + "</li>")
    return "<ul>" + "".join(lines) + "</ul>"


def help_view(view: str = "home", page: int = 0, *, skill_commands: dict | None = None) -> tuple[str, Any]:
    """Render Hermes Help as concise rich lists with section navigation."""
    sections = help_sections(skill_commands=skill_commands)
    category_sections = [(name, entries) for name, entries in sections if name != "Skills"]
    skill_entries = next((entries for name, entries in sections if name == "Skills"), [])

    if view in {"home", "overview"}:
        gateway_count = sum(len(entries) for _name, entries in category_sections)
        text = (
            "<h1>Hermes Help</h1>"
            "<p>Choose a section to find a command and see what it does.</p>"
            "<hr /><h2>Command catalog</h2><ul>"
            f"<li><b>Gateway commands:</b> {gateway_count}</li>"
            f"<li><b>Skill commands:</b> {len(skill_entries)}</li></ul>"
            f"<hr /><p><i>Page 1 of 1 • total {_total_entries(sections)}</i></p>"
        )
        return text, _help_keyboard(sections, "home", 0, 1, _total_entries(sections))

    if view == "skills":
        entries = skill_entries
        title = "Skills"
    else:
        try:
            category_index = int(view)
        except (TypeError, ValueError):
            return help_view("home", 0, skill_commands=skill_commands)
        if not category_sections:
            return help_view("home", 0, skill_commands=skill_commands)
        category_index = max(0, min(category_index, len(category_sections) - 1))
        title, entries = category_sections[category_index]

    pages = _page_count(entries)
    try:
        page = max(0, min(int(page), pages - 1))
    except (TypeError, ValueError):
        page = 0
    start = page * PAGE_SIZE
    page_entries = entries[start:start + PAGE_SIZE]
    text = (
        "<h1>Hermes Help</h1>"
        "<p>Send a command below, or choose another section.</p>"
        f"<hr /><h2>{escape(title)}</h2>{_entry_lines(page_entries)}"
        f"<hr /><p><i>Page {page + 1} of {pages} • total {len(entries)}</i></p>"
    )
    return text, _help_keyboard(sections, "skills" if view == "skills" else str(category_index),
                                page, pages, len(entries))


def _help_keyboard(sections: list[tuple[str, list[tuple[str, str]]]], view: str,
                   page: int, pages: int, total: int) -> Any:
    rows: list[list[tuple[str, str, str | None]]] = []
    category_indices = [(i, name) for i, (name, _items) in enumerate(sections)
                        if name not in {"Skills", "Threads"}]
    for offset in range(0, len(category_indices), 2):
        row = []
        for index, name in category_indices[offset:offset + 2]:
            count = len(sections[index][1])
            row.append((f"{name[:20]} · {count}", f"hm:help:{index}:0",
                        "primary" if view == str(index) else None))
        rows.append(row)

    threads_index = next((i for i, (name, _items) in enumerate(sections) if name == "Threads"), None)
    threads_label = "Threads" if threads_index is None else (
        f"Threads · {len(sections[threads_index][1])}"
    )
    skills_count = len(next((items for name, items in sections if name == "Skills"), []))
    skills_label = f"Skills · {skills_count}"
    threads_payload = f"hm:help:{threads_index}:0" if threads_index is not None else "hm:help:home:0"
    rows.append([
        (threads_label, threads_payload, "primary" if threads_index is not None and view == str(threads_index) else None),
        (skills_label, "hm:help:skills:0", "primary" if view == "skills" else None),
    ])
    rows.append([("Overview", "hm:help:home:0", "primary" if view in {"home", "overview"} else None)])
    rows.append([("Personal settings", f"hm:personal:open:{view}:{page}", None),
                 ("GuestBot settings", "hm:help:guest:0", None)])
    if pages > 1:
        navigation = []
        if page > 0:
            navigation.append((f"‹ {page}/{pages} · {total} total", f"hm:help:{view}:{page - 1}", "primary"))
        if page + 1 < pages:
            navigation.append((f"Next {page + 2}/{pages} · {total} total ›", f"hm:help:{view}:{page + 1}", "primary"))
        rows.append(navigation)
    return inline_markup(rows)


def guestbot_view(enabled: bool, *, note: str = "") -> tuple[str, Any]:
    state = "On" if enabled else "Off"
    lines = [
        "# GuestBot settings",
        f"**Hermes GuestBot:** {state}",
        "Authorized Telegram Guest Mode requests are processed through Hermes’ normal message handler.",
        "Telegram Guest Mode must also be enabled for the bot in BotFather; access checks remain in force.",
        "Guest conversations stay separate from regular group-chat threads.",
        "Use the buttons or `/guestbot on` and `/guestbot off`; changes apply to this profile.",
    ]
    if note:
        lines.extend(["", note])
    rows = [[("Turn on", "hm:guestbot:on", "success" if enabled else None),
             ("Turn off", "hm:guestbot:off", "danger" if enabled else "success")]]
    rows.append([("Back to help", "hm:help:home:0", "primary")])
    return "\n\n".join(lines), inline_markup(rows)


def personal_settings_view(settings: dict[str, Any], user_name: str, state_id: int) -> tuple[str, Any]:
    """Render one user's settings as Rich HTML with in-body buttons in the table.

    Row actions (edit/remove instructions, toggles, manage/change) are
    ``<tg-button>`` pills inside the table; the keyboard below keeps only
    navigation (learning, export/import, close). Callbacks are the same
    ``ps:<state_id>:<action>`` payloads the keyboard used.
    """
    model = _plain(settings.get("default_model")) or "Profile default"
    provider = _plain(settings.get("default_provider")) or "Profile default"
    general = "Set" if settings.get("general_instructions") else "Not set"
    search = "Set" if settings.get("search_instructions") else "Not set"
    mcp_mode = settings.get("mcp_mode", "inherit")
    mcp_count = len(settings.get("selected_mcp_servers") or [])
    skill_count = len(settings.get("selected_skills") or [])
    memory = bool(settings.get("memory_enabled", True))
    learning = bool(settings.get("self_learning_enabled", True))
    reactions = bool(settings.get("reactions_enabled", True))
    activity_trail = bool(settings.get("activity_trail_enabled", False))
    mcp_value = "Profile defaults" if mcp_mode == "inherit" else f"Personal selection · {mcp_count}"

    def action(name: str) -> str:
        return f"ps:{state_id}:{name}"

    def instruction_actions(name: str, is_set: bool) -> str:
        pills = [_settings_pill(action(f"edit-{name}"), "Edit")]
        if is_set:
            pills.append(_settings_pill(action(f"remove-{name}"), "Remove", "danger"))
        return " ".join(pills)

    def toggle(name: str, enabled: bool) -> str:
        return _settings_pill(
            action(f"{name}-toggle"),
            "Turn off" if enabled else "Turn on",
            "danger" if enabled else "success",
        )

    rows = [
        ("Name", _plain(user_name) or "Telegram name", ""),
        ("Auto-loaded skills", f"{skill_count} selected",
         _settings_pill(action("skills"), "Manage", "primary")),
        ("MCP servers", mcp_value, _settings_pill(action("mcp"), "Manage")),
        ("Default model", model, _settings_pill(action("model"), "Change")),
        ("Default provider", provider, ""),
        ("General instructions", general,
         instruction_actions("general", bool(settings.get("general_instructions")))),
        ("Search instructions", search,
         instruction_actions("search", bool(settings.get("search_instructions")))),
        ("Memory", "On" if memory else "Off", toggle("memory", memory)),
        ("Self-learning", "On" if learning else "Off", toggle("learning", learning)),
        ("Message reactions", "On" if reactions else "Off", toggle("reactions", reactions)),
        ("Turn activity trail", "On" if activity_trail else "Off", toggle("activity-trail", activity_trail)),
    ]
    text = settings_page_html(
        "Personal settings",
        "These preferences apply only to your Telegram account in this profile.",
        rows,
    )
    rows_markup = [
        [("Learning status", action("learning"), None), ("Learning overview", action("overview"), None)],
        [("Export your data", action("export"), None), ("Import data", action("import"), None)],
        [("Close", action("close"), "danger")],
    ]
    return text, inline_markup(rows_markup)


def self_learning_view(settings: dict[str, Any], state_id: int) -> tuple[str, Any]:
    learning = bool(settings.get("self_learning_enabled", True))
    memory = bool(settings.get("memory_enabled", True))
    skills = len(settings.get("personal_skills") or [])
    updated = _plain(settings.get("updated_at")) or "Not recorded"
    rows = [
        ("Self-learning", "On" if learning else "Off",
         _settings_pill(f"ps:{state_id}:learning-toggle",
                        "Turn off" if learning else "Turn on",
                        "danger" if learning else "success")),
        ("Memory access", "On" if memory else "Off", ""),
        ("Saved personal skills", str(skills), ""),
        ("Last preference update", updated, ""),
    ]
    text = settings_page_html(
        "Learning settings",
        "Personal lessons stay within this Telegram account's private memory.",
        rows,
    )
    return text, inline_markup([[("Back to settings", f"ps:{state_id}:home", "primary")]])


def learning_overview_view(settings: dict[str, Any], state_id: int) -> tuple[str, Any]:
    memory = bool(settings.get("memory_enabled", True))
    learning = bool(settings.get("self_learning_enabled", True))
    instruction_chars = len(settings.get("general_instructions", "")) + len(settings.get("search_instructions", ""))
    updated = _plain(settings.get("updated_at")) or "Not recorded"
    rows = [
        ("Personal self-learning", "On" if learning else "Off", ""),
        ("Personal memory", "On" if memory else "Off", ""),
        ("Selected skills", str(len(settings.get("selected_skills") or [])), ""),
        ("Selected MCP servers", str(len(settings.get("selected_mcp_servers") or [])), ""),
        ("Personal instruction text", f"{instruction_chars} characters", ""),
        ("Last preference update", updated, ""),
    ]
    text = settings_page_html(
        "Learning overview",
        "This overview shows only your personal settings.",
        rows,
    )
    return text, inline_markup([[("Back to settings", f"ps:{state_id}:home", "primary")]])


def mcp_servers_view(available: list[tuple[str, bool]], state_id: int, *, note: str = "", page: int = 0) -> tuple[str, Any]:
    lines = ["# Personal MCP servers", "__Private to your Telegram account.__"]
    pages = max(1, math.ceil(len(available) / MCP_PAGE_SIZE))
    try:
        page = max(0, min(int(page), pages - 1))
    except (TypeError, ValueError):
        page = 0
    start = page * MCP_PAGE_SIZE
    page_items = available[start:start + MCP_PAGE_SIZE]
    if available:
        table_rows = [(name, "Selected" if selected else "Not selected") for name, selected in page_items]
        lines.extend(["", rich_table(("Server", "Access"), table_rows)])
    else:
        lines.extend(["", "__No MCP servers are configured in this profile yet.__"])
    lines.extend(["", "__Selected servers may start a local process or contact a remote endpoint when used.__"])
    if note:
        lines.extend(["", note])
    rows = [[(_plain(name)[:22], f"ps:{state_id}:mcp-toggle:{start + index}",
             "success" if selected else None)] for index, (name, selected) in enumerate(page_items)]
    if pages > 1:
        navigation = []
        if page > 0:
            navigation.append(("‹ Previous", f"ps:{state_id}:mcp-page:{page - 1}", "primary"))
        if page + 1 < pages:
            navigation.append(("Next ›", f"ps:{state_id}:mcp-page:{page + 1}", "primary"))
        rows.append(navigation)
    lines.extend(["", f"__Page {page + 1} of {pages} • total {len(available)}__"])
    rows.append([("Add custom MCP", f"ps:{state_id}:mcp-add", "primary")])
    rows.append([("Use profile defaults", f"ps:{state_id}:mcp-inherit", None)])
    rows.append([("Back to settings", f"ps:{state_id}:home", "primary")])
    return "\n".join(lines), inline_markup(rows)


def export_menu_view(state_id: int) -> tuple[str, Any]:
    rows = [
        ("Personal preferences", "This Telegram account's settings; custom MCP credential values are redacted."),
        ("Profile credentials", "Credential files only; sensitive and sent as a private archive."),
        ("Full profile", "Files, sessions, and configuration; may include credentials and other users' data."),
    ]
    text = (
        "# Export your data\n\n"
        "Choose what to send to this private chat.\n\n"
        + rich_table(("Scope", "Includes"), rows)
        + "\n\n*Sensitive exports require a separate confirmation.*"
    )
    buttons = [
        [("Personal preferences", f"ps:{state_id}:export-preferences", None)],
        [("Profile credentials", f"ps:{state_id}:export-credentials", "danger")],
        [("Full profile", f"ps:{state_id}:export-all", "danger")],
        [("Cancel", f"ps:{state_id}:home", "danger")],
    ]
    return text, inline_markup(buttons)


def export_confirm_view(state_id: int, scope: str) -> tuple[str, Any]:
    details = {
        "preferences": "Only your personal settings, including custom MCP details with credential values redacted. No keys, Telegram session, or other users' data.",
        "credentials": "Profile credential values, including provider/API secrets. The archive contains plaintext secrets and will be sent to this private chat.",
        "all": "The full active-profile backup. It may include credentials, Telegram session data, messages, and other users' data.",
    }
    labels = {"preferences": "Personal preferences", "credentials": "Profile credentials", "all": "Full profile"}
    text = (
        "# Confirm export\n\n"
        + rich_table(("Scope", "What will be sent"), [(labels[scope], details[scope])])
        + "\n\n*Continue only if you trust this Telegram account and chat.*"
    )
    confirm_style = "success" if scope == "preferences" else "danger"
    rows = [[("Confirm export", f"ps:{state_id}:export-confirm:{scope}", confirm_style),
             ("Cancel", f"ps:{state_id}:export", "danger")]]
    return text, inline_markup(rows)


def import_prompt_view(state_id: int) -> tuple[str, Any]:
    rows = [
        ("Preferences JSON", "Updates only this Telegram account's personal settings."),
        ("Hermes backup ZIP", "Restores files into the active profile."),
    ]
    text = (
        "# Import data\n\n"
        + rich_table(("File", "Effect"), rows)
        + "\n\n*Hermes previews the exact changes before anything is applied.*"
    )
    return text, inline_markup([
        [("Cancel", f"ps:{state_id}:import-cancel", "danger"),
         ("Back to settings", f"ps:{state_id}:home", "primary")],
    ])


def confirmation_view(state_id: int, title: str, details: str, *, confirm_label: str = "Confirm") -> tuple[str, Any]:
    text = f"# {title}\n\n{details}\n\n*No change will be made unless you confirm.*"
    return text, inline_markup([[(confirm_label, f"ps:{state_id}:confirm", "danger"),
                                 ("Cancel", f"ps:{state_id}:home", "danger")]])
