"""F9d ``gateway`` tool — self-update, restart, and config read for the gateway.

Port of the external agent's gateway-tool (config get/schema, update run/cleanup,
restart) onto Hermes' own seams: config is read with
``hermes_cli.config.load_config_readonly()`` and schema-looked-up against
``hermes_cli.config_defaults.DEFAULT_CONFIG`` (Hermes has no JSON config
schema); ``update.run`` spawns the ``hermes update`` entrypoint detached
(``hermes update --check`` for a dry run); ``restart`` asks the in-process
runner to restart, else signals a gateway ancestor over SIGUSR1; cleanup
prunes pre-update state snapshots with ``hermes_cli.backup``.

Secret-bearing config values are redacted with
``hermes_cli.config.redact_config_value`` before anything is returned —
config.yaml can hold API keys. The owner reads their own config; the tool is
owner-only throughout (``tools.gateway_owner.requester_is_owner``); the
telethon adapter hides it from every non-owner sender, and tenant sessions
never see it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from tools.gateway_owner import requester_is_owner
from tools.registry import registry

logger = logging.getLogger("tools.gateway")

_CONFIG_CAP = 12_000  # the external agent caps config.get at 12k chars
_SNAPSHOT_KEEP = 1

_PATH_SEGMENT_RE = re.compile(r"([^\.\[\]]+)?(?:\[(\d+)\])?")


def _resolve_path(config: Any, path: str) -> Any:
    """Resolve a dotted path with ``[n]`` index syntax; raises KeyError/IndexError/TypeError."""
    current = config
    for segment in (path or "").split("."):
        if not segment:
            raise KeyError(path)
        match = _PATH_SEGMENT_RE.fullmatch(segment)
        if match is None:
            raise KeyError(path)
        name, index = match.group(1), match.group(2)
        if name:
            if not isinstance(current, dict) or name not in current:
                raise KeyError(path)
            current = current[name]
        if index is not None:
            idx = int(index)
            if not isinstance(current, (list, tuple)) or idx >= len(current):
                raise IndexError(path)
            current = current[idx]
    return current


def _dump_capped(value: Any) -> tuple[str, bool]:
    text = json.dumps(value, indent=2, default=str)
    if len(text) > _CONFIG_CAP:
        return text[:_CONFIG_CAP], True
    return text, False


def _config_get(args: dict) -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly, redact_config_value

    path = str(args.get("path") or "").strip()
    try:
        # Redact the whole config first: a bare leaf resolved below carries no
        # key name, so leaf-level redaction would miss it.
        config = redact_config_value(load_config_readonly())
    except Exception as exc:
        logger.error("gateway: config read failed", exc_info=True)
        return {
            "ok": False,
            "action": "config.get",
            "error": f"Could not read config: {exc}",
        }
    try:
        value = _resolve_path(config, path) if path else config
    except (KeyError, IndexError, TypeError):
        return {
            "ok": False,
            "action": "config.get",
            "path": path,
            "error": f"No config value at '{path}'.",
        }
    text, truncated = _dump_capped(value)
    return {
        "ok": True,
        "action": "config.get",
        "path": path or "(root)",
        "value": text,
        "truncated": truncated,
    }


def _config_schema_lookup(args: dict) -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly, redact_config_value
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    path = str(args.get("path") or "").strip()
    if not path:
        return {
            "ok": False,
            "action": "config.schema.lookup",
            "error": "path is required.",
        }
    try:
        default = _resolve_path(DEFAULT_CONFIG, path)
    except (KeyError, IndexError, TypeError):
        return {
            "ok": False,
            "action": "config.schema.lookup",
            "path": path,
            "error": f"'{path}' is not a known config path.",
        }
    try:
        current = _resolve_path(load_config_readonly(), path)
        present = True
    except Exception:
        logger.debug("gateway: config read failed; reporting default only", exc_info=True)
        current, present = None, False
    return {
        "ok": True,
        "action": "config.schema.lookup",
        "path": path,
        "default": redact_config_value(default),
        "type": type(default).__name__,
        "present": present,
        "value": redact_config_value(current) if present else None,
    }


def _hermes_exe() -> Optional[str]:
    return shutil.which("hermes")


def _update_run(args: dict) -> dict[str, Any]:
    from hermes_constants import get_hermes_home

    exe = _hermes_exe()
    if exe is None:
        return {
            "ok": False,
            "action": "update.run",
            "error": "The hermes executable was not found on PATH.",
        }
    if args.get("dryRun"):
        try:
            done = subprocess.run(
                [exe, "update", "--check"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "action": "update.run",
                "error": "The update check timed out after 120s.",
            }
        except OSError as exc:
            return {
                "ok": False,
                "action": "update.run",
                "error": f"Could not run the update check: {exc}",
            }
        return {
            "ok": True,
            "action": "update.run",
            "status": "checked",
            "output": (done.stdout + done.stderr)[-_CONFIG_CAP:],
        }
    log_dir = Path(get_hermes_home()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"gateway-update-{int(time.time())}.log"
    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [exe, "update"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log_file.close()
        return {
            "ok": False,
            "action": "update.run",
            "error": f"Could not start the update: {exc}",
        }
    finally:
        log_file.close()
    return {
        "ok": True,
        "action": "update.run",
        "status": "started",
        "pid": proc.pid,
        "log": str(log_path),
        "note": "The update restarts the gateway; this turn may not survive it.",
    }


def _restart(args: dict) -> dict[str, Any]:
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except ImportError:
        runner = None
    if runner is not None:
        try:
            if runner.request_restart(via_service=True):
                return {
                    "ok": True,
                    "action": "restart",
                    "status": "requested",
                    "via": "in-process",
                }
        except Exception:
            logger.debug("gateway: in-process restart request failed", exc_info=True)
    try:
        from hermes_cli.gateway import _request_gateway_self_restart, find_gateway_pids

        for pid in find_gateway_pids():
            if _request_gateway_self_restart(pid):
                return {
                    "ok": True,
                    "action": "restart",
                    "status": "requested",
                    "via": "signal",
                    "pid": pid,
                }
    except Exception:
        logger.debug("gateway: signal restart failed", exc_info=True)
    return {
        "ok": False,
        "action": "restart",
        "error": "No running gateway answered: restart is unavailable from this process.",
    }


def _update_cleanup(args: dict) -> dict[str, Any]:
    from hermes_cli.backup import list_quick_snapshots, prune_quick_snapshots
    from hermes_constants import get_default_hermes_root, get_hermes_home

    home = Path(get_hermes_home())
    snapshots = list_quick_snapshots(limit=100, hermes_home=home)
    removable = snapshots[_SNAPSHOT_KEEP:]
    backups_dir = home / "backups"
    backups = (
        sorted(backups_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if backups_dir.is_dir()
        else []
    )
    receipts_dir = Path(get_default_hermes_root()) / "logs" / "update_receipts"
    receipts = (
        sorted(
            receipts_dir.glob("update_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if receipts_dir.is_dir()
        else []
    )
    if args.get("dryRun"):
        return {
            "ok": True,
            "action": "update.cleanup",
            "dryRun": True,
            "would_remove_snapshots": [s.get("id") for s in removable],
            "keep_snapshot": snapshots[0].get("id") if snapshots else None,
            "full_backups_kept": len(backups),
            "update_receipts_kept": len(receipts),
            "note": "Full backups and update receipts are kept; only older quick snapshots are pruned.",
        }
    removed = prune_quick_snapshots(keep=_SNAPSHOT_KEEP, hermes_home=home)
    return {
        "ok": True,
        "action": "update.cleanup",
        "dryRun": False,
        "removed_snapshots": removed,
        "keep_snapshot": snapshots[0].get("id") if snapshots else None,
        "full_backups_kept": len(backups),
        "update_receipts_kept": len(receipts),
        "note": "Full backups and update receipts are kept; only older quick snapshots are pruned.",
    }


_ACTIONS = {
    "config.get": _config_get,
    "config.schema.lookup": _config_schema_lookup,
    "update.run": _update_run,
    "restart": _restart,
    "update.cleanup": _update_cleanup,
}


def gateway(args: Optional[dict] = None, **kwargs) -> str:
    """Gateway self-management. ``action``: config.get, config.schema.lookup, update.run, restart, update.cleanup.

    Returns a JSON string: the registry rejects any other handler result type.
    """
    import json

    from tools.gateway_owner import owner_only_error
    from tools.registry import tool_error

    args = dict(args or {})
    args.update(kwargs)
    if not requester_is_owner(kwargs.get("agent")):
        return owner_only_error("gateway")
    action = str(args.get("action") or "").strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return tool_error(
            f"Unknown gateway action '{action}'.", action=action, ok=False
        )
    try:
        return json.dumps(handler(args), ensure_ascii=False)
    except (
        Exception
    ) as exc:  # fail-closed: a broken config/update seam never takes the turn down
        logger.error("gateway failed", exc_info=True)
        return tool_error(f"gateway {action} failed: {exc}", action=action, ok=False)


_GATEWAY_SCHEMA = {
    "name": "gateway",
    "description": (
        "Gateway self-management (owner only). Actions: config.get reads a config value by dotted path "
        "(empty path reads the whole config; secret values are redacted; 12k char cap); "
        "config.schema.lookup reports a config path's default, type, and whether it is set; "
        "update.run starts the hermes update detached (dryRun runs hermes update --check instead); "
        "restart asks the running gateway to restart itself; update.cleanup prunes older pre-update "
        "state snapshots (dryRun previews; full backups and update receipts are always kept)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "config.get",
                    "config.schema.lookup",
                    "update.run",
                    "restart",
                    "update.cleanup",
                ],
                "description": "Gateway self-management action",
            },
            "path": {
                "type": "string",
                "description": "Dotted config path, with [n] index syntax",
            },
            "dryRun": {
                "type": "boolean",
                "description": "Check only, without applying",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="gateway",
    toolset="gateway",
    schema=_GATEWAY_SCHEMA,
    handler=lambda args, **kw: gateway(
        args,
        db=kw.get("db"),
        agent=kw.get("agent"),
    ),
    emoji="🖥️",
)
