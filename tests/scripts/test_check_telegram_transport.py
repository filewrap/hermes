"""The build guard must reject executable HTTP Telegram paths, not harmless prose."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import telethon

from plugins.platforms.telegram.transport_policy import require_pinned_telethon

spec = importlib.util.spec_from_file_location(
    "check_telegram_transport", Path(__file__).resolve().parents[2] / "scripts/check_telegram_transport.py",
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


@pytest.mark.parametrize("snippet", [
    "from telegram.ext import Application", "import aiogram", "import telebot",
    "client = __import__('telegram')", "client = importlib.import_module('aiogram')",
    "url = 'https://api.telegram.org/bot'", "setting = 'TELEGRAM_HTTP_CONNECT_TIMEOUT'",
    "setting = 'TELEGRAM_WEBHOOK_URL'",
    "dependency = 'telethon>=1.45.0'",
    "dependency = 'telethon @ git+https://codeberg.org/lonami/telethon@deadbeef'",
])
def test_http_import_endpoint_or_configuration_fails_build_policy(snippet):
    assert policy.python_violations(snippet, "runtime.py")


def test_native_mtproto_bot_auth_and_comments_are_not_http_paths():
    code = """# Historical HTTP Bot API behavior must not be restored.
from telethon import TelegramClient
async def start(client, bot_token):
    await client.start(bot_token=bot_token)
"""
    assert policy.python_violations(code, "runtime.py") == []


@pytest.mark.parametrize("bad", ["python-telegram-bot==22.0", "aiogram>=3", "Telethon>=1.45.0", "Telethon==1.44.0", "telethon @ git+https://codeberg.org/lonami/telethon@deadbeef"])
def test_http_dependency_or_unpinned_client_rejected(bad):
    requirement = policy.TELETHON_REQUIREMENT
    project = {"project": {"optional-dependencies": {
        "messaging": [requirement, bad], "termux": [requirement],
    }}}
    lock = {"package": [{"name": "telethon", "version": "1.45.0", "source": {
        "git": policy.TELETHON_LOCK_SOURCE,
    }}]}
    assert policy.dependency_violations(project, lock)


def test_exact_package_and_all_lock_requirements_must_agree():
    requirement = policy.TELETHON_REQUIREMENT
    project = {"project": {"optional-dependencies": {
        "messaging": [requirement], "termux": [requirement],
    }}}
    lock = {"package": [
        {"name": "telethon", "version": "1.45.0", "source": {"git": policy.TELETHON_LOCK_SOURCE}},
        {"name": "app", "metadata": {"requires-dist": [{"name": "telethon", "git": policy.TELETHON_LOCK_REQUIREMENT}]}},
    ]}
    assert policy.dependency_violations(project, lock) == []
    lock["package"][1]["metadata"]["requires-dist"][0]["git"] = f"{policy.TELETHON_REPO}?rev=deadbeef"
    assert policy.dependency_violations(project, lock)
    lock["package"][0]["source"]["git"] = f"{policy.TELETHON_REPO}?rev=deadbeef#deadbeef"
    assert policy.dependency_violations(project, lock)
    lock["package"].append({"name": "python-telegram-bot", "version": "22.0"})
    assert any("forbidden" in error for error in policy.dependency_violations(project, lock))


def test_runtime_refuses_wrong_telethon_version_without_http_fallback(monkeypatch):
    require_pinned_telethon()
    monkeypatch.setattr(telethon, "__version__", "1.44.0")
    with pytest.raises(RuntimeError, match=r"Telethon==1\.45\.0"):
        require_pinned_telethon()
