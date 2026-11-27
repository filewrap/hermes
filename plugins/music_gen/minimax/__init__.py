"""MiniMax music generation backend.

Port of the external agent's ``extensions/minimax/music-generation-provider.ts``: a single
blocking ``POST /v1/music_generation`` against ``https://api.minimax.io`` with
``stream: true`` / ``output_format: "hex"`` — hex audio arrives either as SSE
``data:`` frames or as one JSON payload (``data.audio`` hex, or a 24h URL).
Auth via ``MINIMAX_API_KEY``.

Note: MiniMax's paid Music Generation API closed to new users in August 2026 and
the ``-free`` models were discontinued — this backend serves existing paying
accounts (``music-2.6`` / ``music-cover``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    save_bytes_music,
    save_url_music,
    success_response,
)
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "music-2.6"
DEFAULT_BASE_URL = "https://api.minimax.io"
DEFAULT_TIMEOUT_S = 300.0
_MAX_AUDIO_BYTES = 200 * 1024 * 1024
_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

MINIMAX_MUSIC_MODELS: list[dict[str, Any]] = [
    {"id": "music-2.6", "display": "MiniMax Music 2.6", "speed": "~60-180s",
     "strengths": "Full songs with vocals or instrumental.", "price": "per-song",
     "modalities": ["text"], "supports_lyrics": True, "supports_instrumental": True,
     "formats": ["mp3"]},
    {"id": "music-2.6-free", "display": "MiniMax Music 2.6 Free", "speed": "~60-180s",
     "strengths": "Free tier of Music 2.6 (discontinued for new users).", "price": "free",
     "modalities": ["text"], "supports_lyrics": True, "supports_instrumental": True,
     "formats": ["mp3"]},
    {"id": "music-cover", "display": "MiniMax Music Cover", "speed": "~60-180s",
     "strengths": "New rendition of a reference track.", "price": "per-song",
     "modalities": ["text"], "supports_lyrics": True, "supports_instrumental": True,
     "formats": ["mp3"]},
    {"id": "music-cover-free", "display": "MiniMax Music Cover Free", "speed": "~60-180s",
     "strengths": "Free tier of Music Cover (discontinued for new users).", "price": "free",
     "modalities": ["text"], "supports_lyrics": True, "supports_instrumental": True,
     "formats": ["mp3"]},
]


def _resolve_base_url() -> str:
    try:
        from hermes_cli.config import cfg_get, load_config
        value = cfg_get(load_config(), "music_gen", "minimax", "base_url")
    except Exception as exc:
        logger.debug("Could not read music_gen.minimax.base_url: %s", exc, exc_info=True)
        value = None
    if isinstance(value, str) and value.strip():
        return value.strip().rstrip("/")
    return DEFAULT_BASE_URL


def _assert_base_resp(payload: Any, what: str) -> None:
    """MiniMax's ``base_resp.status_code`` is 0 on success; anything else is an API error."""
    base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
    if not isinstance(base_resp, dict):
        return
    code = base_resp.get("status_code")
    if code not in (None, 0, "0"):
        raise ValueError(f"{what} failed: {base_resp.get('status_msg') or code}")


def _decode_hex(data: str, max_bytes: int) -> bytes:
    trimmed = data.strip()
    if not _HEX_RE.match(trimmed) or len(trimmed) % 2:
        raise ValueError("MiniMax music generation returned malformed hex audio data")
    if len(trimmed) // 2 > max_bytes:
        raise ValueError(f"MiniMax generated music exceeds {max_bytes} bytes")
    return bytes.fromhex(trimmed)


def _decode_possible_text(data: str) -> str:
    trimmed = data.strip()
    if not trimmed:
        return ""
    if _HEX_RE.match(trimmed) and len(trimmed) % 2 == 0:
        try:
            return bytes.fromhex(trimmed).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            return trimmed
    return trimmed


def _read_sse_track(response: Any, max_bytes: int) -> bytes:
    """Assemble hex audio from SSE ``data:`` frames; ``data.status == "2"`` completes."""
    chunks: list[bytes] = []
    decoded = 0
    completed = False
    for line in response.iter_lines(decode_unicode=True):
        if not isinstance(line, str):
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        frame = json.loads(data)
        _assert_base_resp(frame, "MiniMax music generation")
        entry = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        if str(entry.get("status") or "") == "2":
            completed = True
        audio = entry.get("audio")
        if isinstance(audio, str) and audio.strip():
            chunk = _decode_hex(audio, max_bytes - decoded)
            chunks.append(chunk)
            decoded += len(chunk)
    if not completed:
        raise ValueError("MiniMax music generation stream ended without completion")
    track = b"".join(chunks)
    if not track:
        raise ValueError("MiniMax music generation response missing audio output")
    return track


def _parse_json_payload(payload: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """``(hex_audio, audio_url, lyrics, task_id)`` from a non-streaming MiniMax payload."""
    _assert_base_resp(payload, "MiniMax music generation")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    audio = payload.get("audio") or data.get("audio")
    audio_url = payload.get("audio_url") or data.get("audio_url")
    audio_str = audio if isinstance(audio, str) and audio.strip() else None
    if audio_str and audio_str.startswith(("http://", "https://")):
        audio_url = audio_url or audio_str
        audio_str = None
    lyrics = _decode_possible_text(str(payload.get("lyrics") or data.get("lyrics") or ""))
    task_id = payload.get("task_id") if isinstance(payload.get("task_id"), str) else None
    return audio_str, audio_url if isinstance(audio_url, str) else None, lyrics or None, task_id


class MiniMaxMusicGenProvider(MusicGenProvider):
    """MiniMax Music 2.6 / Cover backend over the hosted ``/v1/music_generation`` API."""

    name = "minimax"
    display_name = "MiniMax"

    def _api_key(self) -> str:
        try:
            return (get_secret("MINIMAX_API_KEY") or "").strip()
        except Exception as exc:
            logger.debug("MiniMax credential resolution failed: %s", exc, exc_info=True)
            return ""

    def is_available(self) -> bool:
        return bool(self._api_key())

    def list_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in MINIMAX_MUSIC_MODELS]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": "MiniMax", "badge": "paid",
                "env_vars": [{"key": "MINIMAX_API_KEY", "prompt": "MiniMax API key", "url": "https://platform.minimax.io/"}],
                "tag": "Music 2.6 / Music Cover — full songs with vocals or instrumental (existing paying accounts)"}

    def capabilities(self) -> dict[str, Any]:
        # No duration support: MiniMax takes no duration parameter (the tool drops it).
        return {"modalities": ["text"], "formats": ["mp3"],
                "supports_lyrics": True, "supports_instrumental": True,
                "supports_duration": False, "supports_format": True,
                "supports_seed": False, "max_reference_images": 0}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = "mp3", reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del duration, reference_image_urls, seed, kwargs  # no duration / image / seed params on this API
        prompt = (prompt or "").strip()
        model_id = (model or "").strip() or DEFAULT_MODEL

        def fail(error: str, error_type: str) -> dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt, format=format)

        if not prompt:
            return fail("prompt is required.", "missing_prompt")
        if lyrics and instrumental is True:
            return fail("MiniMax music generation cannot use lyrics when instrumental=true.",
                        "capability_unsupported")
        if format and format != "mp3":
            return fail("MiniMax music generation currently supports mp3 output only.",
                        "capability_unsupported")
        api_key = self._api_key()
        if not api_key:
            return fail("No MiniMax credential: set MINIMAX_API_KEY or run `hermes auth add minimax`.",
                        "missing_credentials")
        body = {
            "model": model_id, "prompt": prompt,
            **({"is_instrumental": True} if instrumental is True else {}),
            **({"lyrics": lyrics.strip()} if lyrics and lyrics.strip() else
               ({} if instrumental is True else {"lyrics_optimizer": True})),
            "stream": True, "output_format": "hex",
            "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"},
        }
        try:
            response = requests.post(
                f"{_resolve_base_url()}/v1/music_generation",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body, timeout=DEFAULT_TIMEOUT_S, stream=True)
        except Exception as exc:
            logger.warning("MiniMax music request failed: %s", exc, exc_info=True)
            return fail(f"MiniMax music generation failed: {exc}", "api_error")
        try:
            if response.status_code >= 400:
                detail = response.text[:300]
                try:
                    detail = str((response.json().get("base_resp") or {}).get("status_msg") or detail)
                except Exception as exc:
                    logger.debug("MiniMax error detail unreadable: %s", exc, exc_info=True)
                return fail(f"MiniMax rejected the request (HTTP {response.status_code}): {detail}", "api_error")
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type.startswith("audio/"):
                ext = content_type.split("/", 1)[1].split(";", 1)[0].strip() or "mp3"
                raw = response.content
                if len(raw) > _MAX_AUDIO_BYTES:
                    return fail(f"MiniMax generated music exceeds {_MAX_AUDIO_BYTES} bytes", "api_error")
                if not raw:
                    return fail("MiniMax music generation response missing audio output", "empty_response")
                path = str(save_bytes_music(raw, prefix="minimax", extension=ext))
                return success_response(audio=path, model=model_id, prompt=prompt,
                                        format=format, provider=self.name)
            if "text/event-stream" in content_type:
                raw = _read_sse_track(response, _MAX_AUDIO_BYTES)
                path = str(save_bytes_music(raw, prefix="minimax", extension="mp3"))
                return success_response(audio=path, model=model_id, prompt=prompt,
                                        format=format, provider=self.name)
            try:
                payload = response.json()
            except ValueError as exc:
                return fail(f"MiniMax music generation returned an unreadable response: {exc}", "api_error")
            if not isinstance(payload, dict):
                return fail("MiniMax music generation returned an unexpected response", "api_error")
            hex_audio, audio_url, returned_lyrics, task_id = _parse_json_payload(payload)
            if audio_url:
                try:
                    path = str(save_url_music(audio_url, prefix="minimax"))
                except Exception as exc:
                    logger.warning("MiniMax music download failed (%s): %s", audio_url, exc, exc_info=True)
                    return fail(f"MiniMax generated music download failed: {exc}", "api_error")
                return success_response(audio=path, model=model_id, prompt=prompt,
                                        format=format, provider=self.name,
                                        lyrics=returned_lyrics,
                                        extra=({"task_id": task_id} if task_id else {}))
            if hex_audio:
                path = str(save_bytes_music(_decode_hex(hex_audio, _MAX_AUDIO_BYTES),
                                            prefix="minimax", extension="mp3"))
                return success_response(audio=path, model=model_id, prompt=prompt,
                                        format=format, provider=self.name,
                                        lyrics=returned_lyrics,
                                        extra=({"task_id": task_id} if task_id else {}))
            return fail("MiniMax music generation stream ended without completion", "empty_response")
        except ValueError as exc:
            return fail(str(exc), "api_error")
        except Exception as exc:
            logger.warning("MiniMax music gen failed (model=%s): %s", model_id, exc, exc_info=True)
            return fail(f"MiniMax music generation failed: {exc}", "api_error")
        finally:
            response.close()


def register(ctx) -> None:
    """Plugin entry point — wire ``MiniMaxMusicGenProvider`` into the registry."""
    ctx.register_music_gen_provider(MiniMaxMusicGenProvider())
