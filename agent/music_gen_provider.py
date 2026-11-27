"""Music Generation Provider ABC.

Providers register via ``PluginContext.register_music_gen_provider()`` and live
in ``<repo>/plugins/music_gen/<name>/`` (built-in) or
``~/.hermes/plugins/music_gen/<name>/``; mirrors ``agent/video_gen_provider.py``.
One tool covers text-to-music: ``lyrics`` supplies exact sung words when the
user provides them, ``instrumental`` forces no vocals, ``reference_image_urls``
condition the generation on images (style/mood refs — the audio is still
generated, never an edit of the image). Five providers ship in-tree (fal,
minimax, google, openrouter, comfy); the API keys are the user's to supply.

Response shape (:func:`success_response` / :func:`error_response`): ``success``,
``audio`` (URL or absolute path), ``model``, ``prompt``, ``duration`` (seconds, 0
if n/a), ``format``, ``provider``; plus ``lyrics`` when the provider returns the
sung words, and ``error`` / ``error_type`` only when ``success`` is False.

Generated tracks are saved under ``$HERMES_HOME/cache/generated/tool-music-generation/``
(the ``music_generate`` tool's task records live in the ``tasks/`` subdir there).
"""

from __future__ import annotations

import abc
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import provider_media
from agent.provider_base import CatalogProviderBase

logger = logging.getLogger(__name__)


# Advertised as an enum hint in the tool schema; providers may accept a narrower
# or wider set and are responsible for clamping. The external agent's music-generate tool
# normalises format to exactly these two.
COMMON_FORMATS: tuple[str, ...] = ("mp3", "wav")
DEFAULT_FORMAT = "mp3"


class MusicGenProvider(CatalogProviderBase):
    """Abstract base class for a music generation backend: implement :attr:`name`
    and :meth:`generate`. ``list_models`` entries are **model families** and may
    add ``speed`` / ``strengths`` / ``price`` / advisory ``modalities``."""

    def capabilities(self) -> dict[str, Any]:
        """Supported features (keys below, all optional) used for soft validation,
        capability-gated params in the dynamic ``music_generate`` schema, and the
        picker. Default fails closed: text-only, no lyrics, no vocal toggle.
        Providers may also declare ``supports_duration`` (default True) and
        ``supports_format`` (default True); a model entry in :meth:`list_models`
        may narrow any of these per model (see :meth:`effective_capabilities`)."""
        return {
            "modalities": ["text"], "formats": list(COMMON_FORMATS),
            "max_duration": 60, "min_duration": 1,
            "supports_lyrics": False, "supports_instrumental": False,
            "supports_seed": False, "max_reference_images": 0,
        }

    @abc.abstractmethod
    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = DEFAULT_FORMAT, reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate music from a prompt; return :func:`success_response` /
        :func:`error_response`. ``lyrics`` is exact sung words (only when the
        user supplied them); ``instrumental=True`` forces no vocals — never
        invent lyrics. ``reference_image_urls`` are conditioning refs only.
        Unknown ``kwargs`` MUST be ignored."""

    def effective_capabilities(self, model: Optional[str] = None) -> dict[str, Any]:
        """``capabilities()`` merged with the matching :meth:`list_models` entry:
        a model entry may narrow ``supports_lyrics`` / ``supports_instrumental`` /
        ``supports_duration`` / ``formats`` / ``max_duration`` / ``min_duration`` /
        ``max_reference_images``. The ``music_generate`` tool normalises each
        request against this before calling :meth:`generate`."""
        caps = dict(self.capabilities())
        if not model:
            return caps
        entry = next(
            (m for m in self.list_models() if isinstance(m, dict) and m.get("id") == model), None
        )
        if not entry:
            return caps
        for key in ("supports_lyrics", "supports_instrumental", "supports_duration",
                    "formats", "max_duration", "min_duration", "max_reference_images"):
            if entry.get(key) is not None:
                caps[key] = entry[key]
        return caps


def normalize_reference_images(value: Any) -> Optional[list[str]]:
    """Coerce a str or list into a clean list of non-blank strings; ``None`` when
    nothing usable remains so providers treat "no refs" as one sentinel."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] or None


_GENERATED_MUSIC_KIND = f"{provider_media.GENERATED_SUBDIR}/tool-music-generation"


def load_reference_image_bytes(
    url: str, *, timeout: float = 30.0, max_bytes: int = 20 * 1024 * 1024,
) -> tuple[bytes, str]:
    """``(bytes, mime_type)`` for a reference image: a ``data:`` URL decodes inline,
    an ``https://`` URL downloads through the SSRF fetch guard. Raises on any
    failure so callers can surface it instead of sending a broken ref."""
    import base64

    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        mime = header[5:].split(";", 1)[0].strip() or "image/png"
        if ";base64" in header:
            raw = base64.b64decode(payload, validate=True)
        else:
            from urllib.parse import unquote_to_bytes
            raw = unquote_to_bytes(payload)
        if len(raw) > max_bytes:
            raise ValueError(f"Reference image exceeds {max_bytes} bytes")
        if not raw:
            raise ValueError("Reference image was empty")
        return raw, mime
    from tools.fetch_guard import SsrFBlockedError, stream_with_ssrf_guard

    try:
        guarded = stream_with_ssrf_guard(url, timeout=timeout, max_redirects=5)
    except SsrFBlockedError as exc:
        raise ValueError(f"Reference image URL failed the SSRF safety check: {url} ({exc})") from exc
    try:
        response = guarded.response
        if not response.is_success:
            response.read()
            response.raise_for_status()
        mime = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if mime and not mime.startswith("image/"):
            raise ValueError(f"Reference image URL returned Content-Type {mime or '(missing)'}")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(chunk_size=256 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Reference image exceeds {max_bytes} bytes")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            raise ValueError(f"Reference image at {url} was empty (0 bytes).")
        return raw, mime or "image/png"
    finally:
        guarded.close()


def save_b64_music(b64_data: str, *, prefix: str = "music", extension: str = "mp3") -> Path:
    """Decode base64 audio data into ``$HERMES_HOME/cache/generated/tool-music-generation/``; return the path."""
    return provider_media.save_b64(_GENERATED_MUSIC_KIND, b64_data, prefix=prefix, extension=extension)


def save_bytes_music(raw: bytes, *, prefix: str = "music", extension: str = "mp3") -> Path:
    """Write raw audio bytes (e.g. an HTTP download body) to the cache."""
    return provider_media.save_bytes(_GENERATED_MUSIC_KIND, raw, prefix=prefix, extension=extension)


_URL_MUSIC_CONTENT_TYPES = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/ogg": "ogg", "audio/flac": "flac", "audio/mp4": "m4a", "audio/aac": "aac",
}


def save_url_music(
    url: str,
    *,
    prefix: str = "music",
    timeout: float = 180.0,
    max_bytes: int = 200 * 1024 * 1024,
    headers: Optional[dict[str, str]] = None,
    require_music_content_type: bool = False,
    trusted_origin: bool = False,
) -> Path:
    """Download an (often ephemeral) audio URL into ``$HERMES_HOME/cache/generated/music/``;
    raises on network / HTTP / oversize / empty errors so callers can fall back to the URL.
    ``trusted_origin`` is only for URLs built from the operator's configured provider
    ``base_url`` (see ``provider_media.save_url``)."""
    return provider_media.save_url(
        _GENERATED_MUSIC_KIND, url, prefix=prefix, timeout=timeout, max_bytes=max_bytes,
        chunk_size=256 * 1024, content_types=_URL_MUSIC_CONTENT_TYPES,
        url_extensions=("mp3", "wav", "ogg", "flac", "m4a", "aac"), default_extension="mp3",
        label="Music", empty_error="Music at {url} was empty (0 bytes).",
        headers=headers, require_known_content_type=require_music_content_type,
        trusted_origin=trusted_origin,
    )


def success_response(
    *, audio: str, model: str, prompt: str, duration: int = 0, format: str = DEFAULT_FORMAT,
    provider: str, lyrics: Optional[str] = None, extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Uniform success dict; ``extra`` keys are added without overriding standard ones."""
    payload: dict[str, Any] = {
        "success": True, "audio": audio, "model": model, "prompt": prompt,
        "duration": int(duration) if duration else 0, "format": format, "provider": provider,
    }
    if lyrics is not None:
        payload["lyrics"] = lyrics
    for k, v in (extra or {}).items():
        payload.setdefault(k, v)
    return payload


def error_response(
    *, error: str, error_type: str = "provider_error", provider: str = "", model: str = "",
    prompt: str = "", format: str = "",
) -> dict[str, Any]:
    """Build a uniform error response dict."""
    return {
        "success": False, "audio": None, "error": error, "error_type": error_type, "model": model,
        "prompt": prompt, "format": format, "provider": provider,
    }
