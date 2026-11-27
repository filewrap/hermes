"""Pure text helpers for the Telegram provider-management UI.

Moved verbatim from ``provider_management_ui.py`` so that file stays within its
line budget: page footers, hints, notice splitting, credential status labels,
provider/model labels and catalog text. No Telegram client state lives here —
only strings and rich-text nodes.

The rate-limit badge helpers live here too: the pool-cooldown read, the
small red "limited" badge, and its fresh toast. They read the credential
pool through the published ``agent.credential_pool`` surface only, so the
badge never invents a limit the pool does not report.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from html import escape
from typing import Any, Callable

from plugins.platforms.telegram.mtproto_rich import input_rich_message, rich_button

logger = logging.getLogger(__name__)

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")

def _page_footer(page: int, pages: int, total: int) -> str:
    """Consistent page/total footer for Telegram catalogue cards."""
    return f"Page {int(page) + 1} of {int(pages)} • total {int(total)}"

def _provider_hints_block() -> str:
    """Hint footer for the providers card: one short line, what to do next."""
    return "<p><i>Hint: Use /model to configure model settings.</i></p>"

def _plain_rich_text(value: Any) -> str:
    """Flatten a native RichText node for locating a provider-status list item."""
    if isinstance(value, str):
        return value
    texts = getattr(value, "texts", None)
    if texts is not None:
        return "".join(_plain_rich_text(item) for item in texts)
    if hasattr(value, "text"):
        return _plain_rich_text(value.text)
    return ""

def _notice_segments(notice: str, limit: int = 240) -> list[tuple[str, bool]]:
    """Split a notice on `code` spans, truncated to ``limit`` plain chars.

    Backtick spans render monospace in both notice paths (TextFixed in
    blocks, <code> in HTML). A lone backtick — or one cut by truncation —
    stays literal text, and a span cut by the budget stays closed.
    """
    raw = str(notice or "").replace("\n", " ")
    segments: list[tuple[str, bool]] = []
    cursor = 0
    for match in _CODE_SPAN_RE.finditer(raw):
        if match.start() > cursor:
            segments.append((raw[cursor:match.start()], False))
        segments.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(raw):
        segments.append((raw[cursor:], False))
    if not segments:
        return [("", False)]
    kept: list[tuple[str, bool]] = []
    remaining = limit
    for segment, is_code in segments:
        if remaining <= 0:
            break
        if len(segment) <= remaining:
            kept.append((segment, is_code))
            remaining -= len(segment)
        else:
            kept.append((segment[:remaining], is_code))
            remaining = 0
    if sum(len(segment) for segment, _ in segments) > limit and kept:
        text, is_code = kept[-1]
        kept[-1] = (text + "…", is_code)
    return kept

def _notice_insert_at(blocks: list[Any]) -> int:
    """Where the notice block goes: above the divider that precedes the
    page footer — never between the divider and the page indexation."""
    from telethon import types

    insert_at = len(blocks)
    if blocks and _plain_rich_text(blocks[-1]).startswith("Page "):
        insert_at -= 1
        if insert_at > 0 and isinstance(blocks[insert_at - 1], types.PageBlockDivider):
            insert_at -= 1
    return insert_at

def _credential_status(provider: dict[str, Any]) -> str:
    if provider.get("credential_load_error") and not provider.get("ready"):
        return "Status unavailable"
    return "Added" if provider.get("ready") else "Missing"

def _mask_credential_hint(value: Any) -> str:
    """Masked preview of one stored credential: first 3 + last 3 characters.

    Enough to tell two credentials apart (which one is expired, which still
    works) without ever exposing the secret itself. Too short to mask safely
    means no preview at all.
    """
    text = str(value or "").strip()
    if len(text) >= 7:
        return f"{text[:3]}…{text[-3:]}"
    if len(text) >= 4:
        return f"{text[:2]}…{text[-2:]}"
    return ""

def _credential_type_label(value: Any) -> str:
    raw = str(value or "credential").strip().casefold().replace("-", "_")
    return {
        "oauth": "OAuth",
        "api_key": "API key",
        "apikey": "API key",
        "device_code": "Device sign-in",
        "external_process": "Host sign-in",
    }.get(raw, raw.replace("_", " ").title()[:24] or "Credential")

def _is_free_model(model_id: Any) -> bool:
    """A model id is free when it carries a free marker: :free, -free or _free."""
    lowered = str(model_id or "").casefold()
    return any(marker in lowered for marker in (":free", "-free", "_free"))


def _model_display_label(model_id: Any, limit: int = 24) -> str:
    """Button label for a model id: vendor prefix dropped, distinguishing tail kept.

    "anthropic/claude-fable-5.2" -> "claude-fable-5.2", so siblings that share
    a vendor prefix stay distinguishable. Longer than `limit`: front-truncated
    so the version at the end stays visible. The full id stays in the
    callback payload — only the displayed label changes.
    """
    text = str(model_id or "").strip()
    if "/" in text:
        text = text.split("/", 1)[1]
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return "…" + text[-(limit - 1):]


def _model_display_label_full(model_id: Any, limit: int = 32) -> str:
    """Full model id truncated from the front, so the distinguishing tail stays visible.

    Used when two vendor-dropped labels collide on the same page: the vendor
    part is where the models differ, so the head is dropped and the tail —
    the vendor's distinguishing end plus the model name — is kept.
    """
    text = str(model_id or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return "…" + text[-(limit - 1):]


def _provider_status_button(provider: dict, state_id: int, label: str | None = None,
                            *, available: bool | None = None) -> Any:
    """Return an actionable native RichMessage status pill."""
    from telethon import types

    status = label or _credential_status(provider)
    style = (types.RichButtonStyle(bg_success=True) if status == "Added" else
             types.RichButtonStyle(bg_danger=True) if status == "Missing" else
             types.RichButtonStyle(link=True))
    return types.TextButton(
        text=types.TextPlain(text=status),
        type=types.InlineButtonTypeCallback(data=f"pv:{int(state_id)}:credential-status".encode()),
        style=style,
    )


def _provider_detail_rich_message(markdown: str, provider: dict, state_id: int,
                                  credential_count: int) -> Any:
    """Render status as plain RichMessage text; actionable controls stay in the callback keyboard."""
    from telethon import types

    rich = input_rich_message(markdown, rich_format="blocks")
    if not isinstance(rich, types.InputRichMessage):
        raise ValueError("Provider detail status controls require native rich-message blocks")
    credential_status = _credential_status(provider)
    inventory_available = not bool(provider.get("credential_load_error"))
    if inventory_available:
        inventory = f"{credential_count} cred" if credential_count == 1 else f"{credential_count} creds"
    else:
        inventory = "Inventory unavailable"
    status_text = types.TextConcat(texts=[
        types.TextBold(text=types.TextPlain(text="Credential status: ")),
        _provider_status_button(provider, state_id, credential_status),
        types.TextPlain(text=f" · {inventory}"),
    ])
    if not provider.get("enabled", True):
        status_text.texts.append(types.TextPlain(text=" · Disabled in profile configuration"))

    badge = _limited_badge(str(provider.get("slug") or ""))
    if badge is not None:
        label, callback, _style = badge
        button = types.TextButton(
            text=types.TextPlain(text=label),
            type=types.InlineButtonTypeCallback(data=callback.encode()),
            style=types.RichButtonStyle(bg_danger=True),
        )
        # Right after the provider name: the title heading carries the badge.
        for block in rich.blocks:
            if isinstance(block, types.PageBlockHeading1):
                text = block.text
                texts = list(text.texts) if isinstance(text, types.TextConcat) else [text]
                block.text = types.TextConcat(texts=[*texts, types.TextPlain(text=" "), button])
                break
        else:
            # No heading to hang it on: the Credential status line carries it.
            status_text.texts.append(types.TextPlain(text=" · "))
            status_text.texts.append(button)

    for block_index, block in enumerate(rich.blocks):
        if not isinstance(block, types.PageBlockList):
            continue
        items = list(block.items)
        for item_index, item in enumerate(items):
            if (isinstance(item, types.PageListItemText)
                    and _plain_rich_text(item.text).startswith("Credential status:")):
                items[item_index] = types.PageListItemText(
                    text=status_text,
                    checkbox=getattr(item, "checkbox", None),
                    checked=getattr(item, "checked", None),
                )
                rich.blocks[block_index] = types.PageBlockList(items=items)
                return rich
    raise ValueError("Provider detail status line was not found in its rich-message list")


def _parse_reset(value: Any) -> float | None:
    """Epoch seconds from a pool reset/hit stamp (epoch float or ISO string)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def provider_cooldown_state(slug: str) -> dict[str, Any] | None:
    """Rate-limit state for a provider's whole credential pool, or None when usable.

    A provider is limited only when it holds at least one credential and EVERY
    credential is exhausted (``last_status == "exhausted"``) with its cooldown
    still binding. One healthy credential means the pool still routes, so the
    provider is not limited. A credential whose cooldown has elapsed is ready
    to retry and counts as healthy. Per-model cooldowns do not limit a
    provider: the other models on the same credential still serve.

    "Still binding" is the pool's own cooldown decision
    (``_exhausted_until``): the provider-supplied absolute reset when
    present, else ``last_status_at`` + the pool's TTL for the failure. So a
    cooldown with no reset time still expires on its own instead of showing
    "limited" forever. The DISPLAYED reset stays provider-supplied only:
    ``reset_at`` is the earliest known absolute reset among the exhausted
    credentials — the moment the first credential frees up — or None when
    the provider gave no reset time, so an unknown reset is never guessed.

    Returns ``{"reset_at": <epoch|None>, "hit_at": <epoch|None>}`` when limited:
    ``hit_at`` is the earliest exhaustion stamp.

    Any failure reading the pool fails open to None (no badge, never a crash).
    """
    from agent.credential_pool import (
        STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until, load_pool,
    )

    slug = (slug or "").strip().lower()
    if not slug:
        return None
    try:
        entries = load_pool(slug).entries()
    except Exception:
        return None
    if not entries:
        return None
    now = time.time()
    sole_credential = (
        sum(1 for e in entries if str(getattr(e, "last_status", "") or "") != STATUS_DEAD) <= 1
    )
    resets: list[float] = []
    hits: list[float] = []
    for entry in entries:
        if str(getattr(entry, "last_status", "") or "") != STATUS_EXHAUSTED:
            return None
        if (until := _exhausted_until(entry, sole_credential=sole_credential)) is not None and until <= now:
            return None
        reset_at = _parse_reset(getattr(entry, "last_error_reset_at", None))
        if reset_at is not None:
            resets.append(reset_at)
        hit_at = _parse_reset(getattr(entry, "last_status_at", None))
        if hit_at is not None:
            hits.append(hit_at)
    return {"reset_at": min(resets) if resets else None,
            "hit_at": min(hits) if hits else None}


def _short_wait(seconds: float) -> str:
    """Ceilinged short wait: 300 -> '5m', 3600 -> '1h', 3720 -> '1h 2m'."""
    total_minutes = max(0, -(-int(seconds) // 60))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _short_ago(seconds: float) -> str:
    """Floored elapsed time: 30 -> 'just now', 3600 -> '1h ago', 3660 -> '1h 1m ago'."""
    total_minutes = max(0, int(seconds) // 60)
    if total_minutes == 0:
        return "just now"
    if total_minutes < 60:
        return f"{total_minutes}m ago"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago" if minutes else f"{hours}h ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago" if hours else f"{days}d ago"


def _limited_badge(slug: str) -> tuple[str, str, str] | None:
    """The small red 'limited' badge for a rate-limited provider, or None.

    Label carries the remaining wait when the reset is known ('limited · 5m');
    otherwise just 'limited'. The callback value carries the slug so the tap
    can answer a fresh toast; a slug too long for Telegram's 64-byte callback
    limit falls back to the bare kind (static toast).
    """
    state = provider_cooldown_state(slug)
    if state is None:
        return None
    label = "limited"
    reset_at = state.get("reset_at")
    if reset_at:
        label = f"limited · {_short_wait(float(reset_at) - time.time())}"
    callback = f"badge:limited:{slug}"
    if len(callback.encode("utf-8")) > 64:
        callback = "badge:limited"
    return (label, callback, "danger")


def limited_provider_toast(slug: str) -> str | None:
    """Fresh toast for a limited-provider badge tap, or None for the static one.

    Computed on tap so the reset wait never goes stale: names when the limit
    was hit and when it lifts, both as relative durations (no timezone).
    None when the provider is no longer limited or its state cannot be read.
    """
    state = provider_cooldown_state(slug)
    if state is None:
        return None
    now = time.time()
    hit_at = state.get("hit_at")
    reset_at = state.get("reset_at")
    if hit_at and reset_at:
        return (f"Rate limit hit {_short_ago(now - float(hit_at))} · "
                f"resets in {_short_wait(float(reset_at) - now)}.")
    if reset_at:
        return f"Rate-limited · resets in {_short_wait(float(reset_at) - now)}."
    if hit_at:
        return f"Rate limit hit {_short_ago(now - float(hit_at))} · reset time unknown."
    return None
def _provider_button_label(item: dict, selected: bool = False) -> str:
    marker = "Current · " if item.get("is_current") else ("Selected · " if selected else "")
    return (f"{marker}{item['name'][:20]} · {item['total_models'] or len(item['models'])}"
            f" · {_credential_status(item)}")


def _model_capabilities_text(provider: str, model: str) -> str:
    """One compact line for a model: capabilities + context window, or "".

    Cached models.dev data only (``allow_network=False``) — never a live
    fetch for a card render. An unknown model yields "" so the line is
    simply omitted instead of showing a placeholder.
    """
    from agent.models_dev import get_model_info

    try:
        info = get_model_info(str(provider or ""), str(model or ""), allow_network=False)
    except Exception:
        # A broken registry must never break the card render: log it and
        # omit the line instead of showing a placeholder.
        logger.exception("Could not read model capabilities for %s/%s", provider, model)
        return ""
    if info is None:
        return ""
    parts = [info.format_capabilities()]
    context = info.format_context_window()
    if context:
        parts.append(f"{context} context")
    return " · ".join(parts)


def _current_route_buttons(
    short: Callable[[Any, int], str], state: dict,
) -> tuple[str, str]:
    """Current-route values as rich copy buttons: the provider (full slug)
    and the model — copying text has no regular-button equivalent."""
    provider = short(state["current_provider"], 48) or "unknown"
    model = short(state["current_model"], 72) or "unknown"
    full_provider = str(state["current_provider"] or "unknown")
    if full_provider and len(full_provider) <= 256:
        provider_button = rich_button(provider, kind="copy", value=full_provider, style="success")
    else:
        provider_button = f"<b>{escape(provider)}</b>"
    full_model = str(state["current_model"] or "unknown")
    if full_model and len(full_model) <= 256:
        model_button = rich_button(model, kind="copy", value=full_model, style="success")
    else:
        model_button = f"<b>{escape(model)}</b>"
    return provider_button, model_button


def _setup_recipe(
    supports_oauth: Callable[[dict], bool],
    supports_api_key: Callable[[dict], bool],
    provider: dict,
) -> tuple[str, list[str]]:
    """Provider-specific setup description, paired with concrete buttons in the UI."""
    slug = str(provider.get("slug") or "").strip()
    auth_type = str(provider.get("auth_type") or "api_key").casefold()
    if slug == "custom":
        return "Custom OpenAI-compatible endpoint", [
            "Create a named endpoint by entering its URL and optional key; Hermes fetches the live model list for selection.",
            "API keys are saved to Hermes' credential pool, not config.yaml.",
        ]
    if auth_type == "virtual" or slug == "moa":
        return "Virtual routing mode", [
            "No credential is required. Select a configured provider and model first.",
        ]
    if slug == "lmstudio":
        return "Local LM Studio server", [
            "Start the LM Studio local server; Hermes uses its local model catalog.",
            "An API key is optional and the endpoint URL belongs in provider configuration.",
        ]
    if slug == "actual":
        return "Actual hosted relay or local daemon", [
            "Hosted relay uses ACTUAL_API_KEY; local daemons may not require a key.",
            "Use the secure key button here; endpoint settings remain profile configuration.",
        ]
    if auth_type == "aws_sdk":
        return "AWS Bedrock credential chain", [
            "Hermes uses the host's AWS profile, environment credentials or attached role; no key is pasted here.",
            "This card can show local presence but does not run a billable model request.",
        ]
    if auth_type == "vertex":
        return "Google Vertex AI / ADC", [
            "Hermes uses Application Default Credentials and the project/region configured on the host.",
            "This card does not request or expose a service-account key.",
        ]
    if auth_type == "external_process":
        return "Local ACP process", [
            "Hermes launches the provider's local ACP process; this card reports setup state without exposing its credentials.",
        ]
    if slug == "copilot":
        return "GitHub Copilot sign-in or token", [
            "Use an existing GitHub CLI/Copilot login on the Hermes host or add a token using the secure key prompt.",
        ]
    if slug == "qwen-oauth":
        return "Import Qwen Code login", [
            "Hermes can import a Qwen Code login already present on this host; it does not implement a separate Qwen browser flow.",
        ]
    if supports_oauth(provider):
        text = "Start the provider's existing Hermes OAuth/device flow here; approval returns directly to this private chat."
        if supports_api_key(provider):
            text += " An API key may also be added as a separate credential."
        return "Hermes OAuth / device sign-in", [text]
    if supports_api_key(provider):
        instructions = ["Add or rotate an API key here; Hermes stores it in the profile's secure auth credential pool."]
        if provider.get("base_url_env_var"):
            instructions.append(f"Optional endpoint override: `{provider['base_url_env_var']}` is profile configuration, not a secret.")
        return "API key", instructions
    return "Host-native credentials", [
        "This provider uses the host's configured credential chain. Hermes does not expose a safe manual token-add route for it.",
    ]


def _catalog_freshness_text(provider: dict) -> str | None:
    """Freshness line for a provider detail page, or None when nothing is known.

    The refresh is what makes the list fresh: show when the cached list was
    last written ("Updated 2h ago"), and no line at all when no timestamp is
    known — never an unverifiable freshness claim.
    """
    if provider.get("catalog_refreshed"):
        return "Live refresh completed"
    if provider.get("catalog_status") == "unavailable":
        return "No local catalog available; live refresh may be needed"
    if provider.get("catalog_status") == "not_configured":
        return "Provider not configured"
    updated_at = provider.get("catalog_updated_at")
    if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
        return f"Updated {_short_ago(time.time() - float(updated_at))}"
    return None


def _models_catalog_lines(
    short: Callable[[Any, int], str],
    profile_default_route: Callable[[dict], str],
    state: dict, provider: dict, models: list,
    filtered_indices: list, query: str, live_search: bool, no_models: str,
    model_filter_label: str = "",
) -> list[str]:
    """Catalog text block for the per-provider models card. Short lines, no
    filler: the search/filter/source lines appear only when they say
    something (no "Search: All model IDs" when nothing is searched). A live
    search lists the live pool's entry count and source instead of the
    curated snapshot's; the static rendering is unchanged."""
    if live_search:
        freshness = "Live pool — fetched for this search"
    elif provider.get("catalog_refreshed"):
        freshness = "Live refresh completed"
    else:
        # The refresh is what makes the list fresh: show when the cached list
        # was last written, and say nothing at all when no timestamp is known.
        updated_at = provider.get("catalog_updated_at")
        freshness = (
            f"Updated {_short_ago(time.time() - float(updated_at))}"
            if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool)
            else None
        )
    free_count = sum(1 for index in filtered_indices if _is_free_model(models[index]))
    content_lines = [
        f"# Models · {short(provider['name'], 64)}",
        "Tap a model to make it the default route.",
        "---",
        f"Entries: **{len(models) if live_search else (provider['total_models'] or len(models))}** · matching: **{len(filtered_indices)}**",
        *( [f"{free_count} {'model is' if free_count == 1 else 'models are'} free"] if free_count else [] ),
        *( [f"Search: {short(query, 60)}"] if query else [] ),
        *( [f"Filter: {model_filter_label}"] if model_filter_label else [] ),
        *( [f"Source: Live pool — **{len(models)}** models fetched for this search"] if live_search else [] ),
        *( [f"Freshness: {freshness}"] if freshness else [] ),
        "Green marks the current model"
        + (" and free models" if any(_is_free_model(model) for model in models) else ""),
        "---",
        f"Route: **`{short(state['current_provider'], 64)} / {short(state['current_model'] or 'unknown', 160)}`**",
        f"Profile-wide default: **`{profile_default_route(state)}`**",
        f"Provider-listed default: `{short(provider.get('default_model') or 'not available', 160)}`",
    ]
    capabilities = _model_capabilities_text(
        str(state.get("current_provider") or ""),
        str(state.get("current_model") or ""),
    )
    if capabilities:
        content_lines.append(f"Capabilities: {capabilities}")
    if no_models:
        content_lines.extend(["", no_models])
    return content_lines
