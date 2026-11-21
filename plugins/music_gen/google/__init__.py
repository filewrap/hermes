"""Google Lyria music generation backend.

Port of the external agent's ``extensions/google/music-generation-provider.ts`` (SDK call
replaced by the equivalent REST call, so no new dependency): ``POST
/v1beta/models/{model}:generateContent`` with ``responseModalities`` ``["AUDIO",
"TEXT"]``. Text parts come back as lyrics; the ``inlineData`` part is base64
audio. Up to 10 reference images ride along as ``inlineData`` parts. Auth via
``GOOGLE_API_KEY`` (``GEMINI_API_KEY`` alias).
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Dict, List, Optional

import requests

from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    load_reference_image_bytes,
    save_bytes_music,
    success_response,
)
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "lyria-3-clip-preview"
PRO_MODEL = "lyria-3-pro-preview"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 180.0
MAX_INPUT_IMAGES = 10
_MAX_AUDIO_BYTES = 200 * 1024 * 1024

GOOGLE_MUSIC_MODELS: list[dict[str, Any]] = [
    {"id": DEFAULT_MODEL, "display": "Lyria 3 Clip", "speed": "~30-60s",
     "strengths": "Short clips up to 30s, MP3.", "price": "per-request",
     "modalities": ["text", "image"], "supports_lyrics": True,
     "supports_instrumental": True, "formats": ["mp3"], "max_reference_images": MAX_INPUT_IMAGES},
    {"id": PRO_MODEL, "display": "Lyria 3 Pro", "speed": "~60-180s",
     "strengths": "Full-length songs up to ~3 minutes, MP3 or WAV.", "price": "per-request",
     "modalities": ["text", "image"], "supports_lyrics": True,
     "supports_instrumental": True, "formats": ["mp3", "wav"],
     "max_reference_images": MAX_INPUT_IMAGES},
]


def _canonical_b64(data: str) -> Optional[str]:
    """Strip whitespace, normalise URL-safe alphabet, re-pad; None when malformed."""
    cleaned = "".join(data.split()).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        return None
    return cleaned


def _resolve_base_url() -> str:
    try:
        from hermes_cli.config import cfg_get, load_config
        value = cfg_get(load_config(), "music_gen", "google", "base_url")
    except Exception as exc:
        logger.debug("Could not read music_gen.google.base_url: %s", exc, exc_info=True)
        value = None
    if isinstance(value, str) and value.strip():
        return value.strip().rstrip("/")
    return DEFAULT_BASE_URL


def _terminal_no_audio_reason(payload: dict[str, Any]) -> Optional[str]:
    """A block/stop reason is terminal — retrying the same prompt cannot help."""
    feedback = payload.get("promptFeedback") if isinstance(payload.get("promptFeedback"), dict) else {}
    block = feedback.get("blockReason")
    if isinstance(block, str) and block and not block.endswith("_UNSPECIFIED"):
        return f"prompt blocked ({block})"
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        reason = candidate.get("finishReason")
        if isinstance(reason, str) and reason and reason not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            return f"generation stopped ({reason})"
    return None


def _extract_lyria_audio(payload: dict[str, Any]) -> tuple[list[bytes], list[str], str]:
    """(audio chunks, lyric text, audio mime) from a ``generateContent`` payload.

    Raises ``ValueError`` on malformed base64 audio or an oversize track; an
    audio-less payload returns empty chunks and the caller decides (terminal
    block reason vs a transient retry).
    """
    lyrics_out: list[str] = []
    audio_chunks: list[bytes] = []
    audio_mime = "audio/mpeg"
    for candidate in payload.get("candidates") or []:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        part_list = content.get("parts") if isinstance(content, dict) else None
        for part in part_list or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                lyrics_out.append(text)
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            data = inline.get("data") if isinstance(inline, dict) else None
            if not isinstance(data, str) or not data.strip():
                continue
            standard = _canonical_b64(data)
            if standard is None:
                raise ValueError("Generated music asset contains malformed base64 audio data")
            raw = base64.b64decode(standard)
            if len(raw) > _MAX_AUDIO_BYTES:
                raise ValueError(f"Google generated music exceeds {_MAX_AUDIO_BYTES} bytes")
            audio_chunks.append(raw)
            mime = inline.get("mimeType") or inline.get("mime_type")
            if isinstance(mime, str) and mime.strip():
                audio_mime = mime.strip().lower()
    return audio_chunks, lyrics_out, audio_mime


class GoogleMusicGenProvider(MusicGenProvider):
    """Lyria 3 Clip / Pro backend over the Gemini API ``generateContent`` endpoint."""

    name = "google"
    display_name = "Google"

    def _api_key(self) -> str:
        try:
            return (get_secret("GOOGLE_API_KEY") or get_secret("GEMINI_API_KEY") or "").strip()
        except Exception as exc:
            logger.debug("Google credential resolution failed: %s", exc, exc_info=True)
            return ""

    def is_available(self) -> bool:
        return bool(self._api_key())

    def list_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in GOOGLE_MUSIC_MODELS]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": "Google", "badge": "paid",
                "env_vars": [{"key": "GOOGLE_API_KEY", "prompt": "Google AI Studio API key",
                              "url": "https://aistudio.google.com/app/apikey"}],
                "tag": "Lyria 3 Clip / Pro — text-to-music with lyrics, up to 10 reference images"}

    def capabilities(self) -> dict[str, Any]:
        # No duration parameter on this API: duration hints ride in the prompt text.
        return {"modalities": ["text", "image"], "formats": ["mp3", "wav"],
                "supports_lyrics": True, "supports_instrumental": True,
                "supports_duration": False, "supports_format": True,
                "supports_seed": False, "max_reference_images": MAX_INPUT_IMAGES}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = "mp3", reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del duration, seed, kwargs  # no duration / seed params on this API
        prompt = (prompt or "").strip()
        model_id = (model or "").strip() or DEFAULT_MODEL
        refs = [r for r in (reference_image_urls or []) if isinstance(r, str) and r.strip()]

        def fail(error: str, error_type: str) -> dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt, format=format)

        if not prompt:
            return fail("prompt is required.", "missing_prompt")
        if len(refs) > MAX_INPUT_IMAGES:
            return fail(f"Google music generation supports at most {MAX_INPUT_IMAGES} reference images.",
                        "capability_unsupported")
        if format:
            supported = ["mp3", "wav"] if model_id == PRO_MODEL else ["mp3"]
            if format not in supported:
                return fail(f"Google music generation model {model_id} supports "
                            f"{', '.join(supported)} output.", "capability_unsupported")
        api_key = self._api_key()
        if not api_key:
            return fail("No Google credential: set GOOGLE_API_KEY (or GEMINI_API_KEY).",
                        "missing_credentials")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if instrumental is True:
            parts[0]["text"] += "\n\nInstrumental only. No vocals, no sung lyrics, no spoken word."
        if lyrics and lyrics.strip():
            parts[0]["text"] += f"\n\nLyrics:\n{lyrics.strip()}"
        try:
            for ref in refs:
                raw, mime = load_reference_image_bytes(ref)
                parts.append({"inlineData": {
                    "mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}})
        except (OSError, ValueError) as exc:
            return fail(f"Could not load reference image: {exc}", "invalid_request")
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["AUDIO", "TEXT"]},
        }
        url = f"{_resolve_base_url()}/models/{model_id}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        # Lyria promises audio for successful responses but has returned unblocked
        # text-only payloads transiently. Never retry explicit stops.
        for _attempt in range(2):
            try:
                response = requests.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT_S)
            except Exception as exc:
                logger.warning("Google music request failed: %s", exc, exc_info=True)
                return fail(f"Google music generation failed: {exc}", "api_error")
            try:
                if response.status_code >= 400:
                    return fail(f"Google rejected the request (HTTP {response.status_code}): "
                                f"{response.text[:300]}", "api_error")
                try:
                    payload = response.json()
                except ValueError as exc:
                    return fail(f"Google music generation returned an unreadable response: {exc}",
                                "api_error")
                if not isinstance(payload, dict):
                    return fail("Google music generation returned an unexpected response", "api_error")
                try:
                    audio_chunks, lyrics_out, audio_mime = _extract_lyria_audio(payload)
                except ValueError as exc:
                    return fail(str(exc), "api_error")
                if audio_chunks:
                    ext = "wav" if "wav" in audio_mime else "mp3"
                    path = str(save_bytes_music(b"".join(audio_chunks), prefix="google", extension=ext))
                    return success_response(
                        audio=path, model=model_id, prompt=prompt, format=ext,
                        provider=self.name,
                        lyrics="\n".join(lyrics_out) if lyrics_out else None,
                        extra={"input_image_count": len(refs),
                               "instrumental": instrumental is True})
                reason = _terminal_no_audio_reason(payload)
                if reason:
                    return fail(f"Google music generation returned no audio: {reason}", "empty_response")
            finally:
                response.close()
        return fail("Google music generation response missing audio data", "empty_response")


def register(ctx) -> None:
    """Plugin entry point — wire ``GoogleMusicGenProvider`` into the registry."""
    ctx.register_music_gen_provider(GoogleMusicGenProvider())
