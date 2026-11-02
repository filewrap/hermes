#!/usr/bin/env python3
"""Fail the test/build gate if Telegram stops being pinned Python Telethon MTProto.

This is a static policy check, not a network probe. It parses Python imports/literals
and dependency metadata; comments and documentation do not count as transport paths.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import sys
import tomllib

PIN = "1.45.0"
TELETHON_COMMIT = "9a02e4746f330b9886dd4e63472b9ed23b01a9d1"
TELETHON_REPO = "https://codeberg.org/lonami/telethon"
TELETHON_REQUIREMENT = f"telethon @ git+{TELETHON_REPO}@{TELETHON_COMMIT}"
TELETHON_LOCK_SOURCE = f"{TELETHON_REPO}?rev={TELETHON_COMMIT}#{TELETHON_COMMIT}"
TELETHON_LOCK_REQUIREMENT = f"{TELETHON_REPO}?rev={TELETHON_COMMIT}"
HTTP_PACKAGES = {"python-telegram-bot", "telegram", "aiogram", "pytelegrambotapi", "telepot", "aiotg", "telegram-bot-api"}
HTTP_IMPORTS = {"telegram", "aiogram", "telebot", "telepot", "aiotg"}
EXCLUDED = {"tests", "evals", "website", "skills", "optional-skills", "docs", "node_modules", ".venv", "venv"}
HTTP_SETTING = re.compile(r"TELEGRAM_(?:HTTP_|POLLING_|WEBHOOK_|FALLBACK_IP|BASE_URL|BOT_API_URL)")


def python_violations(text: str, filename: str) -> list[str]:
    errors = []
    tree = ast.parse(text, filename=filename)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            dynamic = ((isinstance(func, ast.Name) and func.id == "__import__")
                       or (isinstance(func, ast.Attribute) and func.attr == "import_module"))
            if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        for name in names:
            if name.split(".", 1)[0] in HTTP_IMPORTS:
                errors.append(f"{filename}:{node.lineno}: forbidden HTTP Telegram import {name}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if "api.telegram.org" in value.lower() or HTTP_SETTING.search(value):
                errors.append(f"{filename}:{node.lineno}: forbidden HTTP Telegram endpoint/configuration")
            if re.match(r"^telethon(?:\[[^]]+\])?(?:[<>=!~]|\s*@)", value, re.IGNORECASE) and value.lower() != TELETHON_REQUIREMENT.lower():
                errors.append(f"{filename}:{node.lineno}: inconsistent Telethon runtime dependency pin")
    return errors


def dependency_violations(project: dict, lock: dict) -> list[str]:
    errors = []
    deps = list(project.get("project", {}).get("dependencies", []))
    extras = project.get("project", {}).get("optional-dependencies", {})
    for values in extras.values():
        deps.extend(values)
    for dependency in deps:
        name = re.split(r"[\[<>=!~; @]", dependency, maxsplit=1)[0].lower().replace("_", "-")
        if name in HTTP_PACKAGES:
            errors.append(f"pyproject.toml: forbidden HTTP Telegram dependency {name}")
        if name == "telethon" and dependency.lower() != TELETHON_REQUIREMENT.lower():
            errors.append(f"pyproject.toml: Telethon must use the exact tested Git commit {TELETHON_COMMIT}")
    for extra in ("messaging", "termux"):
        if not any(value.lower() == TELETHON_REQUIREMENT.lower() for value in extras.get(extra, [])):
            errors.append(f"pyproject.toml: {extra} must include Telethon commit {TELETHON_COMMIT}")
    found = False
    for package in lock.get("package", []):
        name = package["name"].lower().replace("_", "-")
        if name in HTTP_PACKAGES:
            errors.append(f"uv.lock: forbidden HTTP Telegram package {name}")
        if name == "telethon":
            found = True
            if package.get("version") != PIN:
                errors.append(f"uv.lock: Telethon must resolve to {PIN}")
            source = package.get("source", {})
            if source.get("git") != TELETHON_LOCK_SOURCE:
                errors.append(f"uv.lock: Telethon must resolve from Git commit {TELETHON_COMMIT}")
        for requirement in package.get("metadata", {}).get("requires-dist", []):
            if requirement.get("name", "").lower() == "telethon":
                if requirement.get("git") != TELETHON_LOCK_REQUIREMENT:
                    errors.append(f"uv.lock: inconsistent Telethon requirement in {name}")
    if not found:
        errors.append("uv.lock: pinned Telethon package missing")
    return errors


def audit(root: Path) -> list[str]:
    errors = dependency_violations(
        tomllib.loads((root / "pyproject.toml").read_text()),
        tomllib.loads((root / "uv.lock").read_text()),
    )
    # Include new, uncommitted files as well as tracked files, but not ignored caches.
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    for name in sorted(set(paths)):
        path = Path(name)
        if not name or path.suffix != ".py" or any(part in EXCLUDED for part in path.parts):
            continue
        if name == "scripts/check_telegram_transport.py" or not (root / path).is_file():
            continue
        try:
            errors.extend(python_violations((root / path).read_text(encoding="utf-8"), name))
        except SyntaxError as exc:
            errors.append(f"{name}: cannot audit invalid Python: {exc}")
    policy = root / "plugins/platforms/telegram/transport_policy.py"
    values = [node.value.value for node in ast.walk(ast.parse(policy.read_text()))
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and any(isinstance(target, ast.Name) and target.id == "PINNED_TELETHON_VERSION" for target in node.targets)]
    if values != [PIN]:
        errors.append("Telegram runtime version check disagrees with dependency pins")
    return errors


def main() -> int:
    errors = audit(Path(__file__).resolve().parents[1])
    if errors:
        print("Telegram transport policy failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"Telegram transport policy passed: Telethon=={PIN} at {TELETHON_COMMIT}, Python MTProto only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
