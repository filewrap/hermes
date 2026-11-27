"""OpenRouter music generation backend.

Port of the external agent's ``extensions/openrouter/music-generation-provider.ts``: Lyria 3
(Pro default, Clip) behind OpenRouter's chat-completions API with
``modalities: ["text", "audio"]`` — audio arrives as base64 slices in streamed
``delta.audio.data`` frames plus an optional ``delta.audio.transcript``. One
reference image rides along as an ``image_url`` content part. Credentials resolve
through the runtime provider ladder (pooled/OAuth key counts), like the
OpenRouter video plugin.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, Dict, List, Optional

import requests

from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    save_bytes_music,
    success_response,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/lyria-3-pro-preview"
CLIP_MODEL = "google/lyria-3-clip-preview"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_S = 180.0
_MAX_AUDIO_BYTES = 200 * 1024 * 1024

OPENROUTER_MUSIC_MODELS: list[dict[str, Any]] = [
    {"id": DEFAULT_MODEL, "display": "Lyria 3 Pro (via OpenRouter)", "speed": "~60-180s",
     "strengths": "Full-length songs up to ~3 minutes.", "price": "per-request",
     "modalities": ["text", "image"], "supports_lyrics": True,
     "supports_instrumental": True, "supports_duration": True, "formats": ["mp3", "wav"],
     "max_duration": 180, "min_duration": 1, "max_reference_images": 1},
    {"id": CLIP_MODEL, "display": "Lyria 3 Clip (via OpenRouter)", "speed": "~30-60s",
     "strengths": "Short clips up to 30s.", "price": "per-request",
     "modalities": ["text", "image"], "supports_lyrics": True,
     "supports_instrumental": True, "supports_duration": True, "formats": ["mp3", "wav"],
     "max_duration": 180, "min_duration": 1, "max_reference_images": 1},
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


def _acceptable_image_ref(value: str) -> bool:
    """OpenRouter fetches the image itself: public HTTPS or inline data:image/ only."""
    lowered = value.lower()
    return lowered.startswith("https://") or lowered.startswith("data:image/")


def _read_audio_stream(response: Any, max_bytes: int) -> tuple[bytes, str]:
    """``(audio_bytes, transcript)`` from the SSE stream. ``delta.audio.data`` slices
    are fragments of one base64 stream, so only complete quartets decode per line."""
    buffers: list[bytes] = []
    audio_bytes = 0
    remainder = ""
    transcript_parts: list[str] = []
    transcript_bytes = 0
    done = False
    for line in response.iter_lines(decode_unicode=True):
        if not isinstance(line, str):
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            done = True
            break
        try:
            payload = json.loads(data)
        except ValueError as exc:
            raise ValueError(f"OpenRouter music generation returned malformed stream data: {exc}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message") or "unknown provider stream error"
            raise ValueError(f"OpenRouter music generation failed: {message}")
        choices = payload.get("choices") if isinstance(payload, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        delta = first.get("delta") if isinstance(first, dict) else None
        audio = delta.get("audio") if isinstance(delta, dict) else None
        if not isinstance(audio, dict):
            continue
        chunk_b64 = audio.get("data")
        if isinstance(chunk_b64, str) and chunk_b64:
            combined = remainder + chunk_b64
            complete = len(combined) - (len(combined) % 4)
            remainder = combined[complete:]
            canonical = _canonical_b64(combined[:complete])
            if canonical is None:
                raise ValueError("OpenRouter music generation returned malformed base64 audio data")
            raw = base64.b64decode(canonical)
            if audio_bytes + len(raw) > max_bytes:
                raise ValueError(f"OpenRouter music generation audio exceeded {max_bytes} bytes")
            audio_bytes += len(raw)
            buffers.append(raw)
        transcript = audio.get("transcript")
        if isinstance(transcript, str) and transcript:
            transcript_bytes += len(transcript.encode("utf-8"))
            if transcript_bytes > max_bytes:
                raise ValueError(f"OpenRouter music generation transcript exceeded {max_bytes} bytes")
            transcript_parts.append(transcript)
    if remainder:
        canonical = _canonical_b64(remainder)
        if canonical is None:
            raise ValueError("OpenRouter music generation returned malformed base64 audio data")
        raw = base64.b64decode(canonical)
        if audio_bytes + len(raw) > max_bytes:
            raise ValueError(f"OpenRouter music generation audio exceeded {max_bytes} bytes")
        buffers.append(raw)
    if not done:
        raise ValueError("OpenRouter music generation stream ended before completion")
    return b"".join(buffers), "".join(transcript_parts)


class OpenRouterMusicGenProvider(MusicGenProvider):
    """Lyria 3 via OpenRouter's streaming chat-completions audio endpoint."""

    name = "openrouter"
    display_name = "OpenRouter"

    def _credentials(self) -> tuple[str, str]:
        """``(api_key, base_url)`` from the runtime resolver chat uses, so a pooled or OAuth credential counts
        and a multiplexed profile never spends the launch profile's ``os.environ`` key; raises on failure."""
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="openrouter")
        return (str(runtime.get("api_key") or "").strip(),
                (str(runtime.get("base_url") or "").strip() or DEFAULT_BASE_URL).rstrip("/"))

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/NousResearch/hermes-agent", "X-Title": "Hermes Agent"}

    def is_available(self) -> bool:
        try:
            return bool(self._credentials()[0])
        except Exception as exc:
            logger.debug("OpenRouter music credential resolution failed: %s", exc, exc_info=True)
            return False

    def list_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in OPENROUTER_MUSIC_MODELS]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": "OpenRouter", "badge": "paid",
                "env_vars": [{"key": "OPENROUTER_API_KEY", "prompt": "OpenRouter API key",
                              "url": "https://openrouter.ai/settings/keys"}],
                "tag": "Lyria 3 Pro / Clip — text-to-music, one reference image; uses OPENROUTER_API_KEY"}

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text", "image"], "formats": ["mp3", "wav"],
                "max_duration": 180, "min_duration": 1,
                "supports_lyrics": True, "supports_instrumental": True,
                "supports_duration": True, "supports_format": True,
                "supports_seed": False, "max_reference_images": 1}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = "mp3", reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del seed, kwargs
        prompt = (prompt or "").strip()
        model_id = (model or "").strip() or DEFAULT_MODEL
        refs = [r for r in (reference_image_urls or []) if isinstance(r, str) and r.strip()]
        out_format = (format or "mp3").strip().lower()

        def fail(error: str, error_type: str) -> dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt, format=out_format)

        if not prompt:
            return fail("prompt is required.", "missing_prompt")
        if len(refs) > 1:
            return fail("OpenRouter music generation supports at most one reference image.",
                        "capability_unsupported")
        for ref in refs:
            if not _acceptable_image_ref(ref):
                return fail("image inputs must be public HTTPS URLs or data:image/ URLs "
                            "(OpenRouter fetches them itself)", "invalid_request")
        try:
            api_key, base_url = self._credentials()
        except Exception as exc:
            logger.debug("OpenRouter music credential resolution failed: %s", exc, exc_info=True)
            return fail(f"Could not resolve OpenRouter credentials: {exc}", "missing_credentials")
        if not api_key:
            return fail("No OpenRouter credential: set OPENROUTER_API_KEY or run `hermes auth add openrouter`.",
                        "missing_credentials")
        parts = [prompt]
        if instrumental is True:
            parts.append("Instrumental only. No vocals, no sung lyrics, no spoken word.")
        if lyrics and lyrics.strip():
            parts.append(f"Lyrics:\n{lyrics.strip()}")
        if isinstance(duration, int):
            parts.append(f"Target duration: about {duration} seconds.")
        text = "\n\n".join(parts)
        content: Any = text
        if refs:
            content = [{"type": "text", "text": text},
                       {"type": "image_url", "image_url": {"url": refs[0]}}]
        try:
            response = requests.post(
                f"{base_url}/chat/completions", headers=self._headers(api_key),
                json={"model": model_id, "messages": [{"role": "user", "content": content}],
                      "modalities": ["text", "audio"], "audio": {"format": out_format},
                      "stream": True},
                timeout=DEFAULT_TIMEOUT_S, stream=True)
        except Exception as exc:
            logger.warning("OpenRouter music request failed: %s", exc, exc_info=True)
            return fail(f"OpenRouter music generation failed: {exc}", "api_error")
        try:
            if response.status_code >= 400:
                detail = response.text[:300]
                try:
                    detail = str((response.json().get("error") or {}).get("message") or detail)
                except Exception as exc:
                    logger.debug("OpenRouter error detail unreadable: %s", exc, exc_info=True)
                return fail(f"OpenRouter rejected the request (HTTP {response.status_code}): {detail}",
                            "api_error")
            audio_bytes, transcript = _read_audio_stream(response, _MAX_AUDIO_BYTES)
        except ValueError as exc:
            return fail(str(exc), "api_error")
        except Exception as exc:
            logger.warning("OpenRouter music gen failed (model=%s): %s", model_id, exc, exc_info=True)
            return fail(f"OpenRouter music generation failed: {exc}", "api_error")
        finally:
            response.close()
        if not audio_bytes:
            return fail("OpenRouter music generation response missing audio data", "empty_response")
        ext = "mp3" if out_format == "mp3" else "wav"
        path = str(save_bytes_music(audio_bytes, prefix="openrouter", extension=ext))
        return success_response(
            audio=path, model=model_id, prompt=prompt, format=ext, provider=self.name,
            lyrics=transcript or None,
            extra={"input_image_count": len(refs), "instrumental": instrumental is True,
                   "requested_format": out_format})


def register(ctx) -> None:
    """Plugin entry point — wire ``OpenRouterMusicGenProvider`` into the registry."""
    ctx.register_music_gen_provider(OpenRouterMusicGenProvider())
