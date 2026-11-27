# health: allow FILE_LINES -- upstream model state moved to session state (reasoning_override/one_turn_restore), channel-override route precedence and picker prompt authority, +15
"""Gateway slash commands that switch or tune the model route:
/model, /codex-runtime, /reasoning, /fast, /personality.

Split out of ``gateway/slash_commands.py``; bound onto ``GatewayRunner`` through
``GatewaySlashCommandsMixin``. Origin internals are imported lazily inside the bodies to avoid
the import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from hermes_cli.config import atomic_config_write
from utils import base_url_host_matches

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

# /fast argument -> (service tier, persisted value, i18n label key; None = value.upper()).
_FAST_SELECTIONS = {
    "fast": ("priority", "fast", "gateway.fast.label_fast"),
    "on": ("priority", "fast", "gateway.fast.label_fast"),
    "normal": (None, "normal", "gateway.fast.label_normal"),
    "off": (None, "normal", "gateway.fast.label_normal"),
    "auto": ("auto", "auto", None),
    "cold": ("cold", "cold", None),
    "ultrafast": ("ultrafast", "ultrafast", None),
}


def _fast_route_supports(model: str, runtime: dict, tier: Optional[str] = None) -> bool:
    """The turn's own gate (``run_turn.py::_resolve_turn_agent_config``): a tier /fast accepts must be
    one the session's next request actually carries, so proxies and other providers are refused."""
    from hermes_cli.models import resolve_fast_mode_overrides

    return resolve_fast_mode_overrides(
        model, provider=runtime.get("provider"), base_url=runtime.get("base_url"), tier=tier) is not None


# /reasoning display-toggle arguments -> show_reasoning value.
_REASONING_DISPLAY_TOGGLES = {"show": True, "on": True, "hide": False, "off": False}


# --- /reasoning rich cards ---------------------------------------------------
# In-body <tg-button> payloads the card buttons carry. The tap half lives in
# the Telegram adapter's hm: dispatch
# (plugins/platforms/telegram/telethon_adapter.py, _handle_reasoning_callback),
# re-dispatching each tap as the equivalent typed /reasoning command. The
# contract the dispatcher implements:
#   hm:reasoning:pick            open the effort picker for this chat
#   hm:reasoning:toggle-display  flip show/hide for this chat's platform
#   hm:reasoning:scope           clear a session override (falling back to
#                                the global scope), or toast that the scope
#                                is already global
_REASONING_CARD_EFFORT_ACTION = "hm:reasoning:pick"
_REASONING_CARD_DISPLAY_ACTION = "hm:reasoning:toggle-display"
_REASONING_CARD_SCOPE_ACTION = "hm:reasoning:scope"


def _reasoning_card_button(label: str, action: str) -> str:
    """One nested rich inline button: a callback pill, never a copy pill."""
    from plugins.platforms.telegram.mtproto_rich import rich_button

    return rich_button(label, kind="callback", value=action)


def _reasoning_card_field(label_key: str, button_html: str) -> str:
    """One ``<li>Label: button</li>`` row — indented under the card heading."""
    import html

    return f"<li>{html.escape(t(label_key), quote=False)}: {button_html}</li>"


def _reasoning_card_usage_html(command: str | None = None) -> str:
    """The Usage line — the ONLY copy pill in any /reasoning response."""
    from plugins.platforms.telegram.mtproto_rich import rich_button

    command = command or t("gateway.reasoning.card_usage_command")
    return (
        f"<p><i>{t('gateway.reasoning.card_usage_label')}</i> "
        f"{rich_button(command, kind='copy', value=command)}</p>"
    )


def _reasoning_card_html(title_key: str, body: str) -> str:
    """``<h1>`` title over the card body (list rows, notes, Usage line)."""
    import html

    return f"<h1>{html.escape(t(title_key), quote=False)}</h1>\n{body}"


def _reasoning_settings_card_html(level: str, scope: str, display_state: str) -> str:
    """The bare /reasoning card: Effort/Scope/Display as nested buttons, the
    Usage line as the only copy pill."""
    rows = "\n".join([
        "<ul>",
        _reasoning_card_field("gateway.reasoning.card_effort_label",
                              _reasoning_card_button(level, _REASONING_CARD_EFFORT_ACTION)),
        _reasoning_card_field("gateway.reasoning.card_scope_label",
                              _reasoning_card_button(scope, _REASONING_CARD_SCOPE_ACTION)),
        _reasoning_card_field("gateway.reasoning.card_display_label",
                              _reasoning_card_button(display_state, _REASONING_CARD_DISPLAY_ACTION)),
        "</ul>",
    ])
    return _reasoning_card_html(
        "gateway.reasoning.card_title", rows + "\n" + _reasoning_card_usage_html())


def _reasoning_set_card_html(effort: str, scope: str, scope_note_key: str) -> str:
    """Effort-set confirmation: Effort + Scope as nested buttons, scope note
    and the next-message note in italics."""
    import html

    rows = "\n".join([
        "<ul>",
        _reasoning_card_field("gateway.reasoning.card_effort_label",
                              _reasoning_card_button(effort, _REASONING_CARD_EFFORT_ACTION)),
        _reasoning_card_field("gateway.reasoning.card_scope_label",
                              _reasoning_card_button(scope, _REASONING_CARD_SCOPE_ACTION)),
        "</ul>",
    ])
    body = "\n".join([
        rows,
        f"<p><i>{html.escape(t(scope_note_key), quote=False)}</i></p>",
        f"<p><i>{html.escape(t('gateway.reasoning.card_next_message'), quote=False)}</i></p>",
    ])
    return _reasoning_card_html("gateway.reasoning.card_set_title", body)


def _reasoning_display_card_html(show: bool, platform_key: str) -> str:
    """Display-toggle confirmation: the new Display state as a nested button
    (tapping it toggles back) plus the platform note."""
    import html

    state = t("gateway.reasoning.display_on") if show else t("gateway.reasoning.display_off")
    note_key = ("gateway.reasoning.card_display_on_note" if show
                else "gateway.reasoning.card_display_off_note")
    body = "\n".join([
        "<ul>",
        _reasoning_card_field("gateway.reasoning.card_display_label",
                              _reasoning_card_button(state, _REASONING_CARD_DISPLAY_ACTION)),
        "</ul>",
        f"<p>{html.escape(t(note_key, platform=platform_key), quote=False)}</p>",
    ])
    return _reasoning_card_html("gateway.reasoning.card_display_title", body)


def _reasoning_reset_card_html() -> str:
    """Reset confirmation: the Scope button now reads global config."""
    import html

    body = "\n".join([
        "<ul>",
        _reasoning_card_field("gateway.reasoning.card_scope_label",
                              _reasoning_card_button(t("gateway.reasoning.scope_global"),
                                                     _REASONING_CARD_SCOPE_ACTION)),
        "</ul>",
        f"<p>{html.escape(t('gateway.reasoning.reset_done'), quote=False)}</p>",
    ])
    return _reasoning_card_html("gateway.reasoning.card_reset_title", body)


def _reasoning_reset_global_unsupported_card_html() -> str:
    """``reset --global`` refusal: note plus the Usage copy pill."""
    import html

    body = "\n".join([
        f"<p>{html.escape(t('gateway.reasoning.card_reset_global_unsupported'), quote=False)}</p>",
        _reasoning_card_usage_html(t("gateway.reasoning.card_reset_global_usage")),
    ])
    return _reasoning_card_html("gateway.reasoning.card_reset_title", body)


def _reasoning_unknown_arg_card_html(arg: str, current_effort: str) -> str:
    """Unknown argument: valid levels as a list, the current one first and
    marked (selected-first rule), italic page indexation, Usage copy pill."""
    import html

    from hermes_constants import VALID_REASONING_EFFORTS
    from plugins.platforms.telegram.mtproto_rich import rich_page_footer

    levels = ["none", *VALID_REASONING_EFFORTS]
    ordered = [current_effort] + [lv for lv in levels if lv != current_effort]
    items = []
    for lv in ordered:
        if lv == current_effort:
            items.append(f"<li><b>{html.escape(lv, quote=False)}</b> (current)</li>")
        else:
            items.append(f"<li>{html.escape(lv, quote=False)}</li>")
    body = "\n".join([
        f"<p>{html.escape(t('gateway.reasoning.card_unknown_detail', arg=arg), quote=False)}</p>",
        "<ul>",
        *items,
        "</ul>",
        f"<p>{html.escape(t('gateway.reasoning.card_unknown_display'), quote=False)}</p>",
        f"<p>{html.escape(t('gateway.reasoning.card_unknown_persist'), quote=False)}</p>",
        _reasoning_card_usage_html(),
        rich_page_footer(1, 1, len(levels)),
    ])
    return _reasoning_card_html("gateway.reasoning.card_unknown_title", body)


def _telegram_model_invocation(event: MessageEvent) -> dict[str, Any]:
    """Keep menu deletion bound to the command, never a task/topic reply anchor."""
    platform = getattr(event.source.platform, "value", event.source.platform)
    if str(platform).lower() == "telegram":
        return {"invocation_message_id": event.message_id}
    return {}


def _provider_not_shared_reply(provider: str) -> str:
    """Refusal when a switch would spend a credential another user did not share."""
    return (f"Provider {provider} was added by another user and is not shared with you. "
            "Ask them to share it, or pick another provider.")


def _model_switch_skew_guard() -> Optional[str]:
    """Refuse a model switch when the gateway is running stale code: a first-time lazy import on
    a new code path can crash on a stale cached dependency. Scoped to the highest-risk trigger."""
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=t("gateway.model.err_skew", boot_rev=boot_rev, disk_rev=disk_rev),
    )


async def _persist_model_switch_to_config(result, config_path) -> None:
    """Write-through a resolved /model switch to the profile config at ``config_path``, off the
    event loop (the route comparison can do cold-start disk I/O)."""
    from hermes_cli.model_switch import persist_model_selection
    await asyncio.to_thread(persist_model_selection, result, config_path)


@dataclasses.dataclass
class _ModelSwitchContext:
    """Everything a /model switch needs beyond the target: current route + persistence policy."""

    session_key: str
    source: Any
    config_path: Any
    persist_global: bool
    one_turn: bool = False
    reasoning_effort: str = ""  # `--reasoning <level>` riding with the pick (typed path only)
    restore_snapshot: Optional[dict] = None
    current_model: str = ""
    current_provider: str = "openrouter"
    # The provider actually configured/overridden (None = unset): ``current_provider`` defaults to
    # openrouter for switch_model, which must not label the configured model in metrics.
    route_provider: Optional[str] = None
    current_base_url: str = ""
    current_api_key: str = ""
    user_provs: Any = None
    custom_provs: Any = None
    excluded_provs: list = dataclasses.field(default_factory=list)

    def read_config(self) -> None:
        """Fill the current route from ``config_path``; fail-open to the defaults."""
        from gateway.run import _load_gateway_config
        try:
            cfg = _load_gateway_config(config_path=self.config_path)
            if not cfg:
                return
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                self.current_model = model_cfg.get("default", "")
                self.current_provider = model_cfg.get("provider", self.current_provider)
                self.route_provider = model_cfg.get("provider")
                self.current_base_url = model_cfg.get("base_url", "")
            # Global provider sync: union the profile's definitions with the
            # launch home's store (global wins); the profile's own route
            # above stays the profile's. A union failure falls back to the
            # profile's definitions alone.
            try:
                from pathlib import Path

                from gateway.provider_sharing import union_provider_definitions
                from hermes_cli.config_providers import global_provider_home

                global_home = global_provider_home()
                if global_home.resolve() != Path(self.config_path).parent.resolve():
                    global_cfg = _load_gateway_config(config_path=global_home / "config.yaml")
                    if isinstance(global_cfg, dict) and global_cfg:
                        cfg = union_provider_definitions(cfg, global_cfg)
            except Exception:
                logger.debug("Global provider union failed; using profile definitions only", exc_info=True)
            self.user_provs = cfg.get("providers")
            try:
                from hermes_cli.config import get_compatible_custom_providers
                self.custom_provs = get_compatible_custom_providers(cfg)
            except Exception:
                self.custom_provs = cfg.get("custom_providers")
            excl = cfg.get("model_catalog", {}).get("excluded_providers")
            if isinstance(excl, list):
                self.excluded_provs = excl
        except Exception:
            pass

    def apply_override(self, override: dict) -> None:
        """A session /model override supersedes the configured route."""
        if override:
            self.current_model = override.get("model", self.current_model)
            self.current_provider = override.get("provider", self.current_provider)
            self.route_provider = override.get("provider", self.route_provider)
            self.current_base_url = override.get("base_url", self.current_base_url)
            self.current_api_key = override.get("api_key", self.current_api_key)



_TEXT_LISTING_MODELS = 5


def _model_provider_listing_lines(providers) -> list[str]:
    """Text-list body for ``/model`` with no args on platforms without a picker."""
    lines: list[str] = []
    for p in providers:
        tag = t("gateway.model.current_tag") if p["is_current"] else ""
        lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
        if p["models"]:
            shown = p["models"][:_TEXT_LISTING_MODELS]  # uncapped rows arrive full; this is a preview
            model_strs = ", ".join(f"`{m}`" for m in shown)
            hidden = p["total_models"] - len(shown)
            extra = t("gateway.model.more_models_suffix", count=hidden) if hidden > 0 else ""
            lines.append(f"  {model_strs}{extra}")
        elif p.get("api_url"):
            lines.append(f"  `{p['api_url']}`")
        lines.append("")
    return lines


# --- /draft rich cards ------------------------------------------------------
# Short beats of the /draft showcase. Topic 1 lists every member of
# plugins/platforms/telegram/streaming_response.py::ResponseStreamDraft —
# verified against the class, never from memory; the test cross-checks each
# name against the live class. Topics 2 and 6 are live demos driven by
# gateway/draft_demo.py (step API); this module only calls them.
# Every beat is one short message: a title plus at most three lines.

# Topic 1 beats: (preview label, title key, member names) — grouped by
# role, at most three members per message; every member appears exactly
# once. Descriptions come from gateway.slash_commands_model_draft
# _DRAFT_STREAM_MEMBERS, by name.
_DRAFT_TOPIC1_BEATS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("the draft sink", "gateway.draft.beat_t1_sink_title",
     ("__init__", "random_id", "stop_requested")),
    ("feeding the draft", "gateway.draft.beat_t1_feed_title",
     ("prime_thinking_placeholder", "on_reasoning", "on_content")),
    ("deltas and partial text", "gateway.draft.beat_t1_deltas_title",
     ("on_delta", "partial_text")),
    ("stopping and finishing", "gateway.draft.beat_t1_stop_title",
     ("request_stop", "finish_sync_draft", "cancel")),
    ("inside: frames", "gateway.draft.beat_t1_frames_title",
     ("_frame", "_maybe_send", "_send_now")),
    ("inside: the pump", "gateway.draft.beat_t1_pump_title",
     ("_ensure_pump", "_pump", "_cancel_pump")),
    ("inside: sending", "gateway.draft.beat_t1_sending_title",
     ("_send_sync", "_resolve_input_peer", "_build_action")),
    ("inside: failures", "gateway.draft.beat_t1_failures_title",
     ("_send_frame", "_note_failure", "clear_draft")),
)

# Static beats: (preview label, title key, lines) — each line is a short
# HTML fragment; at most three lines per message.
_DRAFT_TOPIC3_BEATS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("rich drafts", "gateway.draft.beat_t3_rich_title", (
        "Each frame is a native rich draft, never a sent message.",
        "Reasoning-only: a Thinking block, reasoning in italic.",
        "Once content flows, the frozen reasoning stays above it.",
    )),
    ("throttle and keepalive", "gateway.draft.beat_t3_pacing_title", (
        "1s throttle: at most one update per second.",
        "20s keepalive: re-push inside the TTL.",
        "30s TTL: the draft expires without a re-push.",
    )),
    ("sync draft and fail-open", "gateway.draft.beat_t3_sync_title", (
        "Before the final: one draft equal to its first chunk.",
        "The client adopts the final only if it shares a leading prefix.",
        "Fail-open: a dead preview never breaks the turn.",
    )),
)

_DRAFT_TOPIC4_BEATS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("the placeholder action", "gateway.draft.beat_t4_placeholder_title", (
        "Static “Thinking” placeholder — <code>SendMessageTextDraftAction</code>",
        "Stop: on",
    )),
    ("the reasoning action", "gateway.draft.beat_t4_reasoning_title", (
        "Thinking block, reasoning in italic — <code>InputSendMessageRichMessageDraftAction</code>",
        "Stop: on",
    )),
    ("the content action", "gateway.draft.beat_t4_content_title", (
        "Frozen reasoning above streaming content — <code>InputSendMessageRichMessageDraftAction</code>",
        "Stop: on",
    )),
    ("the sync action", "gateway.draft.beat_t4_sync_title", (
        "Plain first chunk of the completed final — <code>InputSendMessageRichMessageDraftAction</code>",
        "Falls back to <code>SendMessageTextDraftAction</code> without rich",
        "Stop: off — the turn is over",
    )),
)

_DRAFT_TOPIC5_BEATS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("how drafts travel", "gateway.draft.beat_t5_travel_title", (
        "<code>SetTypingRequest</code>: drafts travel as a typing action.",
        "One fixed <code>random_id</code> per generation, never reused for the final.",
        "Draft TTL: about 30 seconds.",
    )),
    ("adoption and caps", "gateway.draft.beat_t5_limits_title", (
        "Final adopted only if it shares a leading prefix with the last draft.",
        "Stop routes by <code>random_id</code> to exactly this generation.",
        "Caps: 4000 chars per frame, reasoning preview 8000.",
    )),
)

_DRAFT_TOPIC7_BEATS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Stop", "gateway.draft.beat_t7_stop_title", (
        "Placeholder, reasoning, and content drafts carry Stop.",
        "A press routes by <code>random_id</code> to <code>request_stop</code>.",
        "The streamed partial is delivered as the final.",
    )),
    ("shown once", "gateway.draft.beat_t7_once_title", (
        "Reasoning shows in the draft when streaming is on.",
        "It is never repeated in the final message.",
    )),
    ("fail-open", "gateway.draft.beat_t7_failopen_title", (
        "Draft failures never break the turn.",
        "After three in a row, the sink disables itself.",
        "The final always goes through the normal delivery path.",
    )),
)

# Topic 8 beats: every other SendMessage*Action the installed Telethon
# wheel knows, one beat per action — (preview label, title key, card
# lines, TL action name, persist message). The persist message names
# the action (its TL type plus the official core.telegram.org
# description of what the peer's client shows); these actions have no
# content of their own to persist. The stop-draft beat is a separate
# explicit beat after the loop: it clears the demo reasoning draft, so
# its persist message carries that draft's content.
_DRAFT_TOPIC8_BEATS: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("typing", "gateway.draft.beat_t8_typing_title", (
        "Plain typing indicator — <code>SendMessageTypingAction</code>",
        "The client shows “typing…”",
        "Held 10s, re-sent every 4s: one send expires in seconds.",
    ), "SendMessageTypingAction",
     "Typing\nSendMessageTypingAction — user is typing."),
    ("uploading a photo", "gateway.draft.beat_t8_upload_photo_title", (
        "Upload with progress — <code>SendMessageUploadPhotoAction</code>",
        "Demo progress: 50%.",
        "The client shows “uploading a photo…”",
    ), "SendMessageUploadPhotoAction",
     "Uploading a photo\nSendMessageUploadPhotoAction — user is uploading a photo."),
    ("uploading a video", "gateway.draft.beat_t8_upload_video_title", (
        "Upload with progress — <code>SendMessageUploadVideoAction</code>",
        "Demo progress: 50%.",
        "The client shows “uploading a video…”",
    ), "SendMessageUploadVideoAction",
     "Uploading a video\nSendMessageUploadVideoAction — user is uploading a video."),
    ("uploading a voice message", "gateway.draft.beat_t8_upload_audio_title", (
        "Upload with progress — <code>SendMessageUploadAudioAction</code>",
        "Demo progress: 50%.",
        "The client shows “uploading a voice message…”",
    ), "SendMessageUploadAudioAction",
     "Uploading a voice message\nSendMessageUploadAudioAction — user is uploading a voice message."),
    ("uploading a file", "gateway.draft.beat_t8_upload_document_title", (
        "Upload with progress — <code>SendMessageUploadDocumentAction</code>",
        "Demo progress: 50%.",
        "The client shows “uploading a file…”",
    ), "SendMessageUploadDocumentAction",
     "Uploading a file\nSendMessageUploadDocumentAction — user is uploading a file."),
    ("uploading a round video", "gateway.draft.beat_t8_upload_round_title", (
        "Upload with progress — <code>SendMessageUploadRoundAction</code>",
        "Demo progress: 50%.",
        "The client shows “uploading a round video…”",
    ), "SendMessageUploadRoundAction",
     "Uploading a round video\nSendMessageUploadRoundAction — user is uploading a round video."),
    ("recording a video", "gateway.draft.beat_t8_record_video_title", (
        "Recording indicator — <code>SendMessageRecordVideoAction</code>",
        "The client shows “recording a video…”",
    ), "SendMessageRecordVideoAction",
     "Recording a video\nSendMessageRecordVideoAction — user is recording a video."),
    ("recording a voice message", "gateway.draft.beat_t8_record_audio_title", (
        "Recording indicator — <code>SendMessageRecordAudioAction</code>",
        "The client shows “recording a voice message…”",
    ), "SendMessageRecordAudioAction",
     "Recording a voice message\nSendMessageRecordAudioAction — user is recording a voice message."),
    ("recording a round video", "gateway.draft.beat_t8_record_round_title", (
        "Recording indicator — <code>SendMessageRecordRoundAction</code>",
        "The client shows “recording a round video…”",
    ), "SendMessageRecordRoundAction",
     "Recording a round video\nSendMessageRecordRoundAction — user is recording a round video to share."),
    ("choosing a location", "gateway.draft.beat_t8_geo_title", (
        "Location picker — <code>SendMessageGeoLocationAction</code>",
        "The client shows “choosing a location…”",
    ), "SendMessageGeoLocationAction",
     "Choosing a location\nSendMessageGeoLocationAction — user is selecting a location to share."),
    ("choosing a contact", "gateway.draft.beat_t8_contact_title", (
        "Contact picker — <code>SendMessageChooseContactAction</code>",
        "The client shows “choosing a contact…”",
    ), "SendMessageChooseContactAction",
     "Choosing a contact\nSendMessageChooseContactAction — user is selecting a contact to share."),
    ("choosing a sticker", "gateway.draft.beat_t8_sticker_title", (
        "Sticker picker — <code>SendMessageChooseStickerAction</code>",
        "The client shows “choosing a sticker…”",
    ), "SendMessageChooseStickerAction",
     "Choosing a sticker\nSendMessageChooseStickerAction — user is choosing a sticker."),
    ("playing a game", "gateway.draft.beat_t8_game_title", (
        "Game status — <code>SendMessageGamePlayAction</code>",
        "The client shows “playing a game…”",
    ), "SendMessageGamePlayAction",
     "Playing a game\nSendMessageGamePlayAction — user is playing a game."),
    ("importing chat history", "gateway.draft.beat_t8_history_title", (
        "History import — <code>SendMessageHistoryImportAction</code>",
        "Demo progress: 50%.",
        "The client shows “importing chat history…”",
    ), "SendMessageHistoryImportAction",
     "Importing chat history\nSendMessageHistoryImportAction — chat history is being imported."),
    ("cancelling the indicator", "gateway.draft.beat_t8_cancel_title", (
        "Clears the indicator — <code>SendMessageCancelAction</code>",
        "Invalidates all previous action updates.",
        "The client shows nothing after it.",
    ), "SendMessageCancelAction",
     "Cancelling the indicator\nSendMessageCancelAction — invalidates all previous action updates; the indicator clears."),
)


def _draft_is_owner_actor(runner: Any, adapter: Any, source: Any) -> bool:
    """True when the actor may run /draft, mirroring personal_settings_ui._is_owner_actor:
    no owner distinction exists unless multiuser is enabled for the profile home; under
    multiuser the adapter's owner check wins; an undeterminable owner fails closed."""
    from pathlib import Path

    try:
        from gateway.mongo_multiuser import enabled

        home = runner._authorization_home_for_source(source)
        home_path = Path(home) if home else None
        if home_path is None or not enabled(home_path):
            return True
    except Exception:
        return False
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(getattr(source, "user_id", None), source))
    except Exception:
        return False



def _canonical_route_provider(provider: str) -> str:
    """Canonical slug for a stored route id (registry id, aliases folded).

    Picks resolve under the models.dev id, so config may store an alias
    ("opencode" for OpenCode Zen, "kilo" for Kilo Code) the registry never
    lists; folding keeps the by_slug lookup from inventing a second row named
    after the alias as the Current one.
    """
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.models import normalize_provider

    provider = str(provider or "").strip().lower()
    config = PROVIDER_REGISTRY.get(provider)
    if config is not None:
        provider = str(config.id or provider).strip().lower()
    return normalize_provider(provider)


def _annotate_catalog_rows(by_slug: dict) -> None:
    """Attach each snapshot row's catalog status and cached-list last-updated time.

    The cache is keyed by canonical slug, so an aliased row (kilo) looks up
    its canonical key (kilocode); ollama stays raw. None when there is no
    usable cached list — the card omits the freshness line then, instead of
    claiming an unverifiable freshness.
    """
    from hermes_cli.model_switch_providers import provider_models_cache_updated_at_map
    from hermes_cli.models import normalize_provider

    catalog_updated_at = provider_models_cache_updated_at_map()
    for slug, row in by_slug.items():
        row.setdefault("catalog_status", "not_configured")
        if row.get("configured") and row["catalog_status"] == "not_configured":
            row["catalog_status"] = "cached_or_curated" if row.get("models") else "unavailable"
        row["catalog_updated_at"] = catalog_updated_at.get(
            slug, catalog_updated_at.get(normalize_provider(slug)))


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime, /reasoning, /fast, /personality)."""

    async def _union_global_provider_definitions(self, cfg: dict, profile_home) -> dict:
        """Union the profile config with the global provider store (global wins).

        The global load and the home comparison run off the event loop
        (Path.resolve() is a blocking filesystem call). The profile's own
        route stays the profile's — only definitions union.
        """
        from pathlib import Path

        from gateway.provider_sharing import union_provider_definitions
        from gateway.run import _load_gateway_config
        from hermes_cli.config_providers import global_provider_home

        global_home = global_provider_home()
        same_home = await asyncio.to_thread(
            lambda: global_home.resolve() == Path(profile_home).resolve(),
        )
        if same_home:
            return cfg
        global_cfg = await asyncio.to_thread(
            _load_gateway_config, config_path=global_home / "config.yaml",
        )
        if isinstance(global_cfg, dict) and global_cfg:
            return union_provider_definitions(cfg, global_cfg)
        return cfg

    async def _provider_inventory_snapshot(self, source, *, force_rebuild: bool = False):
        """Build a cache-first provider snapshot; stale catalogs warm off the render path."""
        from gateway.run import _hermes_home, _load_gateway_config, _profile_runtime_scope
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.config import get_compatible_custom_providers, stringify_provider_map
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.provider_catalog import provider_catalog
        from hermes_cli.providers import custom_provider_slug

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(source)
        profile_home = profile_home or _hermes_home
        config_path = profile_home / "config.yaml"
        scope = _profile_runtime_scope(profile_home)
        with scope:
            cfg = await asyncio.to_thread(_load_gateway_config, config_path=config_path)
            cfg = cfg if isinstance(cfg, dict) else {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                current_model = str(model_cfg.get("default", model_cfg.get("name", "")) or "")
                current_provider = str(model_cfg.get("provider", "openrouter") or "openrouter")
                current_base_url = str(model_cfg.get("base_url", "") or "")
            else:
                current_model, current_provider = str(model_cfg or ""), "openrouter"
                current_base_url = ""
            configured_default_model = current_model
            configured_default_provider = current_provider.strip().lower()
            normalized_source = await asyncio.to_thread(self._normalize_source_for_session_key, source)
            session_key = self._session_key_for_source(normalized_source)
            override = (getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}
            if override:
                current_model = str(override.get("model") or current_model)
                current_provider = str(override.get("provider") or current_provider)
                current_base_url = str(override.get("base_url") or current_base_url)
            current_provider = _canonical_route_provider(current_provider)
            configured_default_provider = _canonical_route_provider(configured_default_provider)

            # Repeated opens re-read config, pool and catalogs; a 60s cache
            # with stale-serve background refresh covers them. Any input-file
            # change busts the key, so a fresh credential or catalog refresh
            # shows on the next open, not after the TTL.
            cache = getattr(self, "_provider_inventory_cache", None)
            if cache is None:
                from gateway.provider_inventory_cache import ProviderInventoryCache

                cache = ProviderInventoryCache()
                self._provider_inventory_cache = cache
            cache_key = cache.key_for(
                profile_home=profile_home,
                launch_home=_hermes_home,
                session_key=session_key,
                override=override,
                user_id=getattr(source, "user_id", None),
            )
            if not force_rebuild:
                state, cached = cache.get(cache_key)
                if state is not None and cached is not None:
                    if state == "stale":
                        cache.refresh_in_background(
                            cache_key, source, self._provider_inventory_snapshot
                        )
                    return cache.copy(cached)

            # Global provider sync: the launch home holds the one provider
            # store. The snapshot unions the profile's own definitions with
            # the global ones (global wins on slug/URL conflict); the
            # profile's own route (model.default/provider) stays the
            # profile's. The visibility filter below
            # (provider_visible_to_user) then hides what the acting user
            # may not use.
            cfg = await self._union_global_provider_definitions(cfg, profile_home)
            user_providers = stringify_provider_map(cfg.get("providers"))
            custom_providers = get_compatible_custom_providers(cfg)
            excluded = cfg.get("model_catalog", {}).get("excluded_providers")
            if not isinstance(excluded, list):
                excluded = []
            # Return disk/curated snapshots immediately and let the picker
            # API deduplicate stale catalog warming in the background. Never
            # wait for live provider/custom-endpoint probes just to render UI.
            authenticated_rows = await asyncio.to_thread(
                list_authenticated_providers,
                current_provider=current_provider,
                current_base_url=current_base_url,
                current_model=current_model,
                user_providers=user_providers,
                custom_providers=custom_providers,
                excluded_providers=excluded,
                max_models=250,
                refresh=False,
                probe_custom_providers=False,
                probe_current_custom_provider=False,
                non_blocking_catalogs=True,
            )

            # Providers with credentials right now (the credential truth for the
            # "configured" badge); a config.yaml model.provider pointer alone is
            # a route selection, not a credential, and must never mark a
            # credential-less provider as configured.
            authed_slugs = {
                str(provider.get("slug") or "").strip().lower()
                for provider in authenticated_rows
            }

            by_slug: dict[str, dict[str, Any]] = {}
            # Start from the CLI's unified catalog, not the authenticated-only
            # list, so users can inspect setup instructions before configuring a
            # provider. This metadata is display-safe and contains no credentials.
            catalog = provider_catalog()
            for descriptor in catalog:
                slug = str(descriptor.slug or "").strip().lower()
                if not slug:
                    continue
                by_slug[slug] = {
                    "slug": slug,
                    "name": str(descriptor.label or slug),
                    "configured": False,
                    "enabled": True,
                    "is_catalog_provider": True,
                    "is_user_defined": slug in {"custom", "moa"},
                    "models": [],
                    "total_models": 0,
                    "description": str(descriptor.description or ""),
                    "auth_type": str(descriptor.auth_type or "api_key"),
                    "api_key_env_vars": list(descriptor.api_key_env_vars or ()),
                    "base_url_env_var": str(descriptor.base_url_env_var or ""),
                    "signup_url": str(descriptor.signup_url or ""),
                    "catalog_order": int(descriptor.order),
                    "default_model": "",
                    "default_reasoning": "",
                    "catalog_refreshed": False,
                }
            for provider in PROVIDER_REGISTRY.values():
                slug = str(getattr(provider, "id", "") or "").strip().lower()
                if slug:
                    by_slug.setdefault(slug, {
                        "slug": slug,
                        "name": str(getattr(provider, "name", "") or slug),
                        "configured": False,
                        "enabled": True,
                        "is_catalog_provider": False,
                        "is_user_defined": False,
                        "models": [],
                        "total_models": 0,
                        "catalog_order": len(catalog) + len(by_slug),
                    })

            for slug, entry in user_providers.items():
                if not isinstance(entry, dict):
                    continue
                normalized = str(slug).strip().lower()
                if not normalized:
                    continue
                row = by_slug.setdefault(normalized, {
                    "slug": normalized, "name": str(entry.get("name") or normalized),
                    "configured": True, "enabled": True, "is_user_defined": True,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row["name"] = str(entry.get("name") or row["name"])
                row["enabled"] = bool(entry.get("enabled", True))
                row["is_user_defined"] = True
                row["configured"] = True
                configured_model = str(entry.get("default_model") or entry.get("model") or "").strip()
                if configured_model:
                    row["default_model"] = configured_model

            for custom in custom_providers:
                if not isinstance(custom, dict):
                    continue
                slug = (str(custom.get("provider_key") or "").strip().lower()
                        or custom_provider_slug(str(custom.get("name") or "custom"), ""))
                row = by_slug.setdefault(slug, {
                    "slug": slug, "name": str(custom.get("name") or slug),
                    "configured": True, "enabled": True, "is_user_defined": True,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row["configured"] = True
                row["is_user_defined"] = True
                custom_model = str(custom.get("model") or custom.get("default_model") or "").strip()
                if custom_model:
                    row["default_model"] = custom_model

            for provider in authenticated_rows:
                slug = str(provider.get("slug") or "").strip().lower()
                if not slug:
                    continue
                row = by_slug.setdefault(slug, {
                    "slug": slug, "name": str(provider.get("name") or slug),
                    "configured": True, "enabled": True,
                    "is_user_defined": bool(provider.get("is_user_defined")),
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row.update({
                    "name": str(provider.get("name") or row["name"]),
                    "configured": True,
                    "is_user_defined": bool(provider.get("is_user_defined", row["is_user_defined"])),
                    "models": [str(model) for model in (provider.get("models") or []) if str(model).strip()],
                    "total_models": int(provider.get("total_models") or len(provider.get("models") or [])),
                    "catalog_refreshed": False,
                    "catalog_status": "cached_or_curated" if provider.get("models") else "unavailable",
                })

            current_provider = current_provider.strip().lower()
            current_row = by_slug.get(current_provider)
            if current_row is None:
                current_row = {
                    "slug": current_provider or "unknown", "name": current_provider or "Unknown provider",
                    "configured": False, "enabled": True, "is_user_defined": False,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                }
                by_slug[current_row["slug"]] = current_row
            current_row["configured"] = bool(current_row.get("configured") or current_provider in authed_slugs)
            if current_provider == configured_default_provider:
                current_row["default_model"] = configured_default_model or current_row.get("default_model") or current_model
            agent_cfg = cfg.get("agent", {})
            agent_cfg = agent_cfg if isinstance(agent_cfg, dict) else {}
            reasoning_overrides = agent_cfg.get("reasoning_overrides", {})
            reasoning_overrides = reasoning_overrides if isinstance(reasoning_overrides, dict) else {}
            global_reasoning = agent_cfg.get("reasoning_effort", "")
            if global_reasoning is False:
                global_reasoning = "none"
            for provider_slug, row in by_slug.items():
                if not row.get("default_model"):
                    try:
                        from hermes_cli.models import get_default_model_for_provider
                        row["default_model"] = get_default_model_for_provider(provider_slug) or ""
                    except Exception:
                        row["default_model"] = ""
                effort = reasoning_overrides.get(row.get("default_model"), global_reasoning)
                row["default_reasoning"] = "none" if effort is False else str(effort or "")
            _annotate_catalog_rows(by_slug)
            for slug, row in by_slug.items():
                row["is_current"] = slug == current_provider
                row["is_profile_default"] = slug == configured_default_provider
                row["profile_default_model"] = (
                    configured_default_model if slug == configured_default_provider else ""
                )
                # Status helpers inspect local config/auth stores and env only; skip unknown IDs
                # so a catalog lookup is never triggered just to render a menu.
                # A config.yaml model.provider pointer (or auth.json active_provider) is a
                # route selection, not a credential: it must never resurrect "configured"
                # for a provider whose credentials were removed. Only real auth
                # material counts here (pool entry, env credential, provider-state
                # material such as a Nous agent_key/guest identity, or keyless
                # Vertex/Bedrock routing config).
                if slug in PROVIDER_REGISTRY:
                    try:
                        from hermes_cli.auth import (
                            _explicit_env_credentials_present,
                            _explicit_pool_entry_present,
                            _keyless_provider_has_explicit_config,
                            provider_has_local_auth_material,
                        )
                        row["configured"] = bool(
                            row["configured"]
                            or _explicit_env_credentials_present(slug)
                            or _explicit_pool_entry_present(slug)
                            or provider_has_local_auth_material(slug)
                            or _keyless_provider_has_explicit_config(slug)
                        )
                    except Exception:
                        pass
                row["reasoning_supported"] = True
                row["reasoning_efforts"] = []
                if slug == current_provider and current_model and slug in {"openrouter", "nous"}:
                    try:
                        from hermes_cli.models_reasoning_caps import (
                            nous_model_reasoning_capabilities,
                            openrouter_model_reasoning_capabilities,
                        )
                        caps_fn = (
                            openrouter_model_reasoning_capabilities if slug == "openrouter"
                            else nous_model_reasoning_capabilities
                        )
                        caps = caps_fn(current_model, allow_fetch=False)
                        if caps is not None:
                            row["reasoning_supported"] = caps.get("supports_reasoning") is True
                            efforts = caps.get("supported_efforts")
                            if isinstance(efforts, list):
                                row["reasoning_efforts"] = efforts
                    except Exception:
                        pass
            # Per-user provider sharing (multiuser): a credential added by one
            # user is theirs until they share it. Hide providers whose
            # credentials are all invisible to the acting user; the turn-time
            # check in _resolve_session_agent_runtime still guards stale
            # session overrides / profile defaults pointing at them.
            acting_user = str(getattr(source, "user_id", "") or "").strip() if source is not None else ""
            if acting_user:
                from gateway.provider_sharing import provider_visible_to_user

                by_slug = {
                    slug: row for slug, row in by_slug.items()
                    if provider_visible_to_user(slug, acting_user)
                }
            providers = sorted(
                by_slug.values(),
                key=lambda row: (
                    not row["is_current"], not row["configured"],
                    int(row.get("catalog_order", len(catalog))), str(row["name"]).casefold(),
                ),
            )
            result = (providers, current_model, current_provider, session_key, profile_home)
            cache.put(cache_key, cache.copy(result))
            return result

    @staticmethod
    def _provider_inventory_text(providers: list[dict], current_model: str, current_provider: str,
                                 *, models_only: bool = False) -> str:
        if models_only:
            lines = ["# Available models", "Read-only local cached/curated snapshot. Stale catalogs warm in the background; freshness is not verified on this view. Use `/model` to switch the current model.", ""]
            visible = [row for row in providers if row.get("models")]
            if not visible:
                lines.append("No available model catalogs were found for the configured providers.")
            for row in visible:
                lines.append(f"## {row['name']} (`{row['slug']}`) — {row['total_models']} models")
                lines.extend(f"- `{model}`" for model in row["models"])
                lines.append("")
            return "\n".join(lines).rstrip()
        lines = ["# Providers", f"Current route: `{current_provider}` · `{current_model or 'unknown'}`",
                 "Catalog counts are local cached/curated snapshots; stale catalogs refresh in the background.", ""]
        for row in providers:
            status = "configured" if row.get("configured") else "not configured"
            if not row.get("enabled", True):
                status = "disabled"
            lines.append(f"- **{row['name']}** (`{row['slug']}`): {status}; {row.get('total_models', 0)} catalog models")
        lines.extend(["", "Credential values are never shown. Use `/models` for the read-only catalog."])
        return "\n".join(lines)

    async def _handle_models_command(self, event: MessageEvent) -> Optional[str]:
        """List available model IDs; this is distinct from /model's switch/picker flow."""
        providers, current_model, current_provider, session_key, profile_home = \
            await self._provider_inventory_snapshot(event.source)
        requested = (event.get_command_args() or "").strip().casefold()
        selected = next((row for row in providers if requested in {
            str(row.get("slug", "")).casefold(), str(row.get("name", "")).casefold()
        }), None) if requested else None
        if requested and selected is None:
            matches = ", ".join(f"`{row['slug']}`" for row in providers if requested in str(row.get("slug", "")).casefold())
            return (f"No provider matched `{requested}`." + (f" Similar IDs: {matches}." if matches else ""))
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_provider_management", None)
        if callable(send_ui):
            if await send_ui(
                source=event.source, providers=providers, current_model=current_model,
                current_provider=current_provider, session_key=session_key, profile_home=profile_home,
                view="models", selected_provider=selected.get("slug") if selected else None,
                reply_to=self._reply_anchor_for_event(event),
                **_telegram_model_invocation(event),
            ):
                return None
            if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
                return None  # Telegram provider cards never degrade to a plain-text fallback.
        return self._provider_inventory_text(providers, current_model, current_provider, models_only=True)

    async def _handle_providers_command(self, event: MessageEvent) -> Optional[str]:
        """Open provider status, model-catalog, credential-status, and reasoning controls."""
        providers, current_model, current_provider, session_key, profile_home = \
            await self._provider_inventory_snapshot(event.source)
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_provider_management", None)
        if callable(send_ui) and await send_ui(
            source=event.source, providers=providers, current_model=current_model,
            current_provider=current_provider, session_key=session_key, profile_home=profile_home,
            view="providers", reply_to=self._reply_anchor_for_event(event),
            **_telegram_model_invocation(event),
        ):
            return None
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # Telegram provider cards never degrade to a plain-text fallback.
        return self._provider_inventory_text(providers, current_model, current_provider)

    async def _handle_mcp_command(self, event: MessageEvent) -> Optional[str]:
        """Open the profile-aware MCP catalogue and personal-access controls."""
        from gateway.run import _hermes_home

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        profile_home = profile_home or _hermes_home
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_mcp_management", None)
        if callable(send_ui) and await send_ui(
            source=event.source, profile_home=profile_home,
            reply_to=self._reply_anchor_for_event(event),
        ):
            return None
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # MCP management uses native MTProto RichMessages only.
        return "Interactive MCP management is not available on this chat platform."

    async def _handle_plugins_command(self, event: MessageEvent) -> Optional[str]:
        """Open the private, profile-aware general-plugin manager."""
        from gateway.run import _hermes_home

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        profile_home = profile_home or _hermes_home
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_plugin_management", None)
        if callable(send_ui):
            try:
                args = (event.get_command_args() or "").strip()
                query = ""
                view = "list"
                catalog_name = None
                if args:
                    parts = args.split(maxsplit=1)
                    sub = parts[0].lower()
                    sub_arg = parts[1].strip() if len(parts) > 1 else ""
                    if sub in {"search", "find"} and sub_arg:
                        query = sub_arg
                        view = "search"
                    elif sub in {"catalog", "browse"}:
                        view = "catalog"
                    elif sub in {"install", "add"} and sub_arg:
                        catalog_name = sub_arg
                        view = "catalog_detail"
                    else:
                        query = args
                        view = "search"
                if await send_ui(
                    source=event.source, profile_home=profile_home,
                    reply_to=self._reply_anchor_for_event(event),
                    query=query, view=view, catalog_name=catalog_name,
                ):
                    return None
            except Exception:
                logger.warning("Could not open the Telegram plugin manager", exc_info=True)
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # Plugin management uses native MTProto RichMessages only.
        return "Interactive plugin management is available only in the Telegram owner's private chat."

    async def _send_model_dashboard(self, event: MessageEvent, providers, current_model,
                                    current_provider, session_key, profile_home) -> Optional[str]:
        from gateway.model_ui import dashboard_payload, render_dashboard

        state = self._session_state(session_key).conversation
        reasoning = state.reasoning_override
        if isinstance(reasoning, dict):
            reasoning = reasoning.get("effort") or reasoning.get("reasoning_effort") or ""
        payload = dashboard_payload(
            current_model=current_model, current_provider=current_provider, providers=providers,
            settings=dict(state.model_settings), reasoning_effort=str(reasoning or ""),
        )
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_model_dashboard", None)
        if callable(send_ui):
            if await send_ui(
                source=event.source, dashboard=payload, providers=providers,
                current_model=current_model, current_provider=current_provider,
                session_key=session_key, profile_home=profile_home,
                reply_to=self._reply_anchor_for_event(event),
                **_telegram_model_invocation(event),
            ):
                return None
        return render_dashboard(payload)

    def _model_settings_owner_error(self, source, profile_home, allow_group: bool = False) -> Optional[str]:
        """Require a Telegram private conversation for model settings, or a
        group chat when the caller allows it.

        No-limits: any admitted user may change model settings. The DM
        requirement stays because the settings UI only renders there; a group
        is admitted by the /msettings handler for any admitted user.
        """
        allowed_chats = {"dm", "private", "group", "forum"} if allow_group else {"dm", "private"}
        if (str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower()
                != "telegram" or getattr(source, "chat_type", None) not in allowed_chats
                or not getattr(source, "chat_id", None) or getattr(source, "is_bot", False)):
            return ("Model settings are available only in a Telegram DM or group."
                    if allow_group else "Model settings are available only in a Telegram DM.")
        return None

    def _model_route_owner(self, source, profile_home) -> bool:
        """True when the actor may change the profile-wide default route.

        No-limits: any admitted user may change it. The owner check is gone.
        """
        return True

    def _model_settings_reasoning(self, session_key: str) -> str:
        _rs_state = self._peek_session_state(session_key)
        value = _rs_state.conversation.reasoning_override if _rs_state is not None else None
        if isinstance(value, dict):
            return str(value.get("effort") or value.get("reasoning_effort") or "")
        return str(value or "")

    def _telegram_model_ui_context(self, event) -> Optional[dict[str, Any]]:
        """Return the live native-settings callback context, never untrusted inbound metadata."""
        try:
            adapter = self._delivery_adapter_for(event.source)
            getter = getattr(adapter, "_model_settings_ui", None)
            ui = getter() if callable(getter) else None
            callback_context = getattr(ui, "callback_context", None)
            context = callback_context(event) if callable(callback_context) else None
            return context if isinstance(context, dict) else None
        except Exception:
            logger.debug("Could not read the active Telegram model UI callback context", exc_info=True)
            return None

    @staticmethod
    def _model_ui_provider_row(context: Optional[dict[str, Any]], current_provider: str) -> dict[str, Any]:
        if not context:
            return {}
        rows = context.get("providers") or []
        if isinstance(rows, list):
            return next((row for row in rows if isinstance(row, dict)
                         and str(row.get("slug") or "").casefold() == current_provider.casefold()), {})
        return {}

    async def _send_model_settings(self, event, session_key: str, page: str = "sampling") -> Optional[str]:
        from gateway.model_ui import render_settings, settings_payload

        state = self._session_state(session_key).conversation
        ui_context = self._telegram_model_ui_context(event)
        cached_settings = (ui_context or {}).get("settings") or {}
        cached_dashboard = (ui_context or {}).get("dashboard") or {}
        cached_provider = str(cached_settings.get("current_provider") or
                              (ui_context or {}).get("current_provider") or "")
        if ui_context and (cached_settings or cached_dashboard):
            current_model = str(cached_settings.get("current_model") or
                                cached_dashboard.get("current_model") or
                                (ui_context or {}).get("current_model") or "")
            current_provider = str(cached_provider or cached_dashboard.get("current_provider") or "")
            current_row = cached_settings or self._model_ui_provider_row(ui_context, current_provider)
        else:
            providers, current_model, current_provider, _key, _home = \
                await self._provider_inventory_snapshot(event.source)
            current_row = next((row for row in providers if row.get("slug") == current_provider), {})
        payload = settings_payload(
            settings=dict(state.model_settings), reasoning_effort=self._model_settings_reasoning(session_key),
            page=page, supported_reasoning_efforts=(current_row.get("reasoning_options") or
                                                    current_row.get("reasoning_efforts")),
            reasoning_supported=current_row.get("reasoning_supported"),
        )
        payload["current_model"] = current_model
        payload["current_provider"] = current_provider
        payload["reasoning_supported"] = current_row.get("reasoning_supported")
        payload["reasoning_options"] = list(current_row.get("reasoning_options") or
                                                current_row.get("reasoning_efforts") or [])
        payload["settings"] = dict(state.model_settings)
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_model_settings", None)
        if callable(send_ui) and await send_ui(
            source=event.source, settings=payload, session_key=session_key,
            reply_to=self._reply_anchor_for_event(event),
            **_telegram_model_invocation(event),
        ):
            return None
        return render_settings(payload)

    async def _handle_msettings_command(self, event: MessageEvent) -> Optional[str]:
        """Open or update conversation-scoped direct model request settings.

        Native buttons dispatch the same deterministic argument forms accepted here, so retries and
        typed use share one validation/update path.
        """
        from gateway.model_ui import MODEL_SETTINGS, SETTINGS_PAGES, parse_setting_value
        from gateway.run import _hermes_home

        resolver = getattr(self, "_resolve_profile_home_for_source", None)
        profile_home = resolver(event.source) if callable(resolver) else None
        profile_home = profile_home or _hermes_home
        blocked = self._model_settings_owner_error(event.source, profile_home, allow_group=True)
        if blocked:
            return blocked
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        parts = (event.get_command_args() or "").strip().split()
        pages = {key: index + 1 for index, (key, _label) in enumerate(SETTINGS_PAGES)}
        if not parts:
            return await self._send_model_settings(event, session_key)
        action = parts[0].casefold()
        if action in {"back", "dashboard"}:
            providers, model, provider, key, home = await self._provider_inventory_snapshot(event.source)
            return await self._send_model_dashboard(event, providers, model, provider, key, home)
        if action == "page":
            if len(parts) != 2:
                return "Usage: `/msettings page <1-3|sampling|output|capabilities>`."
            selected = parts[1].casefold()
            if selected.isdecimal():
                number = int(selected)
                selected = next((name for name, index in pages.items() if index == number), "")
            if selected not in pages:
                return "Unknown settings page. Choose 1 (Sampling), 2 (Output), or 3 (Capabilities)."
            return await self._send_model_settings(event, session_key, selected)
        if action == "reset":
            return await self._handle_msettings_reset(event, session_key, parts)
        if action == "reasoning":
            if len(parts) != 2:
                return "Usage: `/msettings reasoning <none|low|medium|high|xhigh|reset>`."
            if parts[1].casefold() != "reset":
                ui_context = self._telegram_model_ui_context(event)
                cached_settings = (ui_context or {}).get("settings") or {}
                cached_dashboard = (ui_context or {}).get("dashboard") or {}
                current_provider = str(cached_settings.get("current_provider") or
                                       (ui_context or {}).get("current_provider") or
                                       cached_dashboard.get("current_provider") or "")
                current_row = cached_settings or self._model_ui_provider_row(ui_context, current_provider)
                if not ui_context:
                    providers, _model, current_provider, _key, _home = \
                        await self._provider_inventory_snapshot(event.source)
                    current_row = next((row for row in providers if row.get("slug") == current_provider), {})
                if current_row.get("reasoning_supported") is False:
                    return "The current model/provider does not support configurable reasoning effort."
                supported = current_row.get("reasoning_options") or current_row.get("reasoning_efforts") or []
                if supported and parts[1].casefold() not in supported and parts[1].casefold() != "none":
                    return "That reasoning effort is not supported by the current model. Choose: " + ", ".join(supported)
            result = self._apply_reasoning_selection(session_key, "telegram", parts[1], persist_global=False)
            return result
        if action == "set" and len(parts) == 3:
            setting, raw = parts[1].casefold(), parts[2]
        elif len(parts) == 2:
            setting, raw = action, parts[1]
        else:
            return "Usage: `/msettings set <setting> <value>` (for example `/msettings set temperature 0.7`)."
        try:
            value = parse_setting_value(setting, raw)
        except ValueError as exc:
            return str(exc)
        state = self._session_state(session_key).conversation
        state.model_settings[setting] = value
        self._evict_cached_agent(session_key)
        page = next(field.page for field in MODEL_SETTINGS if field.key == setting)
        return await self._send_model_settings(event, session_key, page)

    async def _handle_msettings_reset(self, event: MessageEvent, session_key: str,
                                      parts: list[str]) -> Optional[str]:
        """Apply a /msettings reset: one setting, reasoning, or all."""
        from gateway.model_ui import MODEL_SETTINGS

        if len(parts) != 2:
            return "Usage: `/msettings reset <temperature|top_p|frequency_penalty|presence_penalty|max_tokens|reasoning|all>`."
        setting = parts[1].casefold()
        state = self._session_state(session_key).conversation
        if setting == "all":
            state.model_settings.clear()
            self._set_session_reasoning_override(session_key, None)
        elif setting == "reasoning":
            self._set_session_reasoning_override(session_key, None)
        elif any(item.key == setting for item in MODEL_SETTINGS):
            state.model_settings.pop(setting, None)
        else:
            return f"Unsupported setting: `{setting}`."
        self._evict_cached_agent(session_key)
        page = next((field.page for field in MODEL_SETTINGS if field.key == setting), "sampling")
        return await self._send_model_settings(event, session_key, page)

    # ----------------------------------------------------------------- /model

    async def _perform_model_switch(
        self, ctx: _ModelSwitchContext, raw_input: str, explicit_provider, source
    ):
        """Resolve a /model switch off-loop. Returns ``(result, None)`` or ``(None, error_text)``."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import switch_model

        skew_error = _model_switch_skew_guard()
        if skew_error:
            return None, skew_error
        # Off-loop: switch_model() can hit a synchronous models.dev fetch (15s) on a cold cache.
        result = await asyncio.to_thread(
            switch_model, raw_input=raw_input, current_provider=ctx.current_provider,
            current_model=ctx.current_model, current_base_url=ctx.current_base_url,
            current_api_key=ctx.current_api_key, is_global=ctx.persist_global,
            explicit_provider=explicit_provider, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs,
        )
        if not result.success:
            return None, t("gateway.model.error_prefix", error=result.error_message)
        try:
            from hermes_cli.context_switch_guard import enrich_model_switch_warnings_for_gateway
            # Off-loop: merge_preflight_compression_warning() runs the sync provider probe ladder.
            await asyncio.to_thread(
                enrich_model_switch_warnings_for_gateway, result, self, session_key=ctx.session_key,
                source=source, custom_providers=ctx.custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)
        return result, None

    def _switch_cached_agent_model(self, result, ctx: _ModelSwitchContext, picker: bool) -> Optional[str]:
        """In-place swap on the cached agent; returns the error reply when it failed.

        The agent rolls back to the OLD model/client and re-raises; the commit (DB, override,
        eviction, config) is aborted so the next message doesn't rebuild a broken agent.
        """
        cached_agent = self._cached_agent_for(ctx.session_key)
        if cached_agent is None:
            return None
        try:
            cached_agent.switch_model(
                new_model=result.new_model, new_provider=result.target_provider,
                api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode,
                capabilities=getattr(result, "runtime_capabilities", None),
            )
        except Exception as exc:
            logger.warning(
                "%s model switch failed for cached agent: %s", "Picker" if picker else "In-place", exc
            )
            return t(
                "gateway.model.error_prefix",
                error=t("gateway.model.err_switch_failed", model=result.new_model, error=exc, current=ctx.current_model),
            )
        return None

    async def _record_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        """Persist a committed switch: session DB, next-turn note, config write-through, override map.

        Returns the warning for a ``--global`` switch whose ``config.yaml`` write or stale-override
        cleanup failed (the switch then stays a session override), else ``None``.
        """
        from hermes_cli.model_switch import format_model_for_display

        # Persist the new model to the session DB so the dashboard shows the updated model (#34850).
        _store = getattr(self, "persistence", None)
        if _store is not None:  # so the dashboard shows the updated model
            try:
                _sess_entry = await self.async_session_store.get_or_create_session(source)
                # Typed path: consume the auto-reset flag so the next message's cleanup does not
                # wipe the override stored below.
                if not picker and getattr(_sess_entry, "was_auto_reset", False):
                    # See #48031.
                    _sess_entry.was_auto_reset = False
                await _store.aio.sessions.update_model(
                    _sess_entry.session_id, result.new_model, provider=result.target_provider,
                    base_url=result.base_url, api_mode=result.api_mode,
                )
            except Exception as exc:
                logger.debug("Failed to persist model switch to DB: %s", exc)
        # Prepended to the next user message (no system messages mid-history). Display form strips
        # opaque Palantir RID prefixes; the override map keeps the full ID for the wire.
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[ctx.session_key] = (
            f"[Note: model was just switched from {format_model_for_display(ctx.current_model)} to "
            f"{format_model_for_display(result.new_model)} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        self._session_model_overrides[ctx.session_key] = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            # Catalog alias names carry no credential authority; only a direct alias endpoint does.
            "owns_credentials": result.alias_endpoint_applied,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        if one_turn:
            # A repeated --once before the turn runs must keep the EARLIEST snapshot: the later
            # command's snapshot is the first temporary model, not the user's standing override.
            self._claim_one_turn_restore(ctx.session_key, ctx.restore_snapshot)
        elif not picker and (_otr_state := self._peek_session_state(ctx.session_key)) is not None:
            _otr_state.conversation.one_turn_restore = None
        # A --global switch has ONE durable authority: config.yaml. Write it first; on success drop
        # the session override (memory + store) — a redundant copy would shadow every later global
        # change after a restart (#100314: a stale override resumed `gpt-5.6-sol-900k` as the base
        # 272K model). On failure keep the override so the switch truthfully survives as session-only.
        global_error: Optional[str] = None
        if ctx.persist_global:
            try:
                await _persist_model_switch_to_config(result, ctx.config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        # Precedence is session > channel_overrides > config.yaml: in a chat with a channel_overrides
        # model/provider the session override must stay, or the next turn runs the channel model.
        if ctx.persist_global and global_error is None and self._channel_override_for(source) is None:
            try:
                await self.async_session_store.set_model_override(ctx.session_key, None)
            except Exception as e:
                # Store still holds the stale copy: keep memory in agreement and report it (#100314).
                logger.warning("Failed to clear persisted session model override: %s", e)
                global_error = t("gateway.model.err_stale_override", error=e)
            else:
                self._session_model_overrides.pop(ctx.session_key, None)
        # Non-secret write-through so the override survives a restart (api_key/api_mode are
        # re-resolved on rehydration); a --once override must NOT outlive a restart.
        # Write-through the non-secret parts (model/provider/base_url) to the session store so the override
        # survives a gateway restart. api_key/api_mode are never persisted — they are re-resolved via
        # runtime provider resolution on rehydration. /model --once is intentionally EXCLUDED from the
        # write-through: a one-turn override must never survive a restart. The persisted value stays at the
        # pre-once state (the prior session override, or nothing), which is exactly what the finally-restore
        # reverts the in-memory dict to. (#29923 review defect: the original implementation wrote through,
        # so a crash before the restore rehydrated the once-model permanently.)
        elif not one_turn:
            try:
                await self.async_session_store.set_model_override(
                    ctx.session_key, self._session_model_overrides[ctx.session_key]
                )
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        self._evict_cached_agent(ctx.session_key)  # next turn builds fresh from the override
        return global_error

    async def _model_switch_confirmation(
        self, result, ctx: _ModelSwitchContext, *, one_turn: bool, picker: bool,
        global_error: Optional[str] = None,
    ) -> str:
        """Confirmation text with full metadata (display form shortens opaque Palantir IDs)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import (
            effective_context_pin, format_model_for_display, resolve_display_context_length_async)

        lines = [
            t("gateway.model.switched", model=format_model_for_display(result.new_model)),
            t("gateway.model.provider_label", provider=result.provider_label or result.target_provider),
        ]
        # Provider-aware chain: Codex OAuth, Nous and profile live-catalog caps win over the raw models.dev entry.
        mi = result.model_info
        model_cfg: dict = {}
        config_ctx = None
        with contextlib.suppress(Exception):  # fail-open on config read errors
            model_cfg = _load_gateway_config().get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("context_length") is not None:
                config_ctx = int(model_cfg["context_length"])
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        base_url = result.base_url or ctx.current_base_url or ""
        # The pin applies only while the route matches the config; the number and its label use the same answer.
        config_ctx = effective_context_pin(
            config_ctx, result.new_model, result.target_provider, base_url,
            model_cfg.get("default") or model_cfg.get("model"), model_cfg.get("provider"), model_cfg.get("base_url"))
        ctx_len = await resolve_display_context_length_async(
            result.new_model, result.target_provider, base_url=base_url,
            api_key=result.api_key or ctx.current_api_key or "", model_info=mi,
            custom_providers=ctx.custom_provs, config_context_length=config_ctx,
        )
        if ctx_len:
            from agent.context_pin import context_window_suffix
            lines.append(t("gateway.model.context_label", tokens=f"{ctx_len:,}") + context_window_suffix(
                ctx_len, config_ctx, model=result.new_model, provider=result.target_provider, base_url=base_url))
        if mi and mi.max_output:
            lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
        if mi:
            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
        openrouter_claude = (
            base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower()
        )
        if not picker and (openrouter_claude or result.api_mode == "anthropic_messages"):
            lines.append(t("gateway.model.prompt_caching_enabled"))
        if result.warning_message:
            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
        if getattr(result, "usage_notice", ""):
            lines.append(result.usage_notice)
        if ctx.persist_global and global_error is not None:
            # Never claim a clean global commit the disk did not take (#100314).
            lines.append(t("gateway.model.warning_prefix", warning=global_error))
            lines.append(t("gateway.model.session_only_hint"))
        elif ctx.persist_global:
            lines.append(t("gateway.model.saved_global"))
        elif one_turn:
            lines.append(t("gateway.model.next_turn_only"))
        else:
            lines.append(t("gateway.model.session_only_hint"))
        return "\n".join(lines)

    async def _commit_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool = False
    ) -> str:
        """Apply a resolved switch (cached agent, session, config) and build the confirmation; shared
        by the typed path and the picker callback (``picker=True`` never carries --once).

        Entry for the picker / cost-confirm callbacks, which fire outside ``_handle_model_command``
        and take the switch lock themselves; the typed path already holds it."""
        async with self._model_switch_lock():
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=picker)

    def _channel_override_for(self, source):
        """This chat's ``channel_overrides`` entry (model/provider), or None."""
        from gateway.run import _get_channel_override
        cfg = self._serving_channel_config(source) if source is not None else None
        if not cfg:
            return None
        return _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )

    def _model_switch_lock(self) -> asyncio.Lock:
        """Runner-wide lock over a /model command's read-resolve-commit. Slash commands bypass the busy
        guard while no agent runs, so a second /model on the same (or another) session otherwise
        interleaves with the first's store/config awaits: two ``--global`` picks left config.yaml on
        whichever thread wrote last and a ``--global`` cleanup wiped a session pick issued after it
        (#100314). Lazy: the runner is built without __init__ in tests."""
        lock = self.__dict__.get("_model_switch_lock_obj")
        if lock is None:
            lock = self.__dict__["_model_switch_lock_obj"] = asyncio.Lock()
        return lock

    def _record_switch_metrics(self, result, ctx: _ModelSwitchContext, source) -> None:
        """Slash dispatch does not install the routed profile's scope, so a multiplexed runner binds
        the owning home for the switch row and its switch_away friction."""
        from hermes_cli.observability.shared_metrics_events import record_model_switch
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with contextlib.suppress(Exception):
                home = self._resolve_profile_home_for_source(source)
        session_id = None
        with contextlib.suppress(Exception):
            session_id = getattr(self._cached_agent_for(ctx.session_key), "session_id", None)
        token = set_hermes_home_override(str(home)) if home else None
        try:
            record_model_switch(
                from_provider=ctx.route_provider, to_provider=result.target_provider, surface="gateway",
                from_model=ctx.current_model, session_id=session_id)
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    async def _commit_model_switch_locked(self, result, ctx: _ModelSwitchContext, *, source, picker: bool) -> str:
        one_turn = False if picker else ctx.one_turn
        from gateway.session_setting_commands import defer_local_model_pick, local_authority_live
        authority, live = local_authority_live(self, source)
        level = ""
        if live is not None:
            # A local session runs its frozen policy: the owner's canonical model mutation is the
            # only commit, landing at the next turn boundary (this command is the running turn).
            if one_turn:
                return t("gateway.model.error_prefix", error=t("gateway.model.err_once_local"))
            from hermes_constants import parse_reasoning_effort
            effort = (ctx.reasoning_effort or "").strip().lower()
            level = effort if parse_reasoning_effort(effort) is not None else ""
            await asyncio.to_thread(defer_local_model_pick, authority, live, result, level)
            self._record_switch_metrics(result, ctx, source)
            global_error = None
            if ctx.persist_global:
                try:
                    await _persist_model_switch_to_config(result, ctx.config_path)
                except Exception as e:
                    logger.warning("Failed to persist model switch", exc_info=True)
                    global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        else:
            error = self._switch_cached_agent_model(result, ctx, picker)
            if error is not None:
                return error
            self._record_switch_metrics(result, ctx, source)
            global_error = await self._record_model_switch(
                result, ctx, source=source, one_turn=one_turn, picker=picker)
        reply = await self._model_switch_confirmation(
            result, ctx, one_turn=one_turn, picker=picker, global_error=global_error,
        )
        if level:
            # Committed with the local pick: only the config write-through and the reply remain.
            saved = (ctx.persist_global and global_error is None
                     and self._save_gateway_config_key("agent.reasoning_effort", level))
            reply += "\n" + t("gateway.reasoning.set_global" if saved else "gateway.reasoning.set_session",
                              effort=level)
        elif ctx.reasoning_effort and not one_turn:
            # `/model X --reasoning <level>`: same applier as /reasoning, same scope as the pick.
            # The record step already evicted the cached agent, so the pin lands on the rebuild.
            from gateway.run import _platform_config_key
            reply += "\n" + self._apply_reasoning_selection(
                ctx.session_key, _platform_config_key(source.platform), ctx.reasoning_effort,
                persist_global=ctx.persist_global and global_error is None)
        return reply + ("\n" + t("gateway.model.local_next_turn") if live is not None else "")

    async def _send_model_picker(self, event: MessageEvent, source, adapter, session_key: str, listing_kwargs: dict, on_model_selected) -> bool:
        """Send the interactive /model picker; False when nothing was sent (text fallback). *source*
        is session-key-normalized so the picker's thread metadata lands where the next turn reads."""
        from hermes_cli.model_switch_providers import list_picker_providers
        # Uncapped: each adapter bounds its own rendering (Telegram pages, Discord 75, Slack 100) and
        # shows "N more" past it; a shared cap below those hid every model past #50 (#129260).
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(list_picker_providers, include_moa=True, **listing_kwargs)
        except Exception:
            providers = []
        if not providers:
            return False
        # Per-user sharing: never offer a provider whose credentials the
        # acting user may not use; the turn-time check is the backstop.
        from gateway.provider_sharing import provider_visible_to_user

        user_id = getattr(source, "user_id", None)
        providers = [row for row in providers
                     if provider_visible_to_user(str(row.get("slug") or ""), user_id)]
        if not providers:
            return False
        from gateway.approval_actor import opener_authority
        on_model_selected.prompt_authority = opener_authority(event)  # only its opener (or an admin) taps it
        picker_metadata = dict(self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)) or {})
        picker_metadata.update({"user_id": source.user_id, "chat_type": source.chat_type})
        result = await adapter.send_model_picker(
            chat_id=source.chat_id, providers=providers,
            current_model=listing_kwargs["current_model"],
            current_provider=listing_kwargs["current_provider"], session_key=session_key,
            on_model_selected=on_model_selected,
            metadata=picker_metadata,
        )
        return bool(result.success)

    async def _resolve_display_provider(self, profile_home) -> str:
        """Provider the ``/model`` listing should mark current when config.yaml has no ``model.provider``.

        The runtime resolver's auto-detect answer (env keys can out-rank a logged-in OAuth provider,
        #29285), never a placeholder: a funded OAuth login next to a dry API key must not display
        as the current route while the gateway bills the dry key (#136801). Off-loop — the detect
        reads config/env/auth store; under multiplex it runs inside the profile's scope so the
        env-key tier sees that profile's credentials."""
        from hermes_cli.auth import resolve_provider
        from hermes_cli.runtime_provider import resolve_requested_provider

        def _detect() -> str:
            # The turn's own request order (config, then HERMES_INFERENCE_PROVIDER, else auto),
            # resolved read-only: an env-pinned provider must be named, not auto-detect's pick.
            if profile_home is None:
                return resolve_provider(resolve_requested_provider())
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                return resolve_provider(resolve_requested_provider())

        with contextlib.suppress(Exception):
            return await asyncio.to_thread(_detect)
        return ""

    async def _model_listing_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home
    ) -> Optional[str]:
        """``/model`` with no args: interactive picker where supported, else the text list."""
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.providers import get_label

        # Unset, empty or ``auto`` ``model.provider``: ``ctx.current_provider`` holds a placeholder
        # (``openrouter``, ``auto``, ``""``) — display the provider the resolver routes through instead.
        display_provider = ctx.current_provider
        if str(ctx.route_provider or "").strip().lower() in ("", "auto"):
            display_provider = await self._resolve_display_provider(profile_home) or display_provider
        listing_kwargs = dict(
            current_provider=display_provider, current_base_url=ctx.current_base_url,
            current_model=ctx.current_model, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs, excluded_providers=ctx.excluded_provs,
            # Chat `/model` is a read path: catalogs come from the disk cache and stale ones warm
            # in the background, and only the selected custom endpoint is probed live, so one
            # degraded provider can't stall the reply (#74003). Mirrors the GUI read path.
            non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=True,
        )
        adapter = self._delivery_adapter_for(ctx.source)
        if adapter is not None and getattr(type(adapter), "send_model_picker", None) is not None:
            async def _picker_switch(model_id: str, provider_slug: str) -> str:
                # The picker callback binds the raw event source (pre-normalization).
                result, error = await self._perform_model_switch(ctx, model_id, provider_slug, event.source)
                if error is not None:
                    return error
                from gateway.provider_sharing import provider_visible_to_user

                if not provider_visible_to_user(result.target_provider, getattr(event.source, "user_id", None)):
                    return _provider_not_shared_reply(result.target_provider)
                return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

            async def _on_model_selected(_chat_id: str, model_id: str, provider_slug: str) -> str:
                if profile_home is None:
                    return await _picker_switch(model_id, provider_slug)
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(profile_home):
                    return await _picker_switch(model_id, provider_slug)

            if await self._send_model_picker(event, ctx.source, adapter, ctx.session_key, listing_kwargs, _on_model_selected):
                return None  # Picker sent — adapter handles the response

        lines = [t("gateway.model.current_label", model=ctx.current_model or t("gateway.shared.unknown_value"),
                   provider=get_label(display_provider)), ""]
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(list_authenticated_providers, max_models=_TEXT_LISTING_MODELS, **listing_kwargs)
            from gateway.provider_sharing import provider_visible_to_user

            providers = [row for row in providers
                         if provider_visible_to_user(str(row.get("slug") or ""),
                                                     getattr(ctx.source, "user_id", None))]
            lines.extend(_model_provider_listing_lines(providers))
        except Exception:
            pass
        lines.append(t("gateway.model.usage_switch_model"))
        lines.append(t("gateway.model.usage_switch_provider"))
        lines.append(t("gateway.model.usage_persist"))
        lines.append(t("gateway.model.usage_reset"))
        return "\n".join(lines)

    async def _model_selection_guard_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, result
    ) -> tuple[bool, Optional[str]]:
        """Selection-guard confirmation for the typed path (pickers confirm via their own UI).

        The unified registry (cost + data-policy guards) runs off-loop — pricing lookups may hit
        models.dev on a cache miss. Returns ``(fired, reply)``; the reply is None when the platform
        rendered confirm buttons itself.
        """
        try:
            from hermes_cli.model_selection_guards import (
                combined_selection_warning, selection_context_for_agent)
            warning = await asyncio.to_thread(
                combined_selection_warning, result.new_model, provider=result.target_provider,
                base_url=result.base_url or ctx.current_base_url or "",
                api_key=result.api_key or ctx.current_api_key or "", model_info=result.model_info,
                selection_context=selection_context_for_agent(self._cached_agent_for(ctx.session_key)),
            )
        except Exception:
            warning = None
        if warning is None:
            return False, None

        async def _on_cost_confirm(choice: str) -> str:
            if choice == "cancel":
                return t("gateway.model.switch_cancelled", model=ctx.current_model or t("gateway.shared.unknown_value"))
            # "once" and "always" both proceed — selection guards have no persistent opt-out.
            return await self._commit_model_switch(result, ctx, source=ctx.source)

        _p = self._typed_command_prefix_for(event.source.platform)
        message = t("gateway.model.guard_confirm", title=warning.title, message=warning.message, prefix=_p)
        return True, await self._request_slash_confirm(
            event=event, command="model", title=warning.title, message=message, handler=_on_cost_confirm,
        )

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model. Taken under the switch lock BEFORE the first await so
        concurrent commands commit in issue order (see ``_model_switch_lock``)."""
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

        raw_args = (event.get_command_args() or "").strip()
        if not raw_args:
            ui_context = self._telegram_model_ui_context(event)
            cached_dashboard = (ui_context or {}).get("dashboard") or {}
            cached_providers = (ui_context or {}).get("providers")
            cached_session = (ui_context or {}).get("_callback_session_key")
            if ui_context and isinstance(cached_providers, list) and cached_dashboard and cached_session:
                return await self._send_model_dashboard(
                    event, cached_providers,
                    str(cached_dashboard.get("current_model") or
                        (ui_context or {}).get("current_model") or ""),
                    str(cached_dashboard.get("current_provider") or
                        (ui_context or {}).get("current_provider") or ""),
                    str(cached_session),
                    (ui_context or {}).get("profile_home") or
                    (ui_context or {}).get("_callback_profile_home") or _hermes_home,
                )
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        request = parse_model_switch_args(raw_args)  # single-owner parser
        if request.errors:
            return request.error_messages()[0]  # gateway decoration over canonical copy
        if request.force_refresh:  # bust the disk cache so the picker shows live data
            with contextlib.suppress(Exception):
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
        # Normalize like a message turn (Telegram DM topic recovery) before deriving the override
        # key, so the override lands under the key the next turn reads.
        # Check for session override. See #30479.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        ctx = _ModelSwitchContext(
            # Gateway routing columns — forward ALL of them at CREATE time, same fix as the
            # compression-rotation bug in agent/conversation_compression.py. Without these, the branched
            # child row has NULL routing columns until switch_session() below calls
            # _record_gateway_session_peer() — a crash/kill anywhere between here and there (most plausibly
            # mid-history-copy, since each append_message call a few lines down is independently
            # best-effort) leaves the branch permanently unroutable: unreachable by chat/thread lookup, and
            # unreachable via /resume's IDOR guard too (which requires the row's chat_id/thread_id to match
            # the caller's). user_id is critical for the fallback lookup path (hermes_state.py:1994-2009)
            # that searches by the complete peer tuple when session_key doesn't match. origin_json and
            # display_name complete the identity (same shape as the reset path's db_create_kwargs in
            # gateway/session.py, #82633) so consumers that read routing/presentation data from state.db
            # (the mcp-serve plugin, mirror, channel directory) see the branch row fully formed with zero backfill gap.
            session_key=session_key,
            source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml",
            persist_global=resolve_persist_behavior(
                request.is_global, request.is_session, is_once=request.is_once,
                explicit_provider=request.explicit_provider,
            ),
            one_turn=request.is_once,
            reasoning_effort=request.reasoning_effort,
            restore_snapshot=self._snapshot_session_model_override(session_key) if request.is_once else None,
        )
        ctx.read_config()
        if request.is_reset:
            return await self._handle_model_reset(event, ctx)
        # Turn precedence: session /model > channel_overrides > config.yaml.
        # The channel's full route as the turn resolves it, so a config-backed endpoint (custom) survives.
        from gateway.session_info_route import channel_override_route
        _ch_model, _ch_route, _ch_forced = await asyncio.to_thread(lambda: channel_override_route(
            self._serving_channel_config(source) if source is not None else None, source))
        if _ch_model:
            ctx.apply_override({"model": _ch_model, **(_ch_route or ({"provider": _ch_forced} if _ch_forced else {}))})
        ctx.apply_override(self._session_model_overrides.get(session_key, {}))
        if raw_args.casefold() == "providers":
            return await self._handle_providers_command(dataclasses.replace(event, text="/providers"))
        if raw_args.casefold() in {"list", "change"}:
            return await self._handle_models_command(dataclasses.replace(event, text="/models"))
        # The profile-wide default is changeable by any admitted user; an
        # implicit persist (fresh install, persist_switch_by_default) still
        # degrades to a session-only switch when the route cannot persist.
        if ctx.persist_global and not self._model_route_owner(source, profile_home or _hermes_home):
            if request.is_global:
                return ("Only the profile owner can change the profile-wide default model. "
                        "Run /model without --global to switch this session only.")
            ctx.persist_global = False
        if not request.target and not request.explicit_provider:
            # Upstream routes a bare /model to the interactive picker
            # (_model_listing_reply); the fork's dashboard is route/settings-first
            # and wins only where the adapter actually renders it. When the
            # adapter has no send_model_dashboard, _send_model_dashboard would
            # only fall back to render_dashboard text, so fall through to the
            # picker (or its text listing) instead.
            adapter = self._delivery_adapter_for(event.source)
            if callable(getattr(adapter, "send_model_dashboard", None)):
                providers, current_model, current_provider, dashboard_key, dashboard_home = \
                    await self._provider_inventory_snapshot(source)
                return await self._send_model_dashboard(
                    event, providers, current_model, current_provider, dashboard_key, dashboard_home,
                )
            return await self._model_listing_reply(event, ctx, profile_home)
        result, error = await self._perform_model_switch(ctx, request.target, request.explicit_provider, source)
        if error is not None:
            return error
        from gateway.provider_sharing import provider_visible_to_user

        if not provider_visible_to_user(result.target_provider, getattr(source, "user_id", None)):
            return _provider_not_shared_reply(result.target_provider)
        guard_fired, guard_reply = await self._model_selection_guard_reply(event, ctx, result)
        if guard_fired:
            return guard_reply
        return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)

    def _channel_default_route(self, source, config_path) -> tuple[str, str]:
        """``(model, provider)`` this chat runs with no session override: channel_overrides, else the
        owning profile's config.yaml (read from *config_path*: slash dispatch runs outside the routed
        profile's scope on a multiplexed runner)."""
        from gateway.run import _load_gateway_config, _resolve_gateway_model
        cfg = _load_gateway_config(config_path=config_path) or {}
        model_cfg = cfg.get("model")
        provider = str(model_cfg.get("provider") or "") if isinstance(model_cfg, dict) else ""
        channel = self._channel_override_for(source)
        if channel is not None and channel.model:
            return channel.model, channel.provider or ""
        return _resolve_gateway_model(cfg), "" if provider.lower() == "auto" else provider

    async def _handle_model_reset(self, event: MessageEvent, ctx: _ModelSwitchContext) -> Optional[str]:
        """``/model reset``: drop this session's /model override (memory, store and any armed
        ``--once`` restore) so the next turn resolves channel_overrides > config.yaml again. The
        transcript is kept; before this the only way off a persisted override was ``/new``."""
        source, session_key = ctx.source, ctx.session_key
        default_model, default_provider = await asyncio.to_thread(
            self._channel_default_route, source, ctx.config_path)
        from gateway.session_setting_commands import local_authority_live
        if local_authority_live(self, source)[1] is not None:
            # A local session runs a frozen policy with no override tier: reset is a session-scoped
            # pick of the configured default, committed by the owner like any typed /model.
            ctx.persist_global = False
            result, error = await self._perform_model_switch(ctx, default_model, default_provider, source)
            if error is not None:
                return error
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)
        # A restart leaves the persisted override unloaded until first use: load it, or the reply
        # says "none active" while the store resurrects it on the next message.
        await asyncio.to_thread(self._rehydrate_session_model_override, session_key)
        previous = self._session_model_overrides.pop(session_key, None)
        if (_otr_state := self._peek_session_state(session_key)) is not None:
            _otr_state.conversation.one_turn_restore = None
        try:
            await self.async_session_store.set_model_override(session_key, None)
        except Exception:
            logger.warning("Failed to clear persisted session model override", exc_info=True)
        shown = default_model or t("gateway.shared.unknown_value")
        if previous is None:
            return t("gateway.model.reset_none", model=shown)
        from hermes_cli.model_switch import format_model_for_display
        _store = getattr(self, "persistence", None)
        if _store is not None:  # the dashboard's session-model column (#34850)
            try:
                entry = await self.async_session_store.get_or_create_session(source)
                await _store.aio.sessions.update_model(entry.session_id, default_model, provider=default_provider or None)
            except Exception:
                logger.debug("Failed to persist model reset to DB", exc_info=True)
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[session_key] = (
            f"[Note: the session model override was cleared; model changed from "
            f"{format_model_for_display(previous.get('model') or '')} back to the default "
            f"{format_model_for_display(default_model)}. Adjust your self-identification accordingly.]")
        self._evict_cached_agent(session_key)  # next turn builds from the default route
        return t("gateway.model.reset_done", model=format_model_for_display(shown))

    # -------------------------------------------------- usage-window limit: one-tap switch

    def _schedule_usage_limit_offer(self, event: MessageEvent, limit: dict, *, session_key: str = "",
                                    generation: Optional[int] = None) -> None:
        """After a usage-window refusal's reply: offer the plan's recommended model as a button where the
        platform already has quick-choice buttons, else one line of text.

        Runs as the adapter's post-delivery callback, so the offer always lands under the reply (the reply
        is sent after this turn returns). The tap switches this session only (never the default) and
        re-sends the refused message, like /retry. No credits button in a chat: anyone in the room could
        spend the owner's credits, so the Portal is named instead. Hermes never switches on its own."""
        try:
            adapter = self._delivery_adapter_for(event.source) if event.source is not None else None
            register = getattr(adapter, "register_post_delivery_callback", None)
            if session_key and callable(register):
                register(session_key, lambda: self._offer_usage_limit_switch(event, limit, generation),
                         generation=generation)
                return
            self._track_background_task(self._offer_usage_limit_switch(event, limit, generation))
        except Exception:
            logger.debug("usage-limit offer scheduling failed", exc_info=True)

    async def _offer_usage_limit_switch(self, event: MessageEvent, limit: dict,
                                        generation: Optional[int] = None) -> None:
        from agent.plan_limits import limit_next_steps_from_payload
        try:
            suggestions = [s for s in (limit.get("suggestions") or []) if isinstance(s, dict) and s.get("model")]
            source = event.source
            if source is None:
                return
            prefix = self._typed_command_prefix_for(source.platform)
            adapter = self._delivery_adapter_for(source)
            session_key = self._session_key_for_source(source)
            if suggestions and adapter is not None and getattr(type(adapter), "send_choice_picker", None) is not None:
                async def _on_choice(_chat_id: str, value: str) -> str:
                    if generation is not None and self._usage_limit_offer_is_stale(session_key, generation):
                        # A newer turn ran since the refusal: /retry would re-run that one, not the refused one.
                        return t("platform.discord.picker.choice_expired")
                    return await self._accept_usage_limit_switch(
                        event, value, offer=None if generation is None else (session_key, generation))

                choices = [{"value": str(s["model"]),
                            "label": t("gateway.usage_limit.switch_choice", model=str(s.get("label") or s["model"]),
                                       note=str(s.get("note") or "")),
                            "is_current": False} for s in suggestions]
                if await self._try_send_choice_picker(
                        event, session_key, title=t("gateway.usage_limit.picker_title"), choices=choices,
                        on_choice_selected=_on_choice):
                    return
            text = limit_next_steps_from_payload(limit, model_command=f"{prefix}model", topup_command=None, portal=True)
            if adapter is not None and text:
                await adapter.send(source.chat_id, text, metadata=self._reply_metadata(event))
        except Exception:
            logger.debug("usage-limit offer failed", exc_info=True)

    def _usage_limit_offer_is_stale(self, session_key: str, generation: int) -> bool:
        """True once the session has started a newer run than the refused one. Fails open (not stale)."""
        try:
            return int(self._session_state(session_key).persistent.run_generation) != int(generation)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _accept_usage_limit_switch(self, event: MessageEvent, model_id: str, *,
                                         offer: Optional[tuple[str, int]] = None) -> str:
        """The tap: switch this session to *model_id* on Nous, then re-send the refused message.
        ``offer`` is the refused turn's ``(session_key, run_generation)``."""
        from gateway.run import _hermes_home
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        ctx = _ModelSwitchContext(session_key=session_key, source=source,
                                  config_path=(profile_home or _hermes_home) / "config.yaml", persist_global=False)

        async def _switch() -> str:
            ctx.read_config()
            ctx.apply_override(self._session_model_overrides.get(session_key, {}))
            result, error = await self._perform_model_switch(ctx, model_id, "nous", event.source)
            if error is not None:
                return error
            return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

        before = self._session_model_overrides.get(session_key)
        if profile_home is None:
            reply = await _switch()
        else:
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                reply = await _switch()
        after = self._session_model_overrides.get(session_key)
        switched = after is not None and after is not before and after.get("provider") == "nous"
        if switched:
            # Only after the switch landed (an error reply leaves the override unchanged). Exactly a typed /retry from the same sender (rewinds the refused turn, runs the message
            # again, and the adapter delivers the reply). Called directly, the re-run's reply would be
            # returned to nobody.
            self._track_background_task(self._dispatch_usage_limit_retry(event, offer))
        return reply

    async def _dispatch_usage_limit_retry(self, event: MessageEvent, offer: Optional[tuple[str, int]] = None) -> None:
        from gateway.platforms.event import MessageType
        adapter = self._delivery_adapter_for(event.source)
        # Re-checked here, with no await before the /retry: the switch awaited model resolution, and a
        # turn that ran meanwhile is what /retry would rewind. The switch itself stands.
        if adapter is None or (offer is not None and self._usage_limit_offer_is_stale(*offer)):
            return
        try:
            await adapter.handle_message(MessageEvent(
                text="/retry", message_type=MessageType.TEXT, source=event.source,
                raw_message=event.raw_message, channel_prompt=event.channel_prompt))
        except Exception:
            logger.debug("usage-limit retry dispatch failed", exc_info=True)

    # -------------------------------------------------- /codex-runtime, /personality

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str | None:
        """Handle /codex-runtime; a real change evicts the cached agent so the new api_mode applies
        on the next message (avoids prompt-cache invalidation mid-session)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Codex runtime", text=text):
                return None
            return text
        from hermes_cli import codex_runtime_switch as crs

        new_value, errors = crs.parse_args(event.get_command_args().strip() if event else "")
        if errors:
            return await _reply("\n".join(f"- {error}" for error in errors))
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return await _reply(t("gateway.codex_runtime.config_load_failed", error=exc))
        result = crs.apply(
            load_config(), new_value, persist_callback=(save_config if new_value is not None else None),
        )
        if result.success and new_value is not None and result.requires_new_session:
            try:
                self._evict_cached_agent(self._session_key_for_source(event.source))
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change", exc_info=True)
        if new_value is None:
            status = "Current"
        elif not result.success:
            status = "Failed"
        elif result.new_value == result.old_value:
            status = "Unchanged"
        else:
            status = "Changed"
        lines = [f"- Status: `{status}`", ""]
        if result.message:
            lines.extend(result.message.split("\n"))
        return await _reply("\n".join(lines))

    async def _handle_personality_command(self, event: MessageEvent) -> str | None:
        """Handle /personality — list or set a personality (hermes_cli.personality owns the state)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Personality", text=text):
                return None
            return text
        from gateway.run import _load_gateway_config
        from hermes_cli.personality import (
            active_personality_name,
            available_personalities,
            describe_personality,
            persist_personality,
            resolve_personality,
        )

        args = event.get_command_args().strip()
        try:
            config = _load_gateway_config()
        except Exception:
            config = {}
        personalities = available_personalities(config)
        if not args:
            current = active_personality_name(config)
            # Selected first (user rule), the rest in configured order; the
            # no-overlay entry is current when no personality is active.
            entries = [("none", "(no personality overlay)", not current)]
            entries.extend(
                (name, describe_personality(prompt), name == current)
                for name, prompt in personalities.items()
            )
            entries.sort(key=lambda entry: not entry[2])
            lines = ["# Available personalities"]
            for name, preview, is_current in entries:
                marker = " ✓" if is_current else ""
                lines.append(f"- `{name}{marker}` — {preview}")
            lines.append("")
            lines.append(t("gateway.personality.usage").strip())
            return await _reply("\n".join(lines))
        try:
            name, _new_prompt = resolve_personality(args, config)
        except ValueError:
            available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
            return await _reply(t("gateway.personality.unknown", name=args.lower(), available=available))
        # Persists the selection only (never agent.system_prompt, a user-owned overlay) into the
        # routed profile's config.yaml; the next turn re-resolves the prompt — no process-global state.
        if not persist_personality(name):
            return await _reply(t("gateway.personality.save_failed", error=t("gateway.personality.err_config_write")))
        if not name:
            return await _reply(t("gateway.personality.cleared"))
        return await _reply(t("gateway.personality.set_to", name=name))

    # ----------------------------------------------------------- /reasoning, /fast

    def _save_gateway_config_key(self, key_path: str, value) -> bool:
        """Save a dot-separated key to config.yaml (shared by /reasoning, /fast and their pickers)."""
        from gateway.slash_commands import _nested_dict
        from gateway.run import _gateway_config_home
        from hermes_cli.config import read_user_config_raw
        config_path = _gateway_config_home() / "config.yaml"
        try:
            user_config = read_user_config_raw(config_path)  # raw: never persist merged defaults
            *parents, leaf = key_path.split(".")
            _nested_dict(user_config, *parents)[leaf] = value
            atomic_config_write(config_path, user_config)
            return True
        except Exception as e:
            logger.error("Failed to save config key %s: %s", key_path, e)
            return False

    def _set_reasoning_override(self, session_key: str, value) -> None:
        """Store (or clear with None) the session reasoning override and drop the cached agent."""
        self._set_session_reasoning_override(session_key, value)
        self._evict_cached_agent(session_key)

    def _apply_reasoning_selection(
        self, session_key: str, platform_key: str, value: str, persist_global: bool = False,
        card_out: list | None = None,
    ) -> str:
        """Apply a /reasoning argument (typed or picked) and return the reply.

        The plain-text reply is always returned: the picker, /model
        --reasoning and /msettings reasoning callers rely on it. When
        *card_out* is given, the rich card for this variant is appended to it
        as HTML — the caller sends it via :meth:`_send_telegram_command_card`
        and returns None instead.
        """
        from hermes_constants import parse_reasoning_effort

        value = (value or "").strip().lower()
        from gateway.session_setting_commands import local_live_for_route
        live = local_live_for_route(self, session_key)
        show = _REASONING_DISPLAY_TOGGLES.get(value)
        plain = ""
        card = None
        if show is not None:  # per-platform display toggle
            self._show_reasoning = show
            if live is not None:
                live.show_reasoning = show  # what this local session's viewers stream (config.get show_reasoning)
            self._save_gateway_config_key(f"display.platforms.{platform_key}.show_reasoning", show)
            key = "gateway.reasoning.display_set_on" if show else "gateway.reasoning.display_set_off"
            plain = t(key, platform=platform_key)
            card = _reasoning_display_card_html(show, platform_key)
        elif value == "reset":
            if persist_global:
                plain = t("gateway.reasoning.reset_global_unsupported")
                card = _reasoning_reset_global_unsupported_card_html()
            else:
                self._set_session_reasoning_override(session_key, None)
                self._reasoning_config = self._load_reasoning_config()
                self._evict_cached_agent(session_key)
                plain = t("gateway.reasoning.reset_done")
                card = _reasoning_reset_card_html()
        else:
            parsed = parse_reasoning_effort(value)
            if parsed is None:
                plain = t("gateway.reasoning.unknown_arg", arg=value)
                card = _reasoning_unknown_arg_card_html(value, self._reasoning_current_effort())
            elif persist_global:
                if self._save_gateway_config_key("agent.reasoning_effort", value):
                    # A local session runs its frozen level, not config.yaml's: it takes the level too.
                    self._set_reasoning_override(session_key, parsed if live is not None else None)
                    plain = t("gateway.reasoning.set_global", effort=value)
                    card = _reasoning_set_card_html(
                        value, t("gateway.reasoning.scope_global"),
                        "gateway.reasoning.card_scope_global_note")
                else:
                    self._set_reasoning_override(session_key, parsed)
                    plain = t("gateway.reasoning.set_global_save_failed", effort=value)
                    card = _reasoning_set_card_html(
                        value, t("gateway.reasoning.scope_session"),
                        "gateway.reasoning.card_scope_save_failed_note")
            else:
                self._set_reasoning_override(session_key, parsed)
                plain = t("gateway.reasoning.set_session", effort=value)
                card = _reasoning_set_card_html(
                    value, t("gateway.reasoning.scope_session"),
                    "gateway.reasoning.card_scope_session_note")
        if card_out is not None and card is not None:
            card_out.append(card)
        return plain

    def _reasoning_current_effort(self) -> str:
        """Current effort level for the unknown-arg card (selected-first)."""
        rc = getattr(self, "_reasoning_config", None) or {}
        if rc.get("enabled") is False:
            return "none"
        return str(rc.get("effort") or "medium")

    async def _try_send_choice_picker(
        self, event: MessageEvent, session_key: str, title: str, choices: list, on_choice_selected,
    ) -> bool:
        """Send an interactive choice picker when the adapter *type* supports it (the /model gate);
        a failed send returns False (text fallback) instead of erroring."""
        adapter = self._delivery_adapter_for(event.source)
        if adapter is None or getattr(type(adapter), "send_choice_picker", None) is None:
            return False
        from gateway.approval_actor import opener_authority
        on_choice_selected.prompt_authority = opener_authority(event)  # only its opener (or an admin) taps it
        try:
            result = await adapter.send_choice_picker(
                chat_id=event.source.chat_id, title=title, choices=choices, session_key=session_key,
                on_choice_selected=on_choice_selected, metadata=self._reply_metadata(event),
            )
            return bool(getattr(result, "success", False))
        except Exception as e:
            logger.warning("send_choice_picker failed, falling back to text: %s", e)
            return False

    async def _handle_reasoning_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reasoning command — manage reasoning effort and display toggle."""
        from gateway.run import _platform_config_key
        from hermes_constants import VALID_REASONING_EFFORTS

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        # Normalize (Telegram DM topic recovery) so the override key matches the next turn's.
        # See #30479.
        _reasoning_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        from gateway.session_setting_commands import local_live
        if (_live := local_live(self, event.source)) is not None:
            self._show_reasoning = getattr(_live, "show_reasoning", None) is True
        # Effective model (session /model override wins) so per-model reasoning_overrides display.
        _session_model = str(
            ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}).get("model") or ""
        )
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source, session_key=session_key, model=_session_model,
        )
        platform_key = _platform_config_key(event.source.platform)
        if args:  # typed path — same applier the picker uses
            card_out: list[str] = []
            reply = self._apply_reasoning_selection(
                session_key, platform_key, args, persist_global=persist_global, card_out=card_out)
            if card_out and await self._send_telegram_command_card(
                    event, title=t("gateway.reasoning.card_title"), html=card_out[0]):
                return None  # Card sent — runner sends nothing else
            return reply
        rc = self._reasoning_config
        # Labels tell the truth about the route: a Hermes-internal step (``ultra``) that the wire
        # clamps is shown as "ultra (sends max on this route)" instead of a distinct level (#61634).
        from agent.reasoning_effort import effort_display_label
        from gateway.run import _load_gateway_config
        from hermes_cli.codex_runtime_switch import get_current_runtime
        _session_route = ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {})
        _model_cfg = {}
        with contextlib.suppress(Exception):  # fail-open on config read errors, like /model does
            _model_cfg = _load_gateway_config(config_path=self.config_path).get("model", {}) or {}
        _route = (
            _session_route.get("provider") or _model_cfg.get("provider"),
            _session_model or _model_cfg.get("default") or _model_cfg.get("model"),
            get_current_runtime({"model": _model_cfg}),
        )
        if rc is None:
            level, current_effort = t("gateway.reasoning.level_default"), "medium"
        elif rc.get("enabled") is False:
            level, current_effort = t("gateway.reasoning.level_disabled"), "none"
        else:
            current_effort = rc.get("effort", "medium")
            level = effort_display_label(current_effort, *_route)
        display_state = t("gateway.reasoning.display_on") if self._show_reasoning else t("gateway.reasoning.display_off")
        _rs_state = self._peek_session_state(session_key)
        has_session_override = _rs_state is not None and _rs_state.conversation.reasoning_override is not None
        scope = t("gateway.reasoning.scope_session") if has_session_override else t("gateway.reasoning.scope_global")

        async def _on_reasoning_choice(_chat_id: str, value: str) -> str:
            def _apply() -> str:
                return self._apply_reasoning_selection(
                    session_key, platform_key, value, persist_global=persist_global,
                )

            if profile_home is None:
                return _apply()
            # Picker taps arrive after the command's profile scope has exited.
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                return _apply()

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.reasoning.picker_title", level=level, scope=scope, display=display_state),
            choices=[
                {"value": "none", "label": t("gateway.reasoning.choice_none"), "is_current": current_effort == "none"},
                *({"value": lv, "label": effort_display_label(lv, *_route), "is_current": lv == current_effort}
                  for lv in VALID_REASONING_EFFORTS),
                *({"value": v, "label": t(f"gateway.reasoning.choice_{v}"), "is_current": False}
                  # `reset` clears the session override, which --global refuses as unsupported.
                  for v in (("show", "hide") if persist_global else ("reset", "show", "hide"))),
            ],
            on_choice_selected=_on_reasoning_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        if await self._send_telegram_command_card(
                event, title=t("gateway.reasoning.card_title"),
                html=_reasoning_settings_card_html(level, scope, display_state)):
            return None  # Card sent — runner sends nothing else
        return "\n".join([
            "# Reasoning settings",
            "",
            f"- Effort: `{level}`",
            f"- Scope: {scope}",
            f"- Display: `{display_state}`",
            "",
            "*Usage:* `/reasoning <none|minimal|low|medium|high|xhigh|max|ultra|reset|show|hide> [--global]`",
        ])

    async def _handle_draft_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /draft — owner-only, DM-only showcase of the draft system.

        Forty-one short beats, in order: the ResponseStreamDraft surface
        (topic 1, eight messages), a live draft sequence (topic 2, four
        beats), rich messages plus streaming response (topic 3, three),
        the four draft actions (topic 4, four), the draft mechanism
        (topic 5, two), temporary reasoning (topic 6, one live beat),
        every other typing action Telegram supports (topic 8, sixteen
        live beats), and Stop / shown-once / fail-open (topic 7, three).
        Topic 8 runs before the closing topic 7 so the Usage-bearing
        final beat stays last. Every beat first shows a draft preview
        naming the next message (``preview_next``), then optionally runs
        one live demo step, then — when the beat ran a live animation —
        sends one real persist message carrying that animation's content
        (label on its own first line, then the content), and then sends
        one short message — a title plus at most three lines. Every
        animation holds 10 s or more (raw typing actions are re-sent
        every 4 s inside the hold, since one send expires in seconds),
        so a full run takes about four and a half minutes. Non-owner,
        non-DM, or non-Telegram callers are denied silently: nothing is
        sent. The live steps run through ``gateway.draft_demo`` (lazy
        import, so an absent demo module never breaks the handler); a
        preview, demo step or persist send returning False aborts the
        rest of the sequence. Only the final beat carries the Usage line
        (the single copy pill).
        """
        from gateway.slash_commands_model_draft import (
            _draft_html_to_text,
            _draft_lines_html,
            _draft_members_html,
        )

        source = getattr(event, "source", None)
        if source is None:
            return None
        try:
            platform = str(getattr(source.platform, "value", source.platform)).lower()
        except Exception:
            return None
        if platform != "telegram":
            return None
        if str(getattr(source, "chat_type", "")) != "dm":
            return None
        try:
            adapter = self._delivery_adapter_for(source)
        except Exception:
            return None
        if adapter is None or not _draft_is_owner_actor(self, adapter, source):
            return None
        try:
            from gateway.draft_demo import (
                DEMO_CONTENT_TOPIC2,
                DEMO_FINAL_TOPIC2,
                DEMO_REASONING_TOPIC2,
                DEMO_REASONING_TOPIC6,
                preview_next,
                send_animation_message,
                show_action,
                show_content,
                show_placeholder,
                show_reasoning,
                show_stop_draft,
                show_sync_and_final,
            )
        except Exception:
            return None

        fallback: list[str] = []

        def _finish() -> Optional[str]:
            return "\n\n".join(fallback) or None

        async def _send_static(title_key: str, html: str) -> None:
            if await self._send_telegram_command_card(
                    event, title=t(title_key), html=html):
                return
            fallback.append(_draft_html_to_text(html))

        async def _run_step(step: Any, *args: Any) -> bool:
            try:
                return bool(await step(*args))
            except Exception:
                logger.debug("draft demo step failed; aborting /draft sequence", exc_info=True)
                return False

        async def _preview(label: str) -> bool:
            return await _run_step(preview_next, adapter, source, f"Next: {label}…")

        async def _beat(label: str, title_key: str, html: str, *,
                        demo: Any = None, demo_args: tuple[Any, ...] = (),
                        persist: Optional[str] = None) -> bool:
            if not await _preview(label):
                return False
            if demo is not None and not await _run_step(demo, adapter, source, *demo_args):
                return False
            if persist is not None and not await _run_step(
                    send_animation_message, adapter, source, persist):
                return False
            await _send_static(title_key, html)
            return True

        for label, title_key, names in _DRAFT_TOPIC1_BEATS:
            if not await _beat(label, title_key, _draft_members_html(title_key, names)):
                return _finish()
        if not await _beat(
                "the thinking placeholder", "gateway.draft.beat_t2_placeholder_title",
                _draft_lines_html("gateway.draft.beat_t2_placeholder_title", (
                    "Static “Thinking” placeholder draft, not a message.",
                    "Covers the gap before reasoning arrives.",
                )), demo=show_placeholder,
                persist="Placeholder\nThinking"):
            return _finish()
        if not await _beat(
                "the reasoning draft", "gateway.draft.beat_t2_reasoning_title",
                _draft_lines_html("gateway.draft.beat_t2_reasoning_title", (
                    "Thinking block, reasoning in italic.",
                    "Frozen in place once content flows.",
                )), demo=show_reasoning, demo_args=(DEMO_REASONING_TOPIC2,),
                persist=f"Reasoning\n{DEMO_REASONING_TOPIC2}"):
            return _finish()
        if not await _beat(
                "the content draft", "gateway.draft.beat_t2_content_title",
                _draft_lines_html("gateway.draft.beat_t2_content_title", (
                    "Streaming content under the frozen reasoning.",
                    "Still a draft — nothing sent yet.",
                )), demo=show_content, demo_args=(DEMO_CONTENT_TOPIC2,),
                persist=f"Content\n{DEMO_CONTENT_TOPIC2}"):
            return _finish()
        if not await _preview("the final message"):
            return _finish()
        if not await _run_step(show_sync_and_final, adapter, source, DEMO_FINAL_TOPIC2):
            return _finish()
        for label, title_key, lines in _DRAFT_TOPIC3_BEATS:
            if not await _beat(label, title_key, _draft_lines_html(title_key, lines)):
                return _finish()
        for label, title_key, lines in _DRAFT_TOPIC4_BEATS:
            if not await _beat(label, title_key, _draft_lines_html(title_key, lines)):
                return _finish()
        for label, title_key, lines in _DRAFT_TOPIC5_BEATS:
            if not await _beat(label, title_key, _draft_lines_html(title_key, lines)):
                return _finish()
        if not await _beat(
                "temporary reasoning", "gateway.draft.beat_t6_temp_title",
                _draft_lines_html("gateway.draft.beat_t6_temp_title", (
                    "Shown in the draft, gone with the draft.",
                    "It never reaches a final message.",
                    "No reasoning in the final when streaming is on.",
                )), demo=show_reasoning, demo_args=(DEMO_REASONING_TOPIC6,),
                persist=f"Temporary reasoning\n{DEMO_REASONING_TOPIC6}"):
            return _finish()
        for label, title_key, lines, action_name, persist in _DRAFT_TOPIC8_BEATS:
            if not await _beat(label, title_key, _draft_lines_html(title_key, lines),
                               demo=show_action, demo_args=(action_name,),
                               persist=persist):
                return _finish()
        if not await _beat(
                "stopping the draft", "gateway.draft.beat_t8_stop_draft_title",
                _draft_lines_html("gateway.draft.beat_t8_stop_draft_title", (
                    "Clears the demo reasoning draft — <code>SendMessageStopDraftAction</code>",
                    "Sent with this sink's <code>random_id</code>.",
                    "The draft vanishes; nothing is sent.",
                )), demo=show_stop_draft,
                persist=f"Stop draft\n{DEMO_REASONING_TOPIC2}"):
            return _finish()
        for label, title_key, lines in _DRAFT_TOPIC7_BEATS[:-1]:
            if not await _beat(label, title_key, _draft_lines_html(title_key, lines)):
                return _finish()
        label, title_key, lines = _DRAFT_TOPIC7_BEATS[-1]
        if not await _beat(label, title_key,
                           _draft_lines_html(title_key, lines, with_usage=True)):
            return _finish()
        return _finish()


    def _apply_fast_selection(self, session_key: str, value: str, persist: bool = False) -> str:
        """Apply a /fast argument (typed or picked) and return the reply."""
        selection = _FAST_SELECTIONS.get(value)
        if selection is None:
            return t("gateway.fast.unknown_arg", arg=value)
        tier, saved_value, label_key = selection
        label = t(label_key) if label_key else value.upper()
        self._service_tier = tier
        from gateway.session_setting_commands import local_live_for_route
        if persist and self._save_gateway_config_key("agent.service_tier", saved_value):
            if local_live_for_route(self, session_key) is not None:
                # A local session runs its frozen tier, not config.yaml's: it takes the tier too.
                self._set_session_service_tier_override(session_key, tier)
            else:
                self._set_session_service_tier_override(session_key, None, clear=True)  # global wins
            self._evict_cached_agent(session_key)
            return t("gateway.fast.saved", label=label)
        # Session override — also the fallback after a failed config write (as /reasoning --global).
        self._set_session_service_tier_override(session_key, tier)
        self._evict_cached_agent(session_key)
        return t("gateway.fast.session_only", label=label)

    async def _handle_fast_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /fast — the CLI Priority Processing toggle; session-scoped unless ``--global``
        (persists agent.service_tier, parity with /model)."""
        from agent.fast_mode import FAST_ARG_ALIASES, service_tier_word
        from gateway.run import _load_gateway_config, _resolve_gateway_model

        # The /reasoning parser strips --global (any position) and normalizes unicode dashes.
        args, persist_global = self._parse_reasoning_command_args(event.get_command_args().strip().lower())
        # Normalized like /model and /reasoning (#30479): the tier override must land under the key
        # the next turn reads, and the eligibility check must see that turn's route.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        self._service_tier = self._resolve_session_service_tier(session_key=session_key)
        user_config = _load_gateway_config()
        try:
            # Off the loop: credential resolution can refresh an OAuth token, as the turn does.
            model, runtime = await asyncio.to_thread(
                self._resolve_session_agent_runtime, source=source, session_key=session_key, user_config=user_config)
        except Exception:
            # No usable route at all (the turn fails too): judge the configured model alone.
            model, runtime = _resolve_gateway_model(user_config), {}
        if not _fast_route_supports(model, runtime):
            return t("gateway.fast.not_supported")
        args = FAST_ARG_ALIASES.get(args, args)
        from agent.codex_service_tiers import ultrafast_offered
        ultrafast = ultrafast_offered(model, provider=runtime.get("provider"), base_url=runtime.get("base_url"),
                                      access_token=runtime.get("api_key"))
        if args == "ultrafast" and not ultrafast:
            return t("gateway.fast.ultrafast_not_supported", model=model)
        if args and args != "status":
            return self._apply_fast_selection(session_key, args, persist=persist_global)
        mode = service_tier_word(self._service_tier)
        status = {"fast": t("gateway.fast.status_fast"), "normal": t("gateway.fast.status_normal")}.get(mode, mode)

        async def _on_fast_choice(_chat_id: str, value: str) -> str:
            return self._apply_fast_selection(session_key, value, persist=persist_global)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.fast.picker_title", mode=status),
            choices=[
                {"value": v, "label": t(f"gateway.fast.choice_{v}"), "is_current": mode == v}
                for v in ("fast", "normal", "auto", "cold", *(("ultrafast",) if ultrafast else ()))
            ],
            on_choice_selected=_on_fast_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return "\n".join([
            "# Priority Processing",
            "",
            f"- Current mode: `{status}`",
            "",
            "*Usage:* `/fast <normal|fast|auto|cold|ultrafast|status>`",
        ])
