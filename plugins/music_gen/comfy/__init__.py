"""ComfyUI music generation backend.

Port of the external agent's ``extensions/comfy/index.ts`` music provider +
``workflow-runtime.ts`` (local mode): the operator supplies a ComfyUI workflow
(JSON) and the node ids that take the prompt / reference image / seed; the
provider submits it to a local ComfyUI server (default ``http://127.0.0.1:8188``)
or Comfy Cloud, polls history, and downloads the ``audio`` outputs. No lyrics,
instrumental, duration or format parameters — the workflow decides all of that.

Config lives under ``music_gen.comfy`` in ``config.yaml`` (shared keys, then a
``music:`` overlay wins — same shape as the external agent's ``plugins.entries.comfy``)::

    music_gen:
      comfy:
        mode: local            # local | cloud
        base_url: http://127.0.0.1:8188
        workflow_path: ~/.hermes/comfy-music-workflow.json   # or inline `workflow: {...}`
        prompt_node_id: "3"    # required — node whose `text` input takes the prompt
        prompt_input_name: text
        input_image_node_id: "7"   # required only when reference images are used
        output_node_id: "9"    # optional — restrict downloads to this node

Cloud mode needs ``COMFY_API_KEY`` (or ``COMFY_CLOUD_API_KEY``).
"""

from __future__ import annotations

import copy
import json
import logging
import random
import time
from pathlib import Path
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

DEFAULT_MODEL = "workflow"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_CLOUD_BASE_URL = "https://cloud.comfy.org"
DEFAULT_POLL_INTERVAL_S = 1.5
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
_RANDOM_SEED_MAX = 2 ** 48 - 1


def _read_config() -> dict[str, Any]:
    """``music_gen.comfy`` merged over its ``music:`` overlay (overlay wins)."""
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        section = cfg_get(cfg, "music_gen", "comfy")
    except Exception as exc:
        logger.debug("Could not read music_gen.comfy config: %s", exc, exc_info=True)
        return {}
    if not isinstance(section, dict):
        return {}
    merged = {k: v for k, v in section.items() if k != "music"}
    overlay = section.get("music")
    if isinstance(overlay, dict):
        merged.update(overlay)
    return merged


def _load_workflow(config: dict[str, Any]) -> dict[str, Any]:
    """Inline ``workflow`` dict, else JSON from ``workflow_path``."""
    workflow = config.get("workflow")
    if isinstance(workflow, dict):
        return copy.deepcopy(workflow)
    path = config.get("workflow_path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            "music_gen.comfy.workflow or music_gen.comfy.workflow_path is required")
    resolved = Path(path.strip()).expanduser()
    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"Comfy workflow at {resolved} could not be read: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"Comfy workflow at {resolved} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Comfy workflow at {resolved} must be a JSON object")
    return parsed


def _set_workflow_input(workflow: dict[str, Any], node_id: str, name: str, value: Any) -> None:
    node = workflow.get(node_id)
    if not isinstance(node, dict):
        raise ValueError(f'Comfy workflow missing node "{node_id}"')
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError(f'Comfy workflow node "{node_id}" is missing an inputs object')
    inputs[name] = value


def _history_entry(history: Any, prompt_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(history, dict):
        return None
    if isinstance(history.get("outputs"), dict):
        return history
    nested = history.get(prompt_id)
    return nested if isinstance(nested, dict) else None


def _output_files(entry: dict[str, Any], output_node_id: Optional[str]) -> list[dict[str, Any]]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return []
    node_ids = [output_node_id] if output_node_id else list(outputs)
    files: list[dict[str, Any]] = []
    for node_id in node_ids:
        bucket = outputs.get(node_id)
        if not isinstance(bucket, dict):
            continue
        for file in bucket.get("audio") or []:
            if isinstance(file, dict):
                files.append({"node_id": node_id, **file})
    return files


class ComfyMusicGenProvider(MusicGenProvider):
    """ComfyUI workflow backend: local server by default, Comfy Cloud with a key."""

    name = "comfy"
    display_name = "ComfyUI"

    def _mode(self, config: Optional[dict[str, Any]] = None) -> str:
        cfg = config if config is not None else _read_config()
        return "cloud" if str(cfg.get("mode") or "").strip().lower() == "cloud" else "local"

    def _api_key(self) -> str:
        try:
            return (get_secret("COMFY_API_KEY") or get_secret("COMFY_CLOUD_API_KEY") or "").strip()
        except Exception as exc:
            logger.debug("Comfy credential resolution failed: %s", exc, exc_info=True)
            return ""

    def is_available(self) -> bool:
        """Config-complete (workflow + prompt node), plus a key in cloud mode. No network calls."""
        try:
            config = _read_config()
            if self._mode(config) == "cloud" and not self._api_key():
                return False
            if config.get("workflow") is not None or config.get("workflow_path"):
                return bool(str(config.get("prompt_node_id") or "").strip())
            return False
        except Exception:  # health: allow BLE001 -- availability probe: any failure reads as unavailable
            return False

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": DEFAULT_MODEL, "display": "ComfyUI workflow", "speed": "workflow-dependent",
                 "strengths": "Operator-supplied music workflow.", "price": "local / cloud",
                 "modalities": ["text", "image"], "supports_lyrics": False,
                 "supports_instrumental": False, "supports_duration": False,
                 "formats": [], "max_reference_images": 1}]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": "ComfyUI", "badge": "local · free",
                "env_vars": [{"key": "COMFY_API_KEY", "prompt": "Comfy Cloud API key (cloud mode only)",
                              "url": "https://cloud.comfy.org/"}],
                "tag": "Operator-supplied ComfyUI music workflow — local server or Comfy Cloud; "
                       "configure under music_gen.comfy in config.yaml"}

    def capabilities(self) -> dict[str, Any]:
        # The workflow decides lyrics / duration / format; the tool drops those params.
        return {"modalities": ["text", "image"], "formats": [],
                "supports_lyrics": False, "supports_instrumental": False,
                "supports_duration": False, "supports_format": False,
                "supports_seed": False, "max_reference_images": 1}

    def generate(
        self, prompt: str, *, model: Optional[str] = None, lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None, duration: Optional[int] = None,
        format: str = "mp3", reference_image_urls: Optional[list[str]] = None,
        seed: Optional[int] = None, **kwargs: Any,
    ) -> dict[str, Any]:
        del lyrics, instrumental, duration, format, seed, kwargs  # the workflow owns all of these
        prompt = (prompt or "").strip()
        model_id = (model or "").strip() or DEFAULT_MODEL
        refs = [r for r in (reference_image_urls or []) if isinstance(r, str) and r.strip()]

        def fail(error: str, error_type: str) -> dict[str, Any]:
            return error_response(error=error, error_type=error_type, provider=self.name,
                                  model=model_id, prompt=prompt)

        if not prompt:
            return fail("prompt is required.", "missing_prompt")
        if len(refs) > 1:
            return fail("Comfy music generation supports at most one reference image.",
                        "capability_unsupported")
        config = _read_config()
        mode = self._mode(config)
        base_url = str(config.get("base_url") or
                       (DEFAULT_CLOUD_BASE_URL if mode == "cloud" else DEFAULT_LOCAL_BASE_URL)).rstrip("/")
        api_key = self._api_key() if mode == "cloud" else ""
        if mode == "cloud" and not api_key:
            return fail("Comfy Cloud API key missing: set COMFY_API_KEY.", "missing_credentials")
        prompt_node_id = str(config.get("prompt_node_id") or "").strip()
        if not prompt_node_id:
            return fail("music_gen.comfy.prompt_node_id is required.", "missing_config")
        try:
            workflow = _load_workflow(config)
            _set_workflow_input(workflow, prompt_node_id,
                                str(config.get("prompt_input_name") or "text"), prompt)
            seed_node_id = str(config.get("seed_node_id") or "").strip()
            if seed_node_id:
                _set_workflow_input(workflow, seed_node_id,
                                    str(config.get("seed_input_name") or "seed"),
                                    random.randint(0, _RANDOM_SEED_MAX))
        except ValueError as exc:
            return fail(str(exc), "missing_config")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        extra_headers = config.get("headers")
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    headers[key] = value
        poll_interval = float(config.get("poll_interval_ms") or 0) / 1000.0 or DEFAULT_POLL_INTERVAL_S
        timeout_s = float(config.get("timeout_ms") or 0) / 1000.0 or DEFAULT_TIMEOUT_S
        deadline = time.monotonic() + timeout_s
        session = requests.Session()
        try:
            upload_error = self._upload_reference_image(
                session, base_url, headers, mode, config, workflow, refs, deadline, fail)
            if upload_error is not None:
                return upload_error
            submit: dict[str, Any] = {"prompt": workflow}
            if mode == "cloud" and api_key:
                submit["extra_data"] = {"api_key_comfy_org": api_key}
            submit_path = "/api/prompt" if mode == "cloud" else "/prompt"
            queued = session.post(f"{base_url}{submit_path}", headers=headers, json=submit,
                                  timeout=max(1.0, deadline - time.monotonic()))
            if queued.status_code >= 400:
                return fail(f"Comfy workflow submit failed (HTTP {queued.status_code}): "
                            f"{queued.text[:300]}", "api_error")
            prompt_id = queued.json().get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                return fail("Comfy workflow submit response missing prompt_id", "api_error")
            history = self._wait_history(session, base_url, headers, mode, prompt_id,
                                         deadline, poll_interval)
            entry = _history_entry(history, prompt_id)
            if entry is None:
                return fail(f"Comfy history response missing outputs for prompt {prompt_id}",
                            "empty_response")
            tracks, files, download_error = self._download_outputs(
                session, base_url, headers, mode, config, entry, prompt_id, deadline, fail)
            if download_error is not None:
                return download_error
            return success_response(
                audio=tracks[0], model=model_id, prompt=prompt,
                format=Path(tracks[0]).suffix.lstrip(".") or "mp3", provider=self.name,
                extra={"prompt_id": prompt_id,
                       "output_node_ids": sorted({f["node_id"] for f in files}),
                       **({"tracks": tracks} if len(tracks) > 1 else {})})
        except requests.RequestException as exc:
            return fail(f"Comfy music generation failed: {exc}", "api_error")
        except ValueError as exc:
            return fail(str(exc), "api_error")
        finally:
            session.close()

    def _upload_reference_image(
        self, session: requests.Session, base_url: str, headers: dict[str, str], mode: str,
        config: dict[str, Any], workflow: dict[str, Any], refs: list[str], deadline: float,
        fail: Any,
    ) -> Optional[dict[str, Any]]:
        """Upload the reference image and wire its filename into the workflow; None, or a fail dict."""
        if not refs:
            return None
        input_image_node_id = str(config.get("input_image_node_id") or "").strip()
        if not input_image_node_id:
            return fail("Comfy music generation with a reference image requires "
                        "music_gen.comfy.input_image_node_id to be configured.",
                        "missing_config")
        try:
            raw, mime = load_reference_image_bytes(refs[0])
        except (OSError, ValueError) as exc:
            return fail(f"Could not load reference image: {exc}", "invalid_request")
        upload_path = "/api/upload/image" if mode == "cloud" else "/upload/image"
        upload_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        uploaded = session.post(
            f"{base_url}{upload_path}", headers=upload_headers,
            files={"image": ("input." + mime.split("/", 1)[1].split(";", 1)[0].strip(),
                            raw, mime)},
            data={"type": "input", "overwrite": "true"},
            timeout=max(1.0, deadline - time.monotonic()))
        if uploaded.status_code >= 400:
            return fail(f"Comfy image upload failed (HTTP {uploaded.status_code}): "
                        f"{uploaded.text[:300]}", "api_error")
        uploaded_name = uploaded.json().get("filename") or uploaded.json().get("name")
        if not uploaded_name:
            return fail("Comfy image upload response missing filename", "api_error")
        _set_workflow_input(workflow, input_image_node_id,
                            str(config.get("input_image_input_name") or "image"),
                            uploaded_name)
        return None

    def _download_outputs(
        self, session: requests.Session, base_url: str, headers: dict[str, str], mode: str,
        config: dict[str, Any], entry: dict[str, Any], prompt_id: str, deadline: float,
        fail: Any,
    ) -> tuple[list[str], list[dict[str, Any]], Optional[dict[str, Any]]]:
        """Download every audio output; (tracks, files, None), or ([], [], fail dict)."""
        output_node_id = str(config.get("output_node_id") or "").strip() or None
        files = _output_files(entry, output_node_id)
        if not files:
            return [], [], fail(f"Comfy workflow {prompt_id} completed without music outputs",
                                "empty_response")
        tracks: list[str] = []
        for file in files:
            file_name = file.get("filename") or file.get("name")
            if not isinstance(file_name, str) or not file_name:
                return [], [], fail("Comfy output entry missing filename", "api_error")
            view_path = "/api/view" if mode == "cloud" else "/view"
            downloaded = session.get(
                f"{base_url}{view_path}",
                params={"filename": file_name, "subfolder": file.get("subfolder") or "",
                        "type": file.get("type") or "output"},
                headers=headers, timeout=max(1.0, deadline - time.monotonic()))
            if downloaded.status_code >= 400:
                return [], [], fail(f"Comfy output download failed (HTTP {downloaded.status_code}): "
                                    f"{downloaded.text[:300]}", "api_error")
            raw = downloaded.content
            if len(raw) > DEFAULT_MAX_BYTES:
                return [], [], fail(f"Comfy output exceeds {DEFAULT_MAX_BYTES} bytes", "api_error")
            if not raw:
                return [], [], fail("Comfy output download was empty", "empty_response")
            ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "mp3"
            ext = ext if ext in ("mp3", "wav", "ogg", "flac", "m4a") else "mp3"
            tracks.append(str(save_bytes_music(raw, prefix="comfy", extension=ext)))
        return tracks, files, None

    def _wait_history(
        self, session: requests.Session, base_url: str, headers: dict[str, str],
        mode: str, prompt_id: str, deadline: float, poll_interval: float,
    ) -> Any:
        """Poll until the run completes; cloud checks job status first, local reads history directly."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Comfy workflow {prompt_id} did not finish in time")
            timeout = max(1.0, min(30.0, remaining))
            if mode == "cloud":
                status = session.get(f"{base_url}/api/job/{prompt_id}/status",
                                     headers=headers, timeout=timeout)
                status.raise_for_status()
                body = status.json()
                state = str(body.get("status") or "")
                if state == "completed":
                    history = session.get(f"{base_url}/api/history_v2/{prompt_id}",
                                          headers=headers, timeout=timeout)
                    history.raise_for_status()
                    return history.json()
                if state in ("failed", "cancelled"):
                    raise ValueError(f"Comfy workflow {state}: "
                                     f"{body.get('error') or body.get('message') or prompt_id}")
            else:
                history = session.get(f"{base_url}/history/{prompt_id}",
                                      headers=headers, timeout=timeout)
                history.raise_for_status()
                entry = _history_entry(history.json(), prompt_id)
                if entry is not None and (entry.get("status", {}).get("completed") is True
                                          or isinstance(entry.get("outputs"), dict)):
                    return entry
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def register(ctx) -> None:
    """Plugin entry point — wire ``ComfyMusicGenProvider`` into the registry."""
    ctx.register_music_gen_provider(ComfyMusicGenProvider())
