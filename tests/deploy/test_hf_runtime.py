"""Offline HF runtime and reviewed source-pin contracts; never launch Hermes."""

import asyncio
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "deploy/huggingface"
SPEC = importlib.util.spec_from_file_location("hf_check_runtime", HELPERS / "check_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
SOURCE_SHA = "a0923910e33f78f3487311a202c8803934c879b7"


def test_real_gateway_loop_uses_pinned_uvloop_without_replacing_policy():
    policy = asyncio.get_event_loop_policy()
    assert runtime.check_gateway_loop() == "uvloop==0.22.1; gateway loop uvloop.Loop"
    assert asyncio.get_event_loop_policy() is policy
    with pytest.raises(RuntimeError):
        asyncio.get_event_loop()


@pytest.mark.parametrize("version", ["0.21.0", "0.23.0"])
def test_wrong_uvloop_version_is_rejected(monkeypatch, version):
    import uvloop
    monkeypatch.setattr(uvloop, "__version__", version)
    with pytest.raises(RuntimeError, match=r"uvloop==0.22.1 required"):
        runtime.check_gateway_loop()


def test_missing_uvloop_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvloop", None)
    with pytest.raises(RuntimeError, match="pinned uvloop or gateway loop selector missing"):
        runtime.check_gateway_loop()


def test_linux_native_loop_failure_cannot_silently_pass_as_asyncio(monkeypatch):
    import uvloop

    def unavailable():
        raise OSError("synthetic-native-failure")

    monkeypatch.setattr(uvloop, "new_event_loop", unavailable)
    with pytest.raises(RuntimeError, match="did not select and close uvloop.Loop"):
        runtime.check_gateway_loop()


def test_loop_smoke_failure_does_not_echo_exception_values(monkeypatch):
    from gateway import event_loop

    def broken(coro):
        coro.close()
        raise RuntimeError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(event_loop, "run_gateway_async", broken)
    with pytest.raises(RuntimeError, match="gateway event-loop smoke test failed") as error:
        runtime.check_gateway_loop()
    assert "synthetic-secret" not in str(error.value)


def test_hf_does_not_claim_support_for_an_unchecked_runtime(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="expected Linux CPython"):
        runtime.check_gateway_loop()


def source_file(path):
    return subprocess.check_output(["git", "show", f"{SOURCE_SHA}:{path}"], cwd=ROOT, text=True)


def test_image_and_docs_pin_reviewed_private_source_not_moving_main():
    dockerfile = (ROOT / "Dockerfile.hf").read_text()
    assert re.search(r"^ARG HERMES_REF=([0-9a-f]{40})$", dockerfile, re.MULTILINE).group(1) == SOURCE_SHA
    assert "ARG HERMES_REPO_URL=https://github.com/filewrap/hermes-agent.git" in dockerfile
    assert 'git fetch --depth=1 --no-tags origin "$HERMES_REF"' in dockerfile
    assert '[ "$(git rev-parse HEAD)" = "$HERMES_REF" ]' in dockerfile
    assert "--frozen --no-dev" in dockerfile
    for path in ("README_HF.md", "deploy/huggingface/README.space.md"):
        assert SOURCE_SHA in (ROOT / path).read_text()
    # The application pin predates the helper changes: no circular self-pin.
    subprocess.run(["git", "merge-base", "--is-ancestor", SOURCE_SHA, "HEAD"], cwd=ROOT, check=True)


def test_pinned_source_lock_and_runtime_retain_reviewed_uvloop_and_downloader():
    project = tomllib.loads(source_file("pyproject.toml"))
    assert any(dep.startswith("uvloop==0.22.1;") and "linux" in dep
               for dep in project["project"]["dependencies"])
    lock = tomllib.loads(source_file("uv.lock"))
    assert [pkg["version"] for pkg in lock["package"] if pkg["name"] == "uvloop"] == ["0.22.1"]
    # Exact source content, not a mutable main ref or an unrelated local checkout.
    for path in ("pyproject.toml", "uv.lock", "gateway/event_loop.py",
                 "plugins/platforms/telegram/parallel_download.py",
                 "plugins/platforms/telegram/download_connections.py",
                 "plugins/platforms/telegram/download_throughput.py"):
        assert source_file(path) == (ROOT / path).read_text(), path
    assert "MAX_DOWNLOAD_CONNECTIONS = 5" in source_file("plugins/platforms/telegram/download_connections.py")
    assert "PART_SIZE = 512 * 1024" in source_file("plugins/platforms/telegram/parallel_download.py")
