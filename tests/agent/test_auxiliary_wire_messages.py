"""The final auxiliary destination owns message sanitation, not virtual MoA."""
import asyncio
import copy
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, OpenAI

from agent.auxiliary_client import _load_openai_cls, _relay_async_completion, _relay_sync_completion


@pytest.mark.parametrize("async_mode", [False, True])
def test_auxiliary_chat_wire_sanitizes_without_mutating_history(async_mode):
    history = [{"role": "assistant", "content": "answer", "_db_persisted": True,
                "timestamp": 12, "tool_calls": [], "reasoning": "private"}]
    original = copy.deepcopy(history)
    kwargs = {"model": "fixture-model", "messages": history}
    captured = []
    if async_mode:
        async def run():
            async with AsyncOpenAI(api_key="fixture", base_url="http://127.0.0.1:1/v1") as client:
                async def send(request):
                    captured.append(request)
                await _relay_async_completion(client, kwargs, create=send)
        asyncio.run(run())
    else:
        with OpenAI(api_key="fixture", base_url="http://127.0.0.1:1/v1") as client:
            _relay_sync_completion(client, kwargs, create=captured.append)
    assert captured[0]["messages"] == [
        {"role": "assistant", "content": "answer", "reasoning": "private"}
    ]
    assert kwargs["messages"] == original


@pytest.mark.parametrize("async_mode", [False, True])
def test_auxiliary_native_adapters_keep_replay_and_tool_fields(async_mode):
    history = [{"role": "assistant", "content": "", "_db_persisted": True,
                "codex_reasoning_items": [{"type": "reasoning", "id": "rs_fixture"}],
                "thinking_blocks": [{"type": "thinking", "thinking": "native", "signature": "sig"}],
                "tool_calls": [{"id": "call_fixture", "type": "function",
                                "function": {"name": "fixture", "arguments": "{}"}}]}]
    kwargs = {"model": "native", "messages": history}
    captured = []
    if async_mode:
        async def send(request):
            captured.append(request)
        asyncio.run(_relay_async_completion(SimpleNamespace(), kwargs, create=send))
    else:
        _relay_sync_completion(SimpleNamespace(), kwargs, create=captured.append)
    # The relay may hand the destination a shallow copy (the strict-free guard
    # copies the top-level request), but it must not strip the native adapter's
    # replay/tool fields, and the messages list itself must pass through
    # untouched — the destination owns sanitation, not the virtual transport.
    assert captured[0] == kwargs
    assert captured[0]["messages"] is history


def test_importing_auxiliary_wire_does_not_import_openai():
    """The title thread imports ``agent.auxiliary_wire`` from ``call_llm``
    on the first turn of a fresh process, while the turn thread imports
    the provider/ACP stack at the same time. If that import drags the
    whole ``openai`` package (and ``openai.types``) in at module load,
    the two threads can deadlock on the import locks — the live
    ``_ModuleLock('openai.types')`` failure. Importing the wire must
    stay side-effect free; ``openai`` loads lazily, under one lock,
    only when a real SDK client is classified."""
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    probe = (
        "import sys; "
        "from agent.auxiliary_wire import prepare_chat_messages; "
        "print('openai' in sys.modules); "
        "print('openai.types' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "False"]


def test_openai_cls_loader_is_safe_under_concurrent_first_use():
    """The title thread and the turn thread can both trigger the first
    ``openai`` import at once: the loader serializes it so every caller
    gets the one cached class, never a half-imported one."""
    results, errors = [], []

    def _call():
        try:
            results.append(_load_openai_cls())
        except Exception as exc:  # collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(results) == 8
    assert all(cls is OpenAI for cls in results)
