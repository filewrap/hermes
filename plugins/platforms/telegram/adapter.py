"""Telegram plugin registration and configuration hooks.

All Telegram gateway and standalone delivery uses the native Telethon/MTProto
adapter. This module deliberately contains no Bot API client implementation.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from hermes_cli import setup_platforms

logger = logging.getLogger(__name__)


def telegram_deps_present() -> bool:
    """Passive dependency probe; never installs packages during status/config reads."""
    try:
        import telethon
        import python_socks
        return True
    except ImportError:
        return False


def check_telegram_requirements() -> bool:
    """Lazily install the native MTProto client and its proxy support if needed."""
    if telegram_deps_present():
        return True
    try:
        from tools.lazy_deps import ensure
        ensure("platform.telegram", prompt=False)
    except Exception:
        return False
    return telegram_deps_present()


def _resolve_notifications_mode() -> str:
    """Return the validated Telegram notification preference."""
    mode = os.getenv("HERMES_TELEGRAM_NOTIFICATIONS", "")
    if not mode:
        try:
            from gateway.config import load_gateway_config
            from gateway.run import cfg_get
            value = cfg_get(load_gateway_config(), "display", "platforms", "telegram", "notifications")
            if value not in {None, ""}:
                mode = str(value).strip().lower()
        except Exception:
            pass
    mode = mode or "important"
    if mode not in {"all", "important"}:
        logger.warning("Unknown telegram notifications mode %r; using 'important'", mode)
        return "important"
    return mode


def _build_adapter(config):
    """Construct the Telethon adapter and apply notification preferences."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    adapter = TelethonAdapter(config)
    # Each adapter owns its own profile-scoped controller and background worker.
    # Live MTProto checks, not persisted verification flags, enable the service.
    try:
        from .forum_audit_transport import install as install_forum_audit
        from hermes_constants import get_hermes_home
        adapter._forum_audit_transport = install_forum_audit(adapter, get_hermes_home())
    except Exception:
        logger.warning("Telegram forum audit transport could not be initialized", exc_info=True)
    try:
        adapter._notifications_mode = _resolve_notifications_mode()
    except Exception:
        adapter._notifications_mode = "important"
    return adapter


def _is_connected(config) -> bool:
    """Require all credentials needed to create a bot-authenticated MTProto session."""
    from agent.secret_scope import get_secret
    token = getattr(config, "token", None)
    if not token:
        import hermes_cli.gateway as gateway_mod
        token = gateway_mod.get_env_value("TELEGRAM_BOT_TOKEN") or ""
    api_id = get_secret("TELEGRAM_API_ID", "")
    api_hash = get_secret("TELEGRAM_API_HASH", "")
    try:
        valid_id = int(api_id) > 0
    except (TypeError, ValueError):
        valid_id = False
    return valid_id and bool(str(api_hash).strip()) and bool(str(token).strip())


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Deliver through the profile-scoped Telethon standalone sender."""
    token = getattr(pconfig, "token", None)
    if not token:
        from agent.secret_scope import get_secret
        token = get_secret("TELEGRAM_BOT_TOKEN", "") or ""
    extras = getattr(pconfig, "extra", {}) or {}
    from tools.send_message_senders import _send_telegram
    return await _send_telegram(
        token, chat_id, message, media_files=media_files, thread_id=thread_id,
        disable_link_previews=bool(extras.get("disable_link_previews")),
        force_document=force_document,
    )


def interactive_setup() -> None:
    """Delegate Telegram credential setup to the CLI setup wizard."""
    setup_platforms._setup_telegram()


def _apply_yaml_config(yaml_cfg: dict, telegram_cfg: dict) -> dict | None:
    """Bridge supported Telegram YAML options into scoped env and adapter extras.

    Explicit environment values take precedence over YAML. The returned mapping
    contains only options consumed by the Telethon adapter or gateway contract.
    """
    from gateway.platforms._shared import yaml_env_setter

    extras: dict[str, Any] = {}
    set_env = yaml_env_setter()

    def bridge(key: str, env: str, *, lowercase: bool = False, seed: bool = True) -> None:
        if key not in telegram_cfg or telegram_cfg[key] is None:
            return
        value = telegram_cfg[key]
        if seed:
            extras.setdefault(key, value)
        set_env(env, str(value).lower() if lowercase else value)

    require_mention = telegram_cfg.get("require_mention", yaml_cfg.get("require_mention"))
    if require_mention is not None:
        set_env("TELEGRAM_REQUIRE_MENTION", str(require_mention).lower())
    bridge("guest_mode", "TELEGRAM_GUEST_MODE", lowercase=True)
    bridge("free_response_chats", "TELEGRAM_FREE_RESPONSE_CHATS", seed=True)
    bridge("free_response_topics", "TELEGRAM_FREE_RESPONSE_TOPICS", seed=True)
    bridge("allowed_chats", "TELEGRAM_ALLOWED_CHATS", seed=False)
    bridge("allowed_topics", "TELEGRAM_ALLOWED_TOPICS", seed=False)
    bridge("reactions", "TELEGRAM_REACTIONS", lowercase=True)
    if "proxy_url" in telegram_cfg:
        value = str(telegram_cfg["proxy_url"] or "").strip()
        extras.setdefault("proxy_url", value)
        set_env("TELEGRAM_PROXY", value)

    nested = telegram_cfg.get("extra") if isinstance(telegram_cfg.get("extra"), dict) else {}
    reply_mode = telegram_cfg.get("reply_to_mode", nested.get("reply_to_mode"))
    if reply_mode is not None:
        set_env("TELEGRAM_REPLY_TO_MODE", "off" if reply_mode is False else str(reply_mode).lower())

    for key, env in (("allow_from", "TELEGRAM_ALLOWED_USERS"),
                     ("group_allow_from", "TELEGRAM_GROUP_ALLOWED_USERS"),
                     ("group_allowed_chats", "TELEGRAM_GROUP_ALLOWED_CHATS")):
        value = telegram_cfg.get(key, nested.get(key))
        if value is not None:
            set_env(env, value)
    for key in ("guest_mode", "disable_link_previews", "free_response_topics"):
        if key in telegram_cfg:
            extras.setdefault(key, telegram_cfg[key])

    for _topic_key in ("disable_topic_auto_rename", "dm_topic_auto_transfer"):
        if _topic_key in telegram_cfg:
            extras.setdefault(_topic_key, telegram_cfg[_topic_key])

    generic_keys = {
        "reply_prefix", "reply_in_thread", "reply_to_mode", "unauthorized_dm_behavior", "notice_delivery",
        "require_mention", "channel_skill_bindings", "channel_prompts", "gateway_restart_notification",
        "allow_from", "allow_admin_from", "dm_policy", "group_policy", "api_id", "api_hash", "bot_token",
    }
    retired_options = {
        "allow_bots", "allow_cjk_rich_messages", "base_file_url", "base_url", "bots_require_mention",
        "dm_topics", "exclusive_bot_mentions", "fallback_ips", "group_topics",
        "ignored_threads", "local_mode", "mention_patterns", "observe_unmentioned_group_messages",
        "rich_drafts", "status_indicator", "status_offline", "status_online", "webhook_url", "webhook_secret",
    }
    for key in retired_options.intersection(telegram_cfg.keys() | nested.keys()):
        logger.warning("Unsupported Telegram option %s is ignored; only native MTProto is available", key)
    for key, value in nested.items():
        if key not in generic_keys and key not in retired_options and key != "rich_messages":
            extras.setdefault(key, value)
    if "rich_messages" in telegram_cfg or "rich_messages" in nested:
        logger.info("Telegram uses native MTProto rich messages by default; rich_messages is ignored")
    return extras or None


# The Telegram platform hint (native MTProto Rich Messages via Telethon) lives on
# the plugin's PlatformEntry (see register() below) — it is the single source of
# truth; core hint tables (agent/prompt_builder.PLATFORM_HINTS) hold no telegram row.
from gateway.telegram_instructions import TELEGRAM_OPERATIONS_INSTRUCTIONS

_MEDIA_NATIVE = (
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
)

PLATFORM_HINT = (
        TELEGRAM_OPERATIONS_INSTRUCTIONS + " " +
        "You are on Telegram. All text replies are delivered as native MTProto Rich Messages. "
        "Use finished, semantically structured Rich Messages: real paragraphs, headings, lists, quotes, tables, code, and math blocks rather than visual spacing hacks. Choose standard Markdown (the default) or one complete Telegram Rich HTML message; never mix them. "
        "Real supported HTML tags outside code select Telethon's `InputRichMessageHTML`; otherwise Hermes sends "
        "`InputRichMessageMarkdown`. Use blank lines between distinct blocks. Indent nested Markdown list items by exactly two spaces per level, indent continuation lines beneath their item's text, and never use tabs; finish every list item, heading, quote, table, code fence, formula delimiter, and HTML tag cleanly. Do not leave dangling headings, open fences, partial tables, or incomplete sentences. Use Markdown for headings, bold/italic, lists, quotes, code, links, and task "
        "checklists (`- [ ]` / `- [x]`). Markdown pipe tables become readable labeled rows; when a true table or "
        "collapsible section improves clarity, use Telegram Rich HTML such as `<table>`, "
        "`<details><summary>…</summary>…</details>`, headings, and checkbox inputs. Use only Telegram's supported "
        "Rich HTML subset: CSS, classes, `<div>` layout, and arbitrary web HTML are not supported. Keep code samples "
        "fenced so literal HTML inside them remains literal, and do not mix HTML and Markdown syntax in one message. "
        "Math is supported natively: use `$...$` or `\\(...\\)` for inline TeX, and `$$...$$`, `\\[...\\]`, or a dedicated ```math / ```latex / ```tex fence for display equations. Use raw TeX inside math delimiters, not an ordinary code fence; Telegram supports a TeX subset, so avoid unsupported macros and explain any rendering limitation. Keep equations as their own paragraph/block when useful. "
        "For Search or Deep Research in a Telegram forum, keep every progress message, clarification, follow-up, and final response in the active topic; a follow-up in that topic continues the same research thread. "
        "For rich content, choose the best format for each message: native Rich Markdown, supported Rich HTML, or blocks. "
        "When asking the user to choose one of a few options, write a short 'Choose one:' prompt followed by a "
        "Markdown table whose first column is `Option` and second column is `Description`; Telegram will show "
        "native inline choice buttons for that pattern. Telegram has no true dropdown widget, so use these "
        "selectable inline buttons rather than a fake text menu. Inline Markdown code is rendered as a native copy action, "
        "and each fenced code block receives a compact native Copy PageButton; use Markdown fences instead of HTML `<pre>` "
        "when copy is needed. Markdown and supported HTML hrefs remain native clickable links. For separate one-way rich "
        "URL/copy/profile buttons, use `telegram_client(action='rich_message_send', ...)` only when a separate message is "
        "needed; stay in the active chat/topic, use Markdown/blocks for PageButtons, and never invent callbacks or targets. "
        "A successful tool call has already sent the body, so do not repeat that message in the final reply. "
        "Do not claim that an inline button/action exists unless the Telegram UI supplied it; help and GuestBot controls "
        "use native colored callback buttons. "
        f"{_MEDIA_NATIVE}Images (.png, .jpg, .webp) send as photos, videos (.mp4) play inline; image URLs via ![alt](url) send as "
        "photos. Audio: add [[audio_as_voice]] on its own line to send ANY audio file as a native voice bubble "
        "(non-Opus transcodes automatically); without it, .mp3/.m4a arrive as audio files, other formats as documents."
    )


def register(ctx) -> None:
    """Register the Telegram platform plugin and its gateway integration hooks."""
    from .dashboard_card import DASHBOARD_CARD
    from . import gateway_env  # env -> GatewayConfig bridge (gateway.config_env)
    from .telegram_ids import parse_send_target
    ctx.register_platform(
        name="telegram", label="Telegram (MTProto)", adapter_factory=_build_adapter,
        check_fn=telegram_deps_present, ensure_deps_fn=check_telegram_requirements,
        is_connected=_is_connected,
        required_env=["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"],
        install_hint="Run `hermes gateway setup` to install Telegram support.",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="TELEGRAM_ALLOWED_USERS", allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        allow_bots_env="TELEGRAM_ALLOW_BOTS",
        group_allowed_chats_env="TELEGRAM_GROUP_ALLOWED_CHATS",
        allowlist_suppresses_pairing=True,
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL", standalone_sender_fn=_standalone_send,
        max_message_length=4096, emoji="✈️", allow_update_command=True,
        platform_hint=PLATFORM_HINT,
        **DASHBOARD_CARD,
        display_tier="high",
        # Mobile inbox: quiet tool_progress / busy-ack, but keep interim commentary and heartbeats.
        display_overrides={"tool_progress": "off", "busy_ack_detail": False},
        target_ref_syntax_fn=parse_send_target,
        env_overrides=gateway_env.ENV_OVERRIDES,
        env_enable_credentials=gateway_env.ENV_ENABLE_CREDENTIALS,
    )

# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import threading


_PLUGIN_COMPAT_LAZY = {
    'atomic_replace': ('utils', 'atomic_replace'),
    'cache_document_from_bytes': ('gateway.platforms.base', 'cache_document_from_bytes'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
