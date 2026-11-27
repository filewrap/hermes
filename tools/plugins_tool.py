"""F9d ``plugins`` tool — plugin lifecycle over the Hermes plugin machinery.

Port of the external agent's plugins-tool (list/search/inspect/install/update/remove/
enable/disable/reload) onto Hermes' own plugin system: manifests are read
with ``collect_directory_manifests()`` (manifest-only, no plugin code runs),
enable/disable write the ``plugins.enabled`` allow-list / ``plugins.disabled``
deny-list through ``cli.save_config_value`` (the one config writer seam),
reload/activate nudge the running gateway through
``hermes_cli.plugins_activation``, install comes from the official in-tree
catalog only, and remove uses the dashboard remove path.

Divergences from the external agent, all platform differences rather than new limits:
- ``reloadAndResume`` is not offered: Hermes reload is synchronous, there is
  no suspended state to resume.
- Install is catalog-only: ClawHub is not a Hermes source, and git-URL /
  owner/repo installs need the interactive review the CLI performs, so the
  tool refuses them and names the catalog.
- ``update`` refuses when the plugin's update needs a repin consent the tool
  cannot give interactively; the error names the CLI command.

Owner-only throughout (``tools.gateway_owner.requester_is_owner``); the
telethon adapter hides the tool from every non-owner sender, and tenant
sessions never see it.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Optional

from tools.gateway_owner import requester_is_owner
from tools.registry import registry

logger = logging.getLogger("tools.plugins")

_LIST_CAP = 20
_SEARCH_CAP = 20


def _manifests() -> list:
    from hermes_cli.plugins_discovery import collect_directory_manifests

    return collect_directory_manifests()


def _state() -> tuple[set, set]:
    from hermes_cli.plugins_discovery import _get_disabled_plugins, _get_enabled_plugins

    enabled = _get_enabled_plugins()
    return (set(enabled) if enabled is not None else set(), _get_disabled_plugins())


def _save_state(enabled: set, disabled: set) -> bool:
    from cli import save_config_value

    ok_enabled = save_config_value("plugins.enabled", sorted(enabled))
    ok_disabled = save_config_value("plugins.disabled", sorted(disabled))
    return bool(ok_enabled and ok_disabled)


def _key(manifest) -> str:
    return getattr(manifest, "key", "") or manifest.name


def _find(name: str):
    """Manifest matching a name, registry key, or directory name, else None."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for manifest in _manifests():
        path = Path(str(getattr(manifest, "path", "") or ""))
        if wanted in (manifest.name, _key(manifest), path.name):
            return manifest
    return None


def _entry(manifest, enabled: set, disabled: set) -> dict[str, Any]:
    key = _key(manifest)
    return {
        "name": manifest.name,
        "key": key,
        "version": getattr(manifest, "version", "") or "",
        "description": getattr(manifest, "description", "") or "",
        "author": getattr(manifest, "author", "") or "",
        "source": getattr(manifest, "source", "") or "",
        "kind": getattr(manifest, "kind", "") or "",
        "enabled": key in enabled and key not in disabled,
        "disabled": key in disabled,
        "provides_tools": list(getattr(manifest, "provides_tools", []) or []),
        "provides_hooks": list(getattr(manifest, "provides_hooks", []) or []),
        "path": str(getattr(manifest, "path", "") or ""),
    }


def _catalog_entry(name: str):
    from hermes_cli.plugin_catalog import get_catalog_entry

    return get_catalog_entry(name)


def _list(args: dict) -> dict[str, Any]:
    query = str(args.get("query") or "").strip().lower()
    enabled, disabled = _state()
    entries = [_entry(m, enabled, disabled) for m in _manifests()]
    if query:
        entries = [
            e
            for e in entries
            if query in e["name"].lower()
            or query in e["description"].lower()
            or query in e["key"].lower()
        ]
    total = len(entries)
    return {
        "ok": True,
        "action": "list",
        "plugins": entries[:_LIST_CAP],
        "total": total,
    }


def _search(args: dict) -> dict[str, Any]:
    from hermes_cli.plugin_catalog import search_catalog

    query = str(args.get("query") or "")
    installed = {m.name for m in _manifests()} | {_key(m) for m in _manifests()}
    found = []
    for entry in search_catalog(query)[:_SEARCH_CAP]:
        found.append(
            {
                "name": entry.name,
                "description": entry.description or "",
                "tier": getattr(entry, "tier", "") or "",
                "repo": getattr(entry, "repo", "") or "",
                "installed": entry.name in installed,
                "provides_tools": list(
                    getattr(entry.capabilities, "provides_tools", []) or []
                ),
            }
        )
    return {
        "ok": True,
        "action": "search",
        "query": query,
        "results": found,
        "total": len(found),
    }


def _inspect(args: dict) -> dict[str, Any]:
    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "inspect", "error": "pluginId is required."}
    enabled, disabled = _state()
    manifest = _find(name)
    entry = _catalog_entry(name)
    if manifest is None and entry is None:
        return {
            "ok": False,
            "action": "inspect",
            "error": f"Plugin '{name}' is not installed and is not in the Hermes plugin catalog.",
        }
    result: dict[str, Any] = {"ok": True, "action": "inspect", "name": name}
    if manifest is not None:
        result["installed"] = _entry(manifest, enabled, disabled)
        result["requires_plugins"] = list(
            getattr(manifest, "requires_plugins", []) or []
        )
        result["python_dependencies"] = list(
            getattr(manifest, "python_dependencies", []) or []
        )
        result["requires_hermes"] = getattr(manifest, "requires_hermes", "") or ""
    else:
        result["installed"] = None
    if entry is not None:
        result["catalog"] = {
            "name": entry.name,
            "description": entry.description or "",
            "tier": getattr(entry, "tier", "") or "",
            "repo": getattr(entry, "repo", "") or "",
            "sha": getattr(entry, "sha", "") or "",
        }
    else:
        result["catalog"] = None
    return result


def _install(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_cmd_catalog import install_catalog_entry

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "install", "error": "pluginId is required."}
    entry = _catalog_entry(name)
    if entry is None:
        return {
            "ok": False,
            "action": "install",
            "error": f"'{name}' is not in the Hermes plugin catalog. Browse entries with the "
            "plugins search action; git-URL and owner/repo installs need the interactive "
            "review of `hermes plugins install`.",
        }
    try:
        _target, _manifest, installed_name = install_catalog_entry(
            entry, force=bool(args.get("force"))
        )
    except (
        Exception
    ) as exc:  # install failure (clone, scan, incompatible pin) — report, don't raise
        logger.error("plugins: install of %r failed", name, exc_info=True)
        return {
            "ok": False,
            "action": "install",
            "error": f"Could not install '{name}': {exc}",
        }
    from hermes_cli.plugins_activation import activate_plugin_now

    activation = activate_plugin_now(installed_name)
    return {
        "ok": True,
        "action": "install",
        "plugin": installed_name,
        "gateway_reloaded": bool(activation.get("gateway_reloaded")),
        "activation": activation.get("activation"),
        "restart_required": bool(activation.get("restart_required")),
    }


def _update(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_cmd_catalog import RepinConsentRequired
    from hermes_cli.plugins_cmd_update import update_one
    from hermes_cli.plugins_cmd import PluginOperationError, PluginScanBlocked

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "update", "error": "pluginId is required."}
    manifest = _find(name)
    if manifest is None:
        return {
            "ok": False,
            "action": "update",
            "error": f"Plugin '{name}' is not installed.",
        }
    target = Path(str(getattr(manifest, "path", "") or ""))
    if not target.is_dir():
        return {
            "ok": False,
            "action": "update",
            "error": f"Plugin '{name}' has no installed directory to update.",
        }
    from rich.console import Console

    console = Console(file=io.StringIO(), width=100)
    try:
        updated = update_one(
            name, target, console, interactive=False, force=bool(args.get("force"))
        )
    except RepinConsentRequired as exc:
        return {
            "ok": False,
            "action": "update",
            "error": f"Update of '{name}' needs a repin consent this tool cannot give: {exc}. "
            f"Run `hermes plugins update {name}` interactively.",
        }
    except (PluginOperationError, PluginScanBlocked) as exc:
        return {
            "ok": False,
            "action": "update",
            "error": f"Could not update '{name}': {exc}",
        }
    except Exception as exc:  # git failure, lock contention — report, don't raise
        logger.error("plugins: update of %r failed", name, exc_info=True)
        return {
            "ok": False,
            "action": "update",
            "error": f"Could not update '{name}': {exc}",
        }
    return {"ok": True, "action": "update", "plugin": name, "updated": bool(updated)}


def _remove(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_cmd_remove import dashboard_remove_user_plugin

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "remove", "error": "pluginId is required."}
    result = dashboard_remove_user_plugin(name)
    if not result.get("ok"):
        return {
            "ok": False,
            "action": "remove",
            "error": str(result.get("error") or f"Could not remove plugin '{name}'."),
        }
    return {
        "ok": True,
        "action": "remove",
        "plugin": name,
        "cleared_memory_provider": bool(result.get("cleared_memory_provider")),
    }


def _enable(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_activation import activate_plugin_now

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "enable", "error": "pluginId is required."}
    manifest = _find(name)
    if manifest is None:
        return {
            "ok": False,
            "action": "enable",
            "error": f"Plugin '{name}' is not installed.",
        }
    key = _key(manifest)
    enabled, disabled = _state()
    enabled.add(key)
    disabled.discard(key)
    if not _save_state(enabled, disabled):
        return {
            "ok": False,
            "action": "enable",
            "error": f"Could not write plugins.enabled for '{name}'.",
        }
    activation = activate_plugin_now(key)
    return {
        "ok": True,
        "action": "enable",
        "plugin": key,
        "gateway_reloaded": bool(activation.get("gateway_reloaded")),
        "activation": activation.get("activation"),
        "restart_required": bool(activation.get("restart_required")),
    }


def _disable(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_activation import (
        gateway_reload_hint,
        reload_running_gateway,
    )

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "disable", "error": "pluginId is required."}
    manifest = _find(name)
    if manifest is None:
        return {
            "ok": False,
            "action": "disable",
            "error": f"Plugin '{name}' is not installed.",
        }
    key = _key(manifest)
    enabled, disabled = _state()
    enabled.discard(key)
    disabled.add(key)
    if not _save_state(enabled, disabled):
        return {
            "ok": False,
            "action": "disable",
            "error": f"Could not write plugins.disabled for '{name}'.",
        }
    answer = reload_running_gateway()
    return {
        "ok": True,
        "action": "disable",
        "plugin": key,
        "gateway_reloaded": bool(answer and answer.get("reloaded")),
        "hint": gateway_reload_hint(answer, key),
    }


def _reload(args: dict) -> dict[str, Any]:
    from hermes_cli.plugins_activation import activate_plugin_now

    name = str(args.get("pluginId") or args.get("name") or "").strip()
    if not name:
        return {"ok": False, "action": "reload", "error": "pluginId is required."}
    manifest = _find(name)
    if manifest is None:
        return {
            "ok": False,
            "action": "reload",
            "error": f"Plugin '{name}' is not installed.",
        }
    key = _key(manifest)
    activation = activate_plugin_now(key)
    return {
        "ok": True,
        "action": "reload",
        "plugin": key,
        "gateway_reloaded": bool(activation.get("gateway_reloaded")),
        "activation": activation.get("activation"),
        "restart_required": bool(activation.get("restart_required")),
    }


_ACTIONS = {
    "list": _list,
    "search": _search,
    "inspect": _inspect,
    "install": _install,
    "update": _update,
    "remove": _remove,
    "enable": _enable,
    "disable": _disable,
    "reload": _reload,
}


def plugins(args: Optional[dict] = None, **kwargs) -> str:
    """Plugin lifecycle. ``action``: list/search/inspect/install/update/remove/enable/disable/reload.

    Returns a JSON string: the registry rejects any other handler result type.
    """
    import json

    from tools.gateway_owner import owner_only_error
    from tools.registry import tool_error

    args = dict(args or {})
    args.update(kwargs)
    if not requester_is_owner(kwargs.get("agent")):
        return owner_only_error("plugins")
    action = str(args.get("action") or "list").strip().lower()
    handler = _ACTIONS.get(action)
    if handler is None:
        return tool_error(
            f"Unknown plugins action '{action}'. "
            "Hermes has no reloadAndResume: reload is synchronous.",
            action=action,
            ok=False,
        )
    try:
        return json.dumps(handler(args), ensure_ascii=False)
    except (
        Exception
    ) as exc:  # fail-closed: a broken plugin inventory never takes the turn down
        logger.error("plugins failed", exc_info=True)
        return tool_error(f"plugins {action} failed: {exc}", action=action, ok=False)


_PLUGINS_SCHEMA = {
    "name": "plugins",
    "description": (
        "Plugin lifecycle over the Hermes plugin system (owner only). Actions: list installed plugins; "
        "search the official catalog; inspect an installed or catalog plugin; install from the catalog; "
        "update an installed plugin; remove an installed plugin; enable or disable a plugin (writes the "
        "plugins.enabled allow-list / plugins.disabled deny-list); reload a plugin in the running gateway. "
        "There is no reloadAndResume: reload is synchronous. Install is catalog-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list",
                    "search",
                    "inspect",
                    "install",
                    "update",
                    "remove",
                    "enable",
                    "disable",
                    "reload",
                ],
                "description": "Plugin lifecycle action",
            },
            "pluginId": {
                "type": "string",
                "description": "Plugin name or registry key",
            },
            "name": {"type": "string", "description": "Alias for pluginId"},
            "query": {
                "type": "string",
                "description": "Substring filter for list/search",
            },
            "force": {"type": "boolean", "description": "Force install/update"},
        },
    },
}


registry.register(
    name="plugins",
    toolset="plugins",
    schema=_PLUGINS_SCHEMA,
    handler=lambda args, **kw: plugins(
        args,
        db=kw.get("db"),
        agent=kw.get("agent"),
    ),
    emoji="🧩",
)
