"""Chat/multimodal content normalization for the OpenAI-compatible API server.

Split out of ``gateway.platforms.api_server`` (file-size ratchet); the names stay importable from
the facade so existing imports keep working.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any


MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts


MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array


def _cap_text(text: str) -> str:
    return text[:MAX_NORMALIZED_TEXT_LENGTH] if len(text) > MAX_NORMALIZED_TEXT_LENGTH else text


def _cap_list(items: list) -> list:
    return items[:MAX_CONTENT_LIST_SIZE] if len(items) > MAX_CONTENT_LIST_SIZE else items


def _normalize_chat_content(content: Any, *, _max_depth: int = 10, _depth: int = 0) -> str:
    """Flatten OpenAI chat content (string or typed-part array) into one plain string; non-text
    parts are skipped, recursion depth / list size / output length are bounded."""
    if _depth > _max_depth or content is None:
        return ""
    if isinstance(content, str):
        return _cap_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        total_len = 0
        for item in _cap_list(content):
            part = ""
            if isinstance(item, str):
                part = item
            elif isinstance(item, dict):
                if str(item.get("type") or "").strip().lower() in _TEXT_PART_TYPES:
                    text = item.get("text", "")
                    if text:
                        with suppress(Exception):
                            part = str(text)
            elif isinstance(item, list):
                part = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
            if part:
                part = _cap_text(part)
                parts.append(part)
                total_len += len(part)
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        return _cap_text("\n".join(parts))
    try:
        return _cap_text(str(content))
    except Exception:
        return ""


_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})


_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})


_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_image_part(part: dict[str, Any]) -> dict[str, Any]:
    """Validate one image part (Responses top-level ``image_url`` string or Chat Completions
    ``{"url", "detail"}`` dict) into the canonical vision shape; raises ValueError."""
    detail = part.get("detail")
    image_ref = part.get("image_url")
    if isinstance(image_ref, dict):
        url_value = image_ref.get("url")
        detail = image_ref.get("detail", detail)
    else:
        url_value = image_ref
    if not isinstance(url_value, str) or not url_value.strip():
        raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
    url_value = url_value.strip()
    lowered = url_value.lower()
    if lowered.startswith("data:"):
        if not lowered.startswith("data:image/") or "," not in url_value:
            raise ValueError(
                "unsupported_content_type:Only image data URLs are supported. "
                "Non-image data payloads are not supported.")
    elif not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError(
            "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs.")
    image_part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
    if detail is not None:
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
        image_part["image_url"]["detail"] = detail.strip()
    return image_part


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate multimodal content: a plain string when text-only, else canonical ``text`` /
    ``image_url`` parts (native OpenAI vision shape; Anthropic conversion happens downstream).

    Raises ``ValueError("<code>:<message>")`` with codes ``unsupported_content_type`` (file
    parts, non-image data URLs, unknown types), ``invalid_image_url``, ``invalid_content_part``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return _cap_text(content)
    if not isinstance(content, list):
        return _normalize_chat_content(content)
    normalized_parts: list[dict[str, Any]] = []
    for part in _cap_list(content):
        if isinstance(part, str):
            if part:
                normalized_parts.append({"type": "text", "text": _cap_text(part)})
            continue
        if not isinstance(part, dict):
            continue  # unknown scalars are ignored for forward compatibility (e.g. ``refusal``)
        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()
        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is not None and str(text):
                normalized_parts.append({"type": "text", "text": _cap_text(str(text))})
        elif part_type in _IMAGE_PART_TYPES:
            normalized_parts.append(_normalize_image_part(part))
        elif part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint.")
        else:
            raise ValueError(
                f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
                "Only text and image_url/input_image parts are supported.")
    if not normalized_parts:
        return ""
    # Text-only collapses to a plain string so trajectory logging and prompt caching see
    # the native shape.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))
    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """True when content has any text or image attachment.  Used to reject empty turns."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _IMAGE_PART_TYPES or (
                        ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip()):
                    return True
    return False
