"""FAL.ai music generation backend.

Port of the external agent's ``extensions/fal/music-generation-provider.ts``: MiniMax Music
(default), ACE-Step and Stable Audio 2.5 behind fal.ai's queue API (``fal_client``).
Model-specific payloads per the external-agent port — MiniMax takes lyrics/instrumental,
ACE-Step and Stable Audio take neither lyrics nor (Stable Audio) the instrumental
toggle, and each family only accepts its own output format. Auth via ``FAL_KEY``
or the managed Nous fal-queue gateway (same selection rules as the FAL video
plugin: ``music_gen.provider`` ``"nous"`` → managed only, another name → direct
only, never-configured → direct when a key exists, else managed).
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    save_url_music,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "fal-ai/minimax-music/v2.6"
ACE_STEP_MODEL = "fal-ai/ace-step/prompt-to-audio"
STABLE_AUDIO_MODEL = "fal-ai/stable-audio-25/text-to-audio"
DEFAULT_TIMEOUT_S = 180.0

FAL_MUSIC_MODELS: list[dict[str, Any]] = [
    {"id": DEFAULT_MODEL, "display": "MiniMax Music 2.6 (via FAL)", "speed": "~60-180s",
     "strengths": "Full songs with vocals, lyrics optimizer, up to 240s.",
     "price": "per-second", "modalities": ["text"],
     "supports_lyrics": True, "supports_instrumental": True, "formats": ["mp3"],
     "max_duration": 240, "min_duration": 1},
    {"id": ACE_STEP_MODEL, "display": "ACE-Step (via FAL)", "speed": "~60-180s",
     "strengths": "Open music model; instrumental only, wav out.",
     "price": "per-second", "modalities": ["text"],
     "supports_lyrics": False, "supports_instrumental": True, "formats": ["wav"],
     "max_duration": 240, "min_duration": 1},
    {"id": STABLE_AUDIO_MODEL, "display": "Stable Audio 2.5 (via FAL)", "speed": "~60-180s",
     "strengths": "Stability AI text-to-audio; wav out, no lyrics/instrumental toggle.",
     "price": "per-second", "modalities": ["text"],
     "supports_lyrics": False, "supports_instrumental": False, "formats": ["wav"],
     "max_duration": 240, "min_duration": 1},
]


def _build_payload(
    *, model: str, prompt: str, lyrics: Optional[str], instrumental: Optional[bool],
    duration: Optional[int], format: str,
) -> dict[str, Any]:
    """Model-specific request body (port of ``buildFalMusicRequestBody``)."""
    if model in (ACE_STEP_MODEL, STABLE_AUDIO_MODEL):
        is_stable = model == STABLE_AUDIO_MODEL
        if lyrics:
            raise ValueError(
                f"fal {'Stable Audio' if is_stable else 'ACE-Step'} music generation "
                "does not support explicit lyrics.")
        if is_stable and instrumental is True:
            raise ValueError("fal Stable Audio music generation does not support instrumental mode.")
        payload: dict[str, Any] = {"prompt": prompt}
        if instrumental is True:
            payload["instrumental"] = True
        if duration is not None:
            payload["seconds_total" if is_stable else "duration"] = duration
        return payload
    if lyrics and instrumental is True:
        raise ValueError("fal MiniMax music generation cannot use lyrics when instrumental=true.")
    return {
        "prompt": prompt,
        **({"lyrics": lyrics} if lyrics else {}),
        **({"is_instrumental": True} if instrumental is True else {}),
        **({} if lyrics or instrumental is True else {"lyrics_optimizer": True}),
        **({"duration": duration} if duration is not None else {}),
        "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": format},
    }


def _audio_url_from_result(result: Any) -> Optional[str]:
    """Audio URL from a FAL music result: ``audio.url``, ``audio_url``, or a bare string."""
    if not isinstance(result, dict):
        return None
    audio = result.get("audio")
    if isinstance(audio, dict):
        url = audio.get("url")
        return str(url) if isinstance(url, str) and url else None
    if isinstance(audio, str) and audio:
        return audio
    for key in ("audio_url", "url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


_fal_client: Any = None
_fal_client_lock = threading.Lock()

_managed_fal_music_client: Any = None
_managed_fal_music_client_config: Any = None
_managed_fal_music_client_lock = threading.Lock()


def _load_fal_client() -> Any:
    """Lazy-load ``fal_client`` once via ``tools.fal_common``."""
    global _fal_client
    with _fal_client_lock:
        if _fal_client is None:
            from tools.fal_common import import_fal_client
            _fal_client = import_fal_client()
        return _fal_client


def _resolve_managed_fal_music_gateway():
    """Resolve the FAL music route from the stored ``music_gen`` selection: ``"nous"`` → managed
    only (unentitled ⇒ selection-naming error); other stored provider → direct only (missing
    FAL_KEY ⇒ error); never-configured → autodetect."""
    from tools.managed_tool_gateway import resolve_managed_tool_gateway
    from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, fal_key_is_configured, read_selection, selection_error
    selected = read_selection("music_gen")
    if selected == NOUS_MANAGED_PROVIDER:
        gateway = resolve_managed_tool_gateway("fal-queue")
        if gateway is None:
            raise ValueError(selection_error("music_gen", NOUS_MANAGED_PROVIDER, "the Nous Tool Gateway is not available (not entitled or unreachable)"))
        return gateway
    if selected is not None:
        if not fal_key_is_configured():
            raise ValueError(selection_error("music_gen", selected, "FAL_KEY is not set"))
        return None
    return None if fal_key_is_configured() else resolve_managed_tool_gateway("fal-queue")


def _fal_music_available() -> bool:
    """True if the selected (or, never-configured, any) FAL backend is reachable; raises on a stored-but-broken selection."""
    from tools.tool_backend_helpers import fal_key_is_configured
    return _resolve_managed_fal_music_gateway() is not None or fal_key_is_configured()


def _get_managed_fal_music_client(managed_gateway):
    """Reuse the managed FAL client so its internal httpx.Client is not leaked per call."""
    global _managed_fal_music_client, _managed_fal_music_client_config
    from tools.fal_common import _ManagedFalSyncClient
    client_config = (managed_gateway.gateway_origin.rstrip("/"), managed_gateway.nous_user_token)
    with _managed_fal_music_client_lock:
        if _managed_fal_music_client is None or _managed_fal_music_client_config != client_config:
            _managed_fal_music_client = _ManagedFalSyncClient(_load_fal_client(), key=managed_gateway.nous_user_token,
                                                              queue_run_origin=managed_gateway.gateway_origin)
            _managed_fal_music_client_config = client_config
        return _managed_fal_music_client


def _submit_fal_music_request(endpoint: str, arguments: dict[str, Any]):
    """Submit via direct credentials or the managed queue gateway; ``wait_fal_queue_result`` blocks."""
    client = _load_fal_client()
    headers = {"x-idempotency-key": str(uuid.uuid4())}
    managed_gateway = _resolve_managed_fal_music_gateway()
    if managed_gateway is None:
        return client.submit(endpoint, arguments=arguments, headers=headers)
    from tools.fal_common import (
        _extract_http_status, _managed_fal_billing_error, submit_managed_fal_with_rate_limit_retry,
    )
    try:
        return submit_managed_fal_with_rate_limit_retry(
            lambda request_headers: _get_managed_fal_music_client(managed_gateway).submit(
                endpoint, arguments=arguments, headers=request_headers),
            what="music endpoint", name=endpoint)
    except Exception as exc:
        status = _extract_http_status(exc)
        if status is not None and 400 <= status < 500:
            billing = _managed_fal_billing_error(exc, "endpoint")
            if billing is not None:
                raise ValueError(
                    f"Nous Subscription gateway rejected endpoint '{endpoint}' (HTTP {status}): {billing}") from exc
            raise ValueError(f"Nous Subscription gateway rejected endpoint '{endpoint}' (HTTP {status}). This model may not yet be enabled "
                             f"on the Nous Portal's FAL proxy. Either:\n  • Set FAL_KEY in your environment to use FAL.ai directly, or\n"
                             f"  • Pick a different model via `hermes tools` → Music Generation.") from exc
        raise


_NO_BACKEND_MSG = ("No FAL backend available. Either set FAL_KEY (run `hermes tools` → Music Generation → FAL to configure) "
                   "or sign in to Nous (`hermes setup`) for managed gateway access.")


class FALMusicGenProvider(MusicGenProvider):
    """FAL.ai music backend; one family per model id, MiniMax Music by default."""

    name = "fal"
    display_name = "FAL"

    def is_available(self) -> bool:
        # A stored-but-broken selection raises the selection-naming ValueError; report unavailable, never break the picker.
        try:
            return _fal_music_available()
        except Exception:  # health: allow BLE001 -- availability probe: any failure reads as unavailable
            return False

    def list_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in FAL_MUSIC_MODELS]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": "FAL", "badge": "paid", "env_vars": [{"key": "FAL_KEY", "prompt": "FAL.ai API key", "url": "https://fal.ai/dashboard/keys"}],
                "tag": "MiniMax Music 2.6, ACE-Step, Stable Audio 2.5 — text-to-music via the FAL queue API"}

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text"], "formats": ["mp3", "wav"],
                "max_duration": 240, "min_duration": 1,
                "supports_lyrics": True, "supports_instrumental": True,
                "supports_duration": True, "supports_format": True,
                "supports_seed": False, "max_reference_images": 0}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = "mp3", reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del reference_image_urls, seed, kwargs  # FAL music takes no images or seed
        prompt = (prompt or "").strip()
        model_id = (model or "").strip() or DEFAULT_MODEL

        def fail(error: str, error_type: str) -> dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt, format=format)

        if not prompt:
            return fail("prompt is required.", "missing_prompt")
        if model_id not in {m["id"] for m in FAL_MUSIC_MODELS}:
            return fail(f"Unknown FAL music model '{model_id}'.", "unknown_model")
        try:
            if not _fal_music_available():
                return fail(_NO_BACKEND_MSG, "auth_required")
        except ValueError as exc:
            return fail(str(exc), "auth_required")
        try:
            _load_fal_client()
        except ImportError:
            return fail("fal_client Python package not installed (pip install fal-client)", "missing_dependency")
        try:
            payload = _build_payload(model=model_id, prompt=prompt, lyrics=(lyrics or "").strip() or None,
                                     instrumental=instrumental, duration=duration, format=format)
        except ValueError as exc:
            return fail(str(exc), "capability_unsupported")
        try:
            from tools.fal_common import wait_fal_queue_result
            result = wait_fal_queue_result(_submit_fal_music_request(model_id, payload))
        except Exception as exc:
            logger.warning("FAL music gen failed (model=%s): %s", model_id, exc, exc_info=True)
            return fail(f"FAL music generation failed: {exc}", "api_error")
        url = _audio_url_from_result(result)
        if not url:
            return fail("FAL returned no audio URL in response", "empty_response")
        try:
            audio_path = str(save_url_music(url, prefix="fal"))
        except Exception as exc:
            logger.warning("FAL music download failed (%s): %s", url, exc, exc_info=True)
            return fail(f"FAL generated music download failed: {exc}", "api_error")
        record = result if isinstance(result, dict) else {}
        returned_lyrics = record.get("lyrics") if isinstance(record.get("lyrics"), str) else None
        extra: dict[str, Any] = {"endpoint": model_id}
        if isinstance(record.get("seed"), (int, str)):
            extra["seed"] = record["seed"]
        return success_response(
            audio=audio_path, model=model_id, prompt=prompt,
            duration=int(duration or 0), format=format, provider=self.name,
            lyrics=returned_lyrics, extra=extra)


def register(ctx) -> None:
    """Plugin entry point — wire ``FALMusicGenProvider`` into the registry."""
    ctx.register_music_gen_provider(FALMusicGenProvider())
