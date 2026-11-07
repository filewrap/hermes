"""Tenant-scoped Telegram slash command dispatch.

Non-owner Telegram commands run in a short-lived worker whose HERMES_HOME and
HOME point at a per-user directory. The worker has no inherited credentials and
never shares the live gateway's Python process, runner singleton, adapters, or
session database. Owner/process/security controls are denied before the worker is
started; unknown/plugin commands fail closed unless they have an explicit tenant
registration in a future change.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command

from gateway.tenant_runtime import (
    TenantRuntimeDenied,
    _TransportIdentity,
    _active,
    _authorization_stamp,
    _check_admission,
    expand_personal_skill_command,
    tenant_tool_environment,
)

logger = logging.getLogger(__name__)

# These commands are explicitly tied to the configured profile owner, shared
# gateway lifecycle, global security policy, owner memory/skills, or a control
# surface already guarded by Telegram's native owner checks. Ordinary profile
# settings and session commands run against a per-user HERMES_HOME instead.
TENANT_OWNER_ONLY_COMMANDS = frozenset({
    "allow", "allowlist", "approve", "approvals", "debug",
    "deny", "disallow", "egress", "guestbot", "login",
    "msettings", "pause", "platform", "reload-mcp", "reload-skills",
    "restart", "sethome", "stop", "sync", "thread", "topic", "topup",
    "update", "yolo", "auto", "skills", "memory", "memories",
})

# Exhaustive snapshot of gateway-registered core commands. Kept explicit so a
# newly added command fails closed until its tenant scope is reviewed.
_TENANT_COMMANDS = frozenset({
    "start", "new", "save", "retry", "undo", "title", "branch", "compress",
    "rollback", "bg", "btw", "agents", "queue", "steer", "goal", "heartbeat",
    "refine", "review", "loop", "plan", "moa", "subgoal", "status", "context",
    "whoami", "profile", "resume", "sessions", "search", "deepresearch", "settings",
    "trigger", "model", "models", "providers", "codex-runtime", "personality", "diff", "verbose",
    "footer", "reasoning", "fast", "voice", "busy", "memory", "memories", "bundles", "learn",
    "init", "suggestions", "blueprint", "curator", "kanban", "help", "usage",
    "insights", "version",
}) | TENANT_OWNER_ONLY_COMMANDS


def _registered_gateway_commands() -> frozenset[str]:
    return frozenset(
        item.name for item in COMMAND_REGISTRY
        if not item.cli_only or bool(item.gateway_config_gate)
    )


def tenant_command_policy(name: str, arguments: str = "") -> str:
    """Return ``tenant``, ``owner``, or ``unavailable`` for a canonical command.

    Command identity comes from the shared registry; unknown commands are not
    guessed from their spelling. Subcommand-level privileged writes are checked
    after canonicalization here, as well as inside the isolated worker.
    """
    definition = resolve_command(str(name or ""))
    if definition is None or definition.name not in _TENANT_COMMANDS:
        return "unavailable"
    canonical = definition.name
    if canonical in TENANT_OWNER_ONLY_COMMANDS:
        return "owner"
    if canonical == "goal" and re.match(r"^\s*gate\s+add(?:\s|$)", str(arguments or ""), re.IGNORECASE):
        return "owner"
    return "tenant"


def tenant_command_home(root: str | Path, user_id: str) -> Path:
    """Resolve a private per-user profile home beneath the active multiuser root."""
    uid = str(user_id or "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
        raise TenantRuntimeDenied("Tenant access unavailable")
    base = Path(root).expanduser().resolve(strict=True)
    leaf = hashlib.sha256(uid.encode("ascii")).hexdigest()[:32]
    requested = base / ".tenant-cache" / "command-runtimes" / ("telegram-" + leaf)
    if any(part.is_symlink() for part in (requested, *requested.parents) if part != base.parent):
        raise TenantRuntimeDenied("Tenant profile path is unavailable")
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        requested.chmod(0o700)
    except OSError:
        pass
    home = requested.resolve(strict=True)
    if not home.is_relative_to(base) or home.is_symlink():
        raise TenantRuntimeDenied("Tenant profile path is unavailable")
    return home


def _ensure_tenant_owner_identity(home: Path, user_id: str) -> None:
    """Mark the private worker profile as owned by this user, never the host owner.

    Several regular preference handlers use the profile owner marker as their
    write boundary. This per-user marker lets them update only this isolated
    command home. Worker startup skips dotenv loading, and the worker environment
    is credential-scrubbed, so this marker cannot import host secrets.
    """
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(user_id)):
        raise TenantRuntimeDenied("Tenant profile is unavailable")
    env_path = home / ".env"
    if env_path.is_symlink() or not env_path.resolve(strict=False).is_relative_to(home.resolve(strict=True)):
        raise TenantRuntimeDenied("Tenant profile is unavailable")
    marker = f"OWNER_TELEGRAM_ID={user_id}"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        owners = [line.strip() for line in content.splitlines()
                  if line.strip().startswith("OWNER_TELEGRAM_ID=")]
        if owners and owners != [marker]:
            raise TenantRuntimeDenied("Tenant profile identity does not match")
        if not owners:
            with env_path.open("a", encoding="utf-8") as handle:
                if content and not content.endswith("\n"):
                    handle.write("\n")
                handle.write(marker + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    else:
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(marker + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def _tenant_prompt(command: str, description: str, arguments: str) -> str:
    request = str(arguments or "").strip() or "(no arguments supplied)"
    return (
        f"[Tenant command request: /{command} — {description}]\n"
        f"User's command arguments: {request}\n\n"
        "Help within this user's isolated tenant session. Do not access owner data, "
        "other users, or the host; use only tools explicitly enabled by the tenant safety "
        "policy. Do not claim external actions, searches, edits, schedules, or setting "
        "changes unless an available tool or command handler performed and confirmed them."
    )


def _result_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    result = getattr(value, "result", None)
    if isinstance(result, str):
        return result
    return str(value)


async def _run_isolated_worker(home: Path, source: Any, text: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    env = tenant_tool_environment(dict(os.environ))
    python_path = [str(repo_root)]
    for entry in sys.path:
        if ("site-packages" in entry or "dist-packages" in entry) and os.path.isdir(entry):
            if entry not in python_path:
                python_path.append(entry)
    try:
        from pm.environments import committed_venv, site_packages
        venv = committed_venv(repo_root)
        if venv is not None:
            sp = str(site_packages(venv))
            if sp not in python_path and os.path.isdir(sp):
                python_path.append(sp)
    except Exception:
        pass
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "PYTHONPATH": os.pathsep.join(python_path),
        "HERMES_TENANT_COMMAND_WORKER": "1",
    })
    request = {
        "text": text,
        "source": {
            "chat_id": str(getattr(source, "chat_id", "")),
            "chat_type": str(getattr(source, "chat_type", "dm") or "dm"),
            "user_id": str(getattr(source, "user_id", "")),
            "user_name": str(getattr(source, "user_name", "") or ""),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "chat_name": str(getattr(source, "chat_name", "") or ""),
        },
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gateway.tenant_command_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(home), env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(request, ensure_ascii=False).encode("utf-8")),
            timeout=45,
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TenantRuntimeDenied("Tenant command timed out") from None
    if proc.returncode != 0:
        logger.warning("Isolated Telegram command worker failed (exit=%s): %s",
                       proc.returncode, stderr.decode("utf-8", "replace")[-2000:])
        raise TenantRuntimeDenied("Tenant command could not be completed")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except Exception:
        logger.warning("Isolated Telegram command worker returned invalid output: %s",
                       stdout.decode("utf-8", "replace")[-1000:])
        raise TenantRuntimeDenied("Tenant command could not be completed") from None
    if not isinstance(payload, dict):
        raise TenantRuntimeDenied("Tenant command could not be completed")
    return payload


async def handle_tenant_command(
    adapter: Any, event: Any, source: Any, root: str | Path, *, store: Any,
) -> Any:
    """Run a recognized slash command for an admitted tenant identity.

    Authorization is re-read before and after the isolated worker. Owner-only
    commands and unknown plugin commands never reach the worker or owner runner.
    Prompt-style commands fall through to the existing tenant runtime and its existing
    tenant-scoped tool policy.
    """
    text = str(getattr(event, "text", "") or "")
    command = getattr(event, "get_command", lambda: None)()
    if not command:
        raise TenantRuntimeDenied("Tenant command unavailable")
    arguments = getattr(event, "get_command_args", lambda: "")()
    identity = _TransportIdentity.capture(source)
    if identity.user_id != str(getattr(source, "user_id", "")):
        raise TenantRuntimeDenied("Tenant access unavailable")
    principal = _active(store, identity.user_id, identity)
    stamp = _authorization_stamp(store, identity)
    if identity.chat_type in {"group", "forum"} and callable(getattr(store, "get_group_settings", None)):
        group_settings = store.get_group_settings(principal, identity.chat_id) or {}
        if isinstance(group_settings, dict) and group_settings.get("enabled") is False:
            _check_admission(store, source, identity, stamp)
            return {"final_response": "Tenant responses are disabled in this group.",
                    "messages": [], "usage": {}}
    definition = resolve_command(command)
    if definition is None:
        expanded = expand_personal_skill_command(text, Path(root), str(getattr(source, "user_id", "")))
        if expanded == text:
            return {"final_response": f"Command /{command} is not available in your isolated profile. Use /help or your private /skills menu.",
                    "messages": [], "usage": {}}
        from gateway.tenant_runtime import handle_tenant_message
        return await handle_tenant_message(adapter, event, source, root, store=store)
    policy = tenant_command_policy(definition.name, arguments)
    if policy == "owner":
        # Silent denial: an allowed non-owner hitting an owner-only command gets no
        # message at all (the caller sends final_response only when non-empty).
        return {"final_response": "", "messages": [], "usage": {}}
    if policy != "tenant":
        return {"final_response": f"Command /{command} is not available in your isolated profile.",
                "messages": [], "usage": {}}

    if definition.name in {"bg", "btw", "heartbeat", "loop", "goal", "subgoal", "queue", "steer"}:
        from gateway.tenant_tasks import ensure_tenant_task_scheduler
        scheduler = ensure_tenant_task_scheduler(adapter, root, store)
        result = await scheduler.handle_command(
            event, source, identity=identity, stamp=stamp,
            command=definition.name, arguments=arguments,
        )
        _check_admission(store, source, identity, stamp)
        return result

    try:
        home = tenant_command_home(root, identity.user_id)
        _ensure_tenant_owner_identity(home, identity.user_id)
    except Exception:
        raise TenantRuntimeDenied("Tenant profile is unavailable") from None
    _check_admission(store, source, identity, stamp)
    payload = await _run_isolated_worker(home, source, text)
    _check_admission(store, source, identity, stamp)

    if payload.get("handled"):
        response = _result_text(payload.get("result"))
        if definition.name in {"new"}:
            # The isolated command SessionStore is separate from the tenant
            # conversation history; rotate both user-owned stores together.
            try:
                from gateway.tenant_runtime import room_key
                profile_key = str(Path(root).expanduser().resolve(strict=False))
                key = room_key(identity.user_id, identity.chat_id, identity.thread_id,
                               profile=profile_key)
                store.put(principal, "profile", "history:" + key, [])
            except Exception:
                logger.exception("Could not rotate the tenant conversation history")
                response = "The tenant session reset could not be completed; please try again."
        return {"final_response": response, "messages": [], "usage": {}}

    # Rewriting commands (for example /plan or /learn) and registered commands
    # without a direct gateway handler are expressed as a normal tenant request.
    # Preserve the original slash text in history while giving the restricted
    # model the command intent separately.
    prompt_text = str(payload.get("text") or text)
    if prompt_text.lstrip().startswith("/"):
        prompt_text = _tenant_prompt(definition.name, definition.description, arguments)
    if not prompt_text:
        prompt_text = _tenant_prompt(definition.name, definition.description, arguments)
    event_copy = replace(event)
    event_copy.metadata = dict(getattr(event, "metadata", {}) or {})
    event_copy.metadata["tenant_command_prompt"] = prompt_text
    from gateway.tenant_runtime import handle_tenant_message
    return await handle_tenant_message(adapter, event_copy, source, root, store=store)


def assert_tenant_policy_covers_registry() -> None:
    """Raise if a newly registered core command lacks an explicit tenant classification."""
    registered = _registered_gateway_commands()
    missing = registered - _TENANT_COMMANDS
    stale = _TENANT_COMMANDS - registered
    if missing or stale:
        raise AssertionError(f"tenant command policy drift: missing={sorted(missing)}, stale={sorted(stale)}")
