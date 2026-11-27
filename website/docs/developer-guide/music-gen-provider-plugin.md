---
sidebar_position: 13
title: "Music Generation Provider Plugins"
description: "How to build a music-generation backend plugin for Hermes Agent"
---

# Building a Music Generation Provider Plugin

Music-gen provider plugins register a backend that will service every `music_generate` tool call. Five providers ship in-tree — `fal`, `minimax`, `google`, `openrouter`, `comfy` under `plugins/music_gen/<name>/` — so this page is both the contract a first provider implements and the reference for the shipped ones. Add one, by dropping a directory into `plugins/music_gen/<name>/`.

:::tip
Music-gen mirrors [Video Generation Provider Plugins](./video-gen-provider-plugin.md) almost line-for-line — if you've built a video-gen backend, you already know the shape. The main differences: the output is audio (`mp3` / `wav`), the parameters are `lyrics` / `instrumental` / `duration` / `format` instead of aspect ratio / resolution, and reference images only *condition* the generation (style/mood refs) — there is no image-to-music modality split.
:::

## The unified surface (one tool, one modality)

The `music_generate` tool (`tools/music_generation_tool.py`) exposes one modality through one parameter set. Generation runs as a **background task**: the call returns a task id at once, `action=status` polls it, `action=list` shows providers/models plus recent tasks, and a repeated identical request returns the existing task (duplicate guard) instead of generating twice. When the active backend fails, the runner falls through to the other available backends, normalising each request against that backend's capabilities first (unsupported lyrics/instrumental/duration/format are dropped and reported, never sent). An explicit `model` override pins the provider(s) whose catalog carries it and disables fallback.

- **Text-to-music** — call with `prompt` only. The provider routes to its text-to-music endpoint.
- **Lyrics** — pass `lyrics` only when the user supplied exact sung words or asked for vocal words. For song/style requests, the prompt carries the style/genre/mood — never invent lyrics.
- **Instrumental** — pass `instrumental: true` for no vocals.
- **Reference images** — pass `reference_image_urls` to condition on images. The audio is still generated; the image is never edited.

## How discovery works

Hermes scans for music-gen backends in three places:

1. **Bundled** — `<repo>/plugins/music_gen/<name>/` (auto-loaded with `kind: backend`)
2. **User** — `~/.hermes/plugins/music_gen/<name>/` (opt-in via `plugins.enabled`)
3. **Pip** — packages declaring a `hermes_agent.plugins` entry point

Each plugin's `register(ctx)` function calls `ctx.register_music_gen_provider(...)`. The active provider is picked by `music_gen.provider` in `config.yaml`. Like `video_generate`, there is no in-tree legacy backend — every provider is a plugin.

## Directory structure

```
plugins/music_gen/my-backend/
├── __init__.py      # MusicGenProvider subclass + register()
└── plugin.yaml      # Manifest with kind: backend
```

## The MusicGenProvider ABC

Subclass `agent.music_gen_provider.MusicGenProvider`. Required: `name` property and `generate()` method.

```python
# plugins/music_gen/my-backend/__init__.py
from typing import Any, Dict, List, Optional
import os

from agent.music_gen_provider import (
    MusicGenProvider,
    error_response,
    success_response,
)


class MyMusicGenProvider(MusicGenProvider):
    @property
    def name(self) -> str:
        return "my-backend"

    @property
    def display_name(self) -> str:
        return "My Backend"

    def is_available(self) -> bool:
        return bool(os.environ.get("MY_API_KEY"))

    def list_models(self) -> List[Dict[str, Any]]:
        # Each entry is a model FAMILY — a name the user picks once.
        return [
            {
                "id": "fast",
                "display": "Fast",
                "speed": "~30s",
                "strengths": "Cheapest tier",
                "price": "$0.05/s",
                "modalities": ["text"],  # advisory
            },
        ]

    def default_model(self) -> Optional[str]:
        return "fast"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"],
            "formats": ["mp3", "wav"],
            "min_duration": 1,
            "max_duration": 60,
            "supports_lyrics": True,
            "supports_instrumental": True,
            "supports_seed": False,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "My Backend",
            "badge": "paid",
            "tag": "Short description shown in `hermes tools`",
            "env_vars": [
                {
                    "key": "MY_API_KEY",
                    "prompt": "My Backend API key",
                    "url": "https://mybackend.example.com/keys",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        lyrics: Optional[str] = None,
        instrumental: Optional[bool] = None,
        duration: Optional[int] = None,
        format: str = "mp3",
        reference_image_urls: Optional[List[str]] = None,
        seed: Optional[int] = None,
        **kwargs: Any,  # always ignore unknown kwargs for forward-compat
    ) -> Dict[str, Any]:
        # ... call your API ...

        return success_response(
            audio="https://your-cdn/output.mp3",
            model=model or "fast",
            prompt=prompt,
            duration=duration or 30,
            format=format,
            provider=self.name,
        )


def register(ctx) -> None:
    ctx.register_music_gen_provider(MyMusicGenProvider())
```

## The plugin manifest

```yaml
# plugins/music_gen/my-backend/plugin.yaml
name: my-backend
version: 1.0.0
description: "My music generation backend"
author: Your Name
kind: backend
requires_env:
  - MY_API_KEY
```

## The `music_generate` schema

The tool exposes one schema across every backend. Providers ignore parameters they don't support.

| Parameter | What it does |
|---|---|
| `action` | `generate` (default), `list`, or `status` |
| `task_id` | Task id for `action=status` |
| `prompt` | Text instruction: style, genre, mood, tempo, instruments, purpose (required for generate) |
| `lyrics` | Exact sung words, only when the user supplied them |
| `instrumental` | `true` for no vocals |
| `duration` | Seconds — provider clamps |
| `format` | `"mp3"` / `"wav"` — provider clamps |
| `reference_image_urls` | Conditioning refs (style/mood), provider-dependent |
| `model` | `provider/model` override (e.g. `google/lyria-3-pro-preview`) or a bare model id; pins the provider and disables fallback |
| `filename` | Preferred file name for the saved track |
| `seed` | Reproducibility |

The backend and model are user configuration (`music_gen.provider` / `music_gen.model`), and `model` only overrides them for one call. Your `generate()` still receives `model=` — it is the configured (or overridden) model, resolved by the tool layer.

The provider's `capabilities()` advertises which of these are honored, and `effective_capabilities(model)` merges per-model narrowings from `list_models()` entries (`supports_lyrics`, `supports_instrumental`, `supports_duration`, `formats`, `max_duration`, `min_duration`, `max_reference_images`). The agent sees the active backend's capabilities in the tool description, dynamically rebuilt when the user changes backend via `hermes tools`.

## Selection precedence

1. `music_gen.provider` / `music_gen.model` in `config.yaml`
2. Provider's `default_model()`
3. Unset provider + exactly one *available* registered provider → auto-selected

A configured-but-unregistered provider name fails closed — the tool errors instead of silently routing a paid request to another backend.

## Response shape

`success_response()` and `error_response()` produce the dict shape every backend returns. Use them — don't hand-roll the dict.

Success keys: `success`, `audio` (URL or absolute path), `model`, `prompt`, `duration`, `format`, `provider`, plus `lyrics` when the provider returns the sung words, plus `extra`.

Error keys: `success`, `audio` (None), `error`, `error_type`, `model`, `prompt`, `format`, `provider`.

## Where to save artifacts

If your backend returns base64, use `save_b64_music()` to write under `$HERMES_HOME/cache/generated/tool-music-generation/`. For raw bytes from a follow-up HTTP fetch, use `save_bytes_music()`; for an ephemeral delivery URL, `save_url_music()`. Otherwise return the upstream URL directly — the tool runner downloads it into the same directory so the task result never points at an expiring link.

## The shipped providers

| Provider | Models | Notes |
|---|---|---|
| `fal` | MiniMax Music 2.6, ACE-Step, Stable Audio 2.5 | `fal_client` queue API; `FAL_KEY` or the managed Nous fal-queue gateway |
| `minimax` | Music 2.6, Music 2.6 Free, Music Cover, Music Cover Free | Hosted `/v1/music_generation`, SSE hex audio; `MINIMAX_API_KEY` (existing paying accounts — paid music closed to new users Aug 2026) |
| `google` | Lyria 3 Clip, Lyria 3 Pro | Gemini API `generateContent`, `responseModalities` `["AUDIO", "TEXT"]`; `GOOGLE_API_KEY` (`GEMINI_API_KEY` alias); up to 10 reference images |
| `openrouter` | Lyria 3 Pro, Lyria 3 Clip | Streaming chat-completions audio; `OPENROUTER_API_KEY` via the runtime provider ladder; one reference image |
| `comfy` | operator workflow | Local ComfyUI server or Comfy Cloud; configure under `music_gen.comfy` in `config.yaml` (workflow + `prompt_node_id` required) |

## Testing

Drop a smoke test under `tests/plugins/music_gen/test_<name>_plugin.py`. The video-gen tests under `tests/plugins/video_gen/` show the pattern — register, verify catalog, exercise routing, assert clean error responses on missing auth. Tool-level behaviour (background lifecycle, duplicate guard, fallback, capability normalisation) is covered in `tests/tools/test_music_generation_tool.py` and `tests/tools/test_music_generation_tasks.py`.
