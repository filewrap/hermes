#!/usr/bin/env python3
"""``music_generate``: one tool dispatching to a plugin-registered :class:`MusicGenProvider`
(``agent/music_gen_provider.py`` ABC, ``agent/music_gen_registry.py``, ``plugins/music_gen/<name>/``).

Port of the external agent's ``music-generate`` tool (``src/agents/tools/music-generate-tool.ts`` +
``src/music-generation/runtime.ts``): prompt (style/genre/mood), exact lyrics only when
the user supplies them, instrumental toggle, up to 10 reference images, a ``provider/model``
override, mp3/wav out. Generation runs as a **background task** — the call returns a
task id at once; ``action=status`` polls it and ``action=list`` shows providers and
recent tasks. A repeated identical request returns the existing task instead of
spending twice (duplicate guard), and when the active provider fails the runner
falls through to the other available providers, normalising each request against
that provider's capabilities first (unsupported lyrics/instrumental/duration/format
are dropped and reported, never sent). Tracks land under
``$HERMES_HOME/cache/generated/tool-music-generation/``.

A configured-but-unregistered ``music_gen.provider`` fails closed (no silent reroute);
fallback only ever runs between *registered, available* providers, and the result
always names the provider that actually generated.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.music_gen_provider import (
    COMMON_FORMATS,
    DEFAULT_FORMAT,
    error_response,
    save_url_music,
)
from tools import music_generation_tasks as tasks
from tools.registry import registry

logger = logging.getLogger(__name__)

MAX_REFERENCE_IMAGES = 10

MUSIC_GENERATE_SCHEMA: dict[str, Any] = {
    "name": "music_generate",
    # Placeholder: description AND params are rebuilt at get_tool_definitions() time by
    # _build_dynamic_music_schema() from capabilities() + the model's catalog entry. Optional
    # args are advertised ONLY when honored; the handler accepts them regardless (replay compat).
    "description": "(rebuilt at get_definitions() time — see _build_dynamic_music_schema)",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["generate", "list", "status"],
                "description": (
                    "generate (default): start a background generation and return its task id. "
                    "list: providers/models plus recent tasks. status: one task (task_id, or the "
                    "most recent active task when omitted)."
                ),
            },
            "task_id": {
                "type": "string",
                "description": "Task id from a generate call, for action=status.",
            },
            "prompt": {
                "type": "string",
                "description": "Music prompt: style, genre, mood, tempo, instruments, purpose.",
            },
            "lyrics": {
                "type": "string",
                "description": (
                    "Exact sung lyrics only when the user supplies lyrics or asks for vocal words. "
                    "For song/style requests, use prompt instead."
                ),
            },
            "instrumental": {
                "type": "boolean",
                "description": "Instrumental-only toggle.",
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_REFERENCE_IMAGES,
                "description": "Reference image URLs/paths to condition on (style/mood refs).",
            },
            "model": {
                "type": "string",
                "description": (
                    "Provider/model override, e.g. google/lyria-3-pro-preview or a bare model id. "
                    "Omit for the configured model."
                ),
            },
            "duration": {
                "type": "integer",
                "description": "Target seconds; the provider may clamp. Omit for the provider default.",
            },
            "format": {
                "type": "string",
                "enum": list(COMMON_FORMATS),
                "description": "Output format: mp3, wav.",
            },
            "filename": {
                "type": "string",
                "description": "Preferred file name for the saved track (extension is added when missing).",
            },
        },
        "required": [],
    },
}


def _read_music_gen_key(key: str) -> Optional[str]:
    """Return the stripped ``music_gen.<key>`` string from config.yaml, or None."""
    try:
        from hermes_cli.config import cfg_get, load_config
        value = cfg_get(load_config(), "music_gen", key)
    except Exception as exc:
        logger.debug("Could not read music_gen config: %s", exc, exc_info=True)
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_configured_music_provider() -> Optional[str]:
    return _read_music_gen_key("provider")


def _read_configured_music_model() -> Optional[str]:
    return _read_music_gen_key("model")


def _discovered_registry():
    """Import the provider registry after (idempotent) plugin discovery so user-installed plugins are visible."""
    from agent import music_gen_registry
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered()
    return music_gen_registry, _ensure_plugins_discovered


def _provider_call(provider: Any, method: str, default: Any) -> Any:
    """``provider.<method>()`` or ``default`` when it raises or returns a falsy value."""
    try:
        return getattr(provider, method)() or default
    except Exception:  # health: allow BLE001 -- provider probe: any failure reads as "method absent"
        return default


def _is_available(provider: Any) -> bool:
    try:
        return bool(provider.is_available())
    except Exception:  # health: allow BLE001 -- availability probe: any failure reads as unavailable
        return False


def check_music_generation_requirements() -> bool:
    """True when at least one registered provider reports available."""
    registry_mod, _ = _discovered_registry()
    return any(_is_available(p) for p in registry_mod.list_providers())


def _resolve_active_provider():
    """Active provider or None; a miss forces a discovery refresh (sessions older than the plugin install)."""
    try:
        registry_mod, ensure_discovered = _discovered_registry()
        provider = registry_mod.get_active_provider()
        if provider is None:
            ensure_discovered(force=True)
            provider = registry_mod.get_active_provider()
        return provider
    except Exception as exc:
        logger.debug("music_gen provider resolution failed: %s", exc, exc_info=True)
        return None


def _missing_provider_error(configured: Optional[str]) -> str:
    if configured:
        return json.dumps(error_response(
            error=(f"music_gen.provider='{configured}' is set but no plugin registered that name. "
                   f"Run `hermes plugins list` to see installed music gen backends, or "
                   f"`hermes tools` → Music Generation to pick one."),
            error_type="provider_not_registered", provider=configured))
    return json.dumps(error_response(
        error=("No music generation backend is configured. Run `hermes tools` → "
               "Music Generation to enable one (FAL, MiniMax, Google, OpenRouter, or Comfy)."),
        error_type="no_provider_configured"))


_BOOL_WORDS = {"true": True, "1": True, "yes": True, "on": True,
               "false": False, "0": False, "no": False, "off": False}


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return _BOOL_WORDS.get(value.strip().lower()) if isinstance(value, str) else None


def _normalize_reference_images(value: Any) -> Optional[list[str]]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] or None


def _parse_model_ref(raw: Optional[str], providers: list[Any]) -> tuple[Optional[str], Optional[str]]:
    """``provider/model`` → (provider, model); a bare id found in exactly one
    provider's catalog pins that provider; otherwise (None, raw)."""
    if not raw:
        return None, None
    raw = raw.strip()
    if "/" in raw:
        provider, _, model = raw.partition("/")
        if provider.strip() and model.strip():
            return provider.strip(), model.strip()
        return None, raw
    hits = [p for p in providers
            if any(isinstance(m, dict) and m.get("id") == raw for m in _provider_call(p, "list_models", []))]
    if len(hits) == 1:
        return getattr(hits[0], "name", None), raw
    return None, raw


def _resolve_candidates(model_arg: Optional[str]) -> tuple[list[Any], Optional[str], Optional[str]]:
    """Ordered providers to try. An explicit ``model`` override pins the provider(s)
    whose catalog carries it (a ``provider/model`` ref pins exactly one) and disables
    fallback — the model the user named is the model that runs. With no override,
    the active provider runs first and the other available providers follow it
    (fallback). Returns (candidates, pinned_provider, model_id)."""
    registry_mod, _ = _discovered_registry()
    providers = registry_mod.list_providers()
    pinned_provider, model_id = _parse_model_ref(model_arg, providers)
    if pinned_provider:
        provider = next((p for p in providers if getattr(p, "name", None) == pinned_provider), None)
        return ([provider] if provider is not None else []), pinned_provider, model_id
    if model_id:
        carrying = [p for p in providers
                    if any(isinstance(m, dict) and m.get("id") == model_id
                           for m in _provider_call(p, "list_models", []))]
        if carrying:
            active = _resolve_active_provider()
            ordered = [active] if active in carrying else []
            ordered += sorted((p for p in carrying if p not in ordered),
                              key=lambda p: getattr(p, "name", ""))
            return ordered, None, model_id
        active = _resolve_active_provider()
        return ([active] if active is not None else []), None, model_id
    active = _resolve_active_provider()
    ordered = [active] if active is not None else []
    ordered += sorted(
        (p for p in providers if p not in ordered and _is_available(p)),
        key=lambda p: getattr(p, "name", ""),
    )
    return ordered, None, model_id


def _normalize_for_provider(
    provider: Any, model: Optional[str], *, lyrics: Optional[str], instrumental: Optional[bool],
    duration: Optional[int], format: Optional[str], reference_image_urls: Optional[list[str]],
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], dict[str, Any], Optional[str]]:
    """Capability-aware normalisation (port of the external agent ``runtime.ts``): drop what this
    provider/model does not support (recording each as an ignored override), clamp the
    duration to the provider's range (recording the normalisation). Returns
    (kwargs, ignored_overrides, normalization, skip_reason); ``skip_reason`` is set
    when the candidate cannot take this request at all (reference images it rejects)."""
    caps = provider.effective_capabilities(model) if hasattr(provider, "effective_capabilities") \
        else _provider_call(provider, "capabilities", {})
    ignored: list[dict[str, Any]] = []
    normalization: dict[str, Any] = {}
    kwargs: dict[str, Any] = {"model": model}
    refs = list(reference_image_urls or [])
    max_refs = int(caps.get("max_reference_images") or 0)
    if refs and max_refs == 0:
        return None, ignored, normalization, (
            f"Provider '{getattr(provider, 'name', '?')}' does not take reference images.")
    if refs and len(refs) > max_refs:
        refs = refs[:max_refs]
    if lyrics and not caps.get("supports_lyrics", False):
        ignored.append({"key": "lyrics", "value": lyrics})
        lyrics = None
    if isinstance(instrumental, bool) and not caps.get("supports_instrumental", False):
        ignored.append({"key": "instrumental", "value": instrumental})
        instrumental = None
    if duration is not None:
        if not caps.get("supports_duration", True):
            ignored.append({"key": "duration", "value": duration})
            duration = None
        else:
            lo = int(caps.get("min_duration") or 1)
            hi = int(caps.get("max_duration") or 0) or None
            clamped = max(lo, duration)
            if hi is not None:
                clamped = min(hi, clamped)
            if clamped != duration:
                normalization["duration"] = {"requested": duration, "applied": clamped}
            duration = clamped
    if format is not None:
        formats = caps.get("formats") or []
        if not caps.get("supports_format", True) or (formats and format not in formats):
            ignored.append({"key": "format", "value": format})
            format = None
    if lyrics is not None:
        kwargs["lyrics"] = lyrics
    if instrumental is not None:
        kwargs["instrumental"] = instrumental
    if duration is not None:
        kwargs["duration"] = duration
    if format is not None:
        kwargs["format"] = format
    if refs:
        kwargs["reference_image_urls"] = refs
    return kwargs, ignored, normalization, None


def _sanitize_filename(filename: Optional[str], audio_path: str) -> Optional[str]:
    """Basename-only target path for a requested filename, extension kept from the
    saved track; None when the name is unusable."""
    if not filename:
        return None
    name = Path(filename.strip()).name
    if not name or name in (".", ".."):
        return None
    suffix = Path(audio_path).suffix
    if suffix and not name.lower().endswith(suffix.lower()):
        name += suffix
    return str(Path(audio_path).parent / name)


def _localize_audio(audio: str) -> str:
    """A provider-returned http(s) URL is downloaded into the music cache so the
    task result never points at an ephemeral link; anything else passes through."""
    if not isinstance(audio, str) or not audio.startswith(("http://", "https://")):
        return audio
    try:
        return str(save_url_music(audio, prefix="music"))
    except (OSError, ValueError) as exc:
        logger.debug("Could not cache generated music URL %s: %s", audio, exc)
        return audio


def _run_generation(
    home: Path, task_id: str, *, prompt: str, model_arg: Optional[str],
    lyrics: Optional[str], instrumental: Optional[bool], duration: Optional[int],
    format: Optional[str], filename: Optional[str],
    reference_image_urls: Optional[list[str]], candidates: list[Any], model_id: Optional[str],
) -> None:
    """Background worker: try each candidate in order, normalising per provider,
    and land the task record as succeeded (with the track) or failed (with every
    attempt recorded). An unexpected exception lands the task as failed — never
    stuck in running with no live worker."""
    try:
        _run_generation_inner(
            home, task_id, prompt=prompt, model_arg=model_arg, lyrics=lyrics,
            instrumental=instrumental, duration=duration, format=format,
            filename=filename, reference_image_urls=reference_image_urls,
            candidates=candidates, model_id=model_id)
    except Exception as exc:
        logger.exception("music_gen worker failed for task %s", task_id)
        tasks.update_task(home, task_id, status="failed", progress="Failed",
                          error=f"Music generation failed unexpectedly: {exc}",
                          error_type="worker_error")


def _run_generation_inner(
    home: Path, task_id: str, *, prompt: str, model_arg: Optional[str],
    lyrics: Optional[str], instrumental: Optional[bool], duration: Optional[int],
    format: Optional[str], filename: Optional[str],
    reference_image_urls: Optional[list[str]], candidates: list[Any], model_id: Optional[str],
) -> None:
    tasks.update_task(home, task_id, status="running", progress="Generating music")
    last_error = "No music generation provider was available."
    last_error_type = "no_provider_configured"
    for index, provider in enumerate(candidates):
        pname = getattr(provider, "name", "?")
        model = model_id or _read_configured_music_model() or _provider_call(provider, "default_model", None)
        tasks.update_task(
            home, task_id, progress=f"Generating music ({pname}/{model or 'default'}"
            + (f", attempt {index + 1}" if index else "") + ")")
        kwargs, ignored, normalization, skip_reason = _normalize_for_provider(
            provider, model, lyrics=lyrics, instrumental=instrumental, duration=duration,
            format=format, reference_image_urls=reference_image_urls)
        if skip_reason is not None:
            attempt = {"provider": pname, "model": model or "", "error": skip_reason}
            record = tasks.get_task(home, task_id) or {}
            tasks.update_task(home, task_id, attempts=[*(record.get("attempts") or []), attempt])
            last_error, last_error_type = skip_reason, "capability_unsupported"
            continue
        try:
            result = provider.generate(prompt=prompt, **kwargs)
        except TypeError as exc:
            logger.warning("music_gen provider '%s' rejected kwargs: %s", pname, exc)
            error = (f"Provider '{pname}' signature is out of date with the music_generate "
                     f"schema. Report this to the plugin author.")
            result = error_response(error=error, error_type="provider_contract",
                                    provider=pname, model=model or "", prompt=prompt)
        except Exception as exc:
            logger.warning("music_gen provider '%s' raised: %s", pname, exc, exc_info=True)
            result = error_response(error=f"Provider '{pname}' error: {exc}",
                                    error_type="provider_exception",
                                    provider=pname, model=model or "", prompt=prompt)
        if not isinstance(result, dict):
            result = error_response(error="Provider returned a non-dict result",
                                    error_type="provider_contract",
                                    provider=pname, model=model or "", prompt=prompt)
        record = tasks.get_task(home, task_id) or {}
        attempts = [*(record.get("attempts") or []),
                    {"provider": pname, "model": model or "",
                     "error": None if result.get("success") else str(result.get("error") or "failed")}]
        if not result.get("success"):
            tasks.update_task(home, task_id, attempts=attempts)
            last_error = str(result.get("error") or "Provider failed without an error message.")
            last_error_type = str(result.get("error_type") or "provider_error")
            continue
        audio = _localize_audio(str(result.get("audio") or ""))
        if not audio:
            tasks.update_task(home, task_id, attempts=attempts)
            last_error, last_error_type = "Provider returned no audio.", "empty_response"
            continue
        renamed = _sanitize_filename(filename, audio)
        if renamed is not None and renamed != audio:
            try:
                Path(audio).replace(renamed)
                audio = renamed
            except OSError as exc:
                logger.debug("Could not rename generated music to %s: %s", renamed, exc)
        tasks.update_task(
            home, task_id, status="succeeded", progress="Done", audio=audio,
            duration_seconds=int(result.get("duration") or 0) or None,
            lyrics_returned=result.get("lyrics"),
            provider=pname, model=str(result.get("model") or model or ""),
            attempts=attempts, ignored_overrides=ignored, normalization=normalization)
        return
    tasks.update_task(home, task_id, status="failed", progress="Failed",
                      error=last_error, error_type=last_error_type)


def _task_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": record.get("id"), "status": record.get("status"),
        "prompt": record.get("prompt"), "provider": record.get("provider"),
        "model": record.get("model"), "created_at": record.get("created_at"),
        "audio": record.get("audio"), "error": record.get("error"),
    }


def _handle_list(home: Path) -> str:
    registry_mod, _ = _discovered_registry()
    providers = []
    for provider in registry_mod.list_providers():
        caps = _provider_call(provider, "capabilities", {})
        models = _provider_call(provider, "list_models", [])
        providers.append({
            "id": getattr(provider, "name", "?"),
            "display": getattr(provider, "display_name", getattr(provider, "name", "?")),
            "available": _is_available(provider),
            "default_model": provider.default_model(),
            "models": [m.get("id") for m in models if isinstance(m, dict) and m.get("id")],
            "capabilities": {
                "supports_lyrics": bool(caps.get("supports_lyrics")),
                "supports_instrumental": bool(caps.get("supports_instrumental")),
                "formats": list(caps.get("formats") or []),
                "max_duration": caps.get("max_duration"),
                "max_reference_images": int(caps.get("max_reference_images") or 0),
            },
        })
    return json.dumps({
        "success": True, "action": "list", "providers": providers,
        "tasks": [_task_summary(r) for r in tasks.list_tasks(home)],
    })


def _handle_status(home: Path, task_id: Optional[str]) -> str:
    record = tasks.get_task(home, task_id) if task_id else tasks.most_recent_active(home)
    if record is None and not task_id:
        recent = tasks.list_tasks(home, limit=1)
        record = recent[0] if recent else None
    if record is None:
        return json.dumps(error_response(
            error=("No music generation task found."
                   + (" No task with that id." if task_id
                      else " No tasks yet — call music_generate with a prompt first.")),
            error_type="task_not_found"))
    return json.dumps({"success": True, "action": "status", "task": record})


def _handle_generate(args: dict[str, Any], **_kw: Any) -> str:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    prompt = (args.get("prompt") or "").strip()
    reference_image_urls = _normalize_reference_images(args.get("reference_image_urls"))
    turn_task_id = _kw.get("task_id")
    if reference_image_urls:
        # Confinement chokepoint (mirrors video_generate): non-local backends hand providers data: URLs.
        from tools.image_generation_tool import _confine_source_images
        _, reference_image_urls, confine_error = _confine_source_images(
            None, reference_image_urls, turn_task_id)
        if confine_error is not None:
            return confine_error
    if not prompt:
        return json.dumps(error_response(
            error="prompt is required for music generation", error_type="missing_prompt"))
    if reference_image_urls and len(reference_image_urls) > MAX_REFERENCE_IMAGES:
        return json.dumps(error_response(
            error=f"At most {MAX_REFERENCE_IMAGES} reference images are supported",
            error_type="capability_unsupported", prompt=prompt))
    format_arg = (args.get("format") or "").strip().lower() or None
    if format_arg is not None and format_arg not in COMMON_FORMATS:
        return json.dumps(error_response(
            error='format must be one of "mp3" or "wav"', error_type="invalid_format",
            prompt=prompt))
    lyrics = (args.get("lyrics") or "").strip() or None
    instrumental = _coerce_bool(args.get("instrumental"))
    duration = _coerce_int(args.get("duration"))
    filename = (args.get("filename") or "").strip() or None
    model_arg = (args.get("model") or "").strip() or None
    configured = _read_configured_music_provider()
    candidates, pinned_provider, model_id = _resolve_candidates(model_arg)
    if pinned_provider and not candidates:
        return json.dumps(error_response(
            error=(f"Model override names provider '{pinned_provider}' but no plugin registered "
                   f"that name. Run `hermes plugins list` to see installed music gen backends."),
            error_type="provider_not_registered", provider=pinned_provider, prompt=prompt))
    if not candidates:
        return _missing_provider_error(configured)
    request_key = tasks.build_request_key(
        prompt=prompt, provider=pinned_provider or configured,
        model=model_id or _read_configured_music_model(),
        lyrics=lyrics, instrumental=instrumental, duration=duration, format=format_arg,
        filename=filename, reference_image_urls=reference_image_urls)
    duplicate = tasks.find_duplicate(home, request_key)
    if duplicate is not None:
        payload = {"success": True, "duplicate": True, "task_id": duplicate["id"],
                   "status": duplicate["status"], "task": duplicate}
        if duplicate["status"] == "succeeded":
            payload["message"] = "An identical request already produced this track."
            payload["audio"] = duplicate.get("audio")
        else:
            payload["message"] = "An identical request is already generating."
        return json.dumps(payload)
    record = tasks.create_task(
        home, request_key=request_key, prompt=prompt, provider=pinned_provider or configured,
        model=model_id or _read_configured_music_model(), lyrics=lyrics,
        instrumental=instrumental, duration=duration, format=format_arg, filename=filename,
        reference_image_urls=reference_image_urls)
    tasks.spawn_task_thread(
        home, record["id"], _run_generation, home, record["id"],
        prompt=prompt, model_arg=model_arg, lyrics=lyrics, instrumental=instrumental,
        duration=duration, format=format_arg, filename=filename,
        reference_image_urls=reference_image_urls, candidates=candidates, model_id=model_id)
    return json.dumps({
        "success": True, "task_id": record["id"], "status": "pending",
        "message": ("Music generation started in the background. Poll with "
                    f"music_generate action=status task_id={record['id']}."),
    })


def _handle_music_generate(args: dict[str, Any], **_kw: Any) -> str:
    from hermes_constants import get_hermes_home

    action = (args.get("action") or "generate").strip().lower()
    home = Path(get_hermes_home())
    if action == "list":
        return _handle_list(home)
    if action == "status":
        return _handle_status(home, (args.get("task_id") or "").strip() or None)
    if action != "generate":
        return json.dumps(error_response(
            error='action must be one of "generate", "list", or "status"',
            error_type="invalid_action"))
    return _handle_generate(args, **_kw)


# Dynamic schema — reflects the active backend's actual capabilities so the model usually gets
# the call right first try. model_tools.get_tool_definitions() keys its cache on config.yaml
# mtime, so the schema rebuilds on provider/model change. Optional params below are advertised
# only when the provider's capabilities() sets the flag (order = schema property order).
_GENERIC_DESCRIPTION = (
    "Generate music from a text prompt. Style, genre, mood, tempo, instruments, and purpose shape the result. "
    "Lyrics are exact sung words, only when the user supplies them — for song/style requests "
    "the prompt carries the style, never invent lyrics. Generation runs in the background: "
    "the call returns a task id at once; poll it with action=status. A repeated identical "
    "request returns the existing task instead of generating twice. When the active backend "
    "fails, the runner falls through to the other available backends. The backend and model "
    "are user-configured via `hermes tools` → Music Generation; `model` only overrides them "
    "for one call. Returns the result in the `audio` field — an absolute file path under "
    "tool-music-generation/; reference it in your response using the current platform's "
    "file-delivery convention."
)


def _schema(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": []}}


def _build_dynamic_music_schema() -> dict[str, Any]:
    """Description AND params from capabilities() + the model's catalog entry; enums and duration
    bounds tighten to the active model. Unadvertised args are still accepted (replay compat)."""
    static_props = MUSIC_GENERATE_SCHEMA["parameters"]["properties"]
    parts: list[str] = [_GENERIC_DESCRIPTION]
    configured_model = _read_configured_music_model()
    provider = _resolve_active_provider()
    base_props = {k: static_props[k] for k in ("action", "task_id", "prompt")}
    if provider is None:
        parts.append(
            "\nNo music backend is available. Calls will return an error "
            "until the user picks one via `hermes tools` → Music Generation.")
        return _schema("\n".join(parts), base_props)
    caps = _provider_call(provider, "capabilities", {})
    models = _provider_call(provider, "list_models", [])
    active_model = configured_model or provider.default_model()
    model_meta = next((m for m in models if isinstance(m, dict) and m.get("id") == active_model), {})
    effective = dict(caps)
    for key in ("supports_lyrics", "supports_instrumental", "supports_duration",
                "formats", "max_duration", "min_duration", "max_reference_images"):
        if model_meta.get(key) is not None:
            effective[key] = model_meta[key]
    parts.append(f"\nActive backend: {getattr(provider, 'display_name', provider.name)}"
                 + (f" (model {active_model})" if active_model else "") + ".")
    properties: dict[str, Any] = dict(base_props)
    properties["model"] = static_props["model"]
    if effective.get("supports_lyrics"):
        properties["lyrics"] = static_props["lyrics"]
    if effective.get("supports_instrumental"):
        properties["instrumental"] = static_props["instrumental"]
    max_refs = int(effective.get("max_reference_images") or 0)
    if max_refs > 0:
        properties["reference_image_urls"] = {
            "type": "array", "items": {"type": "string"}, "maxItems": min(max_refs, MAX_REFERENCE_IMAGES),
            "description": (f"Up to {min(max_refs, MAX_REFERENCE_IMAGES)} reference image URLs "
                            "(style or mood refs).")}
    min_duration = effective.get("min_duration")
    max_duration = effective.get("max_duration")
    duration_param = dict(static_props["duration"])
    if min_duration and max_duration:
        duration_param["minimum"] = int(min_duration)
        duration_param["maximum"] = int(max_duration)
        duration_param["description"] = (
            f"Target seconds ({min_duration}-{max_duration}). Omit for the provider default.")
    properties["duration"] = duration_param
    format_param = dict(static_props["format"])
    if effective.get("formats"):
        format_param["enum"] = list(effective["formats"])
    properties["format"] = format_param
    properties["filename"] = static_props["filename"]
    return _schema("\n".join(parts), properties)


registry.register(
    name="music_generate",
    toolset="music_gen",
    schema=MUSIC_GENERATE_SCHEMA,
    handler=_handle_music_generate,
    check_fn=check_music_generation_requirements,
    requires_env=[],
    is_async=False,
    emoji="🎵",
    dynamic_schema_overrides=_build_dynamic_music_schema)
