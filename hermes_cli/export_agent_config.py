"""Assemble the exported ``openclaw.json`` from Hermes ``config.yaml``.

This is the reverse of the agent-behaviour / TTS / skills / approvals /
browser mapping in the external agent's ``extensions/migrate-hermes/plan.ts``: the
forward provider reads external-agent keys and writes Hermes keys; this module
reads Hermes keys and writes the external-agent keys. Every mapping below cites
the forward direction it inverts, so a drift in either side is traceable.

Emitted top-level sections: ``agents.defaults``, ``models`` (mode +
providers are merged in by the facade), ``mcp.servers`` (merged in),
``plugins.slots``, ``skills.entries``, ``messages.tts``, ``approvals``,
``browser``. Channel tokens are references only — see
:func:`collect_channel_references`.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Hermes reasoning effort -> external-agent thinkingDefault (forward: "always" /
# "high" / "xhigh" -> high, "auto" / "medium" / "adaptive" -> medium,
# "off" / "low" / "none" / "minimal" -> low).
REASONING_EFFORT_TO_THINKING = {"high": "high", "medium": "auto", "low": "off"}

# Hermes approvals.mode -> external-agent approvals.exec.mode (forward: "auto" ->
# "off", "always" -> "manual", "smart" -> "smart").
APPROVAL_MODE_TO_EXTERNAL_AGENT = {"off": "auto", "manual": "always", "smart": "smart"}

# Hermes TTS provider -> external-agent messages.tts.providers key. The external agent
# renamed "edge" to "microsoft"; both are recognised, Hermes' name is kept.
TTS_PROVIDERS = (
    "elevenlabs",
    "openai",
    "edge",
    "microsoft",
    "xai",
    "minimax",
    "mistral",
    "gemini",
    "deepinfra",
)


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _opt_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def map_skill_entries(config: dict[str, Any]) -> dict[str, Any]:
    """Hermes ``skills.config`` / ``skills.disabled`` -> external-agent ``skills.entries``.

    Port of the forward provider's ``mapSkillEntries``: config values per
    skill plus ``enabled: false`` for disabled skills (Hermes always keeps
    its own operating manual active, so ``hermes-agent`` is never disabled).
    """
    skills = _record(config.get("skills"))
    entries: dict[str, dict[str, Any]] = {}
    for skill_key, value in _record(skills.get("config")).items():
        if isinstance(value, dict):
            entries[skill_key] = {"config": value}
    disabled = skills.get("disabled")
    if isinstance(disabled, str) and disabled.strip().startswith("["):
        try:
            import yaml
        except ImportError:
            # No PyYAML: the value stays a plain skill name below.
            pass
        else:
            try:
                disabled = yaml.safe_load(disabled)
            except yaml.YAMLError:
                # Not valid YAML: the value stays a plain skill name below.
                pass
    if isinstance(disabled, str):
        disabled = [disabled]
    if isinstance(disabled, list):
        for skill_key in disabled:
            if (
                isinstance(skill_key, str)
                and skill_key.strip()
                and skill_key.strip() != "hermes-agent"
            ):
                entry = entries.setdefault(skill_key.strip(), {})
                entry["enabled"] = False
    return entries


def map_agent_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """Hermes agent behaviour keys -> external-agent ``agents.defaults``."""
    defaults: dict[str, Any] = {}
    agent = _record(config.get("agent"))
    max_turns = agent.get("max_turns")
    if (
        isinstance(max_turns, (int, float))
        and not isinstance(max_turns, bool)
        and max_turns > 0
    ):
        # Forward: timeoutSeconds / 10, capped at 200. Reverse: * 10.
        defaults["timeoutSeconds"] = min(int(max_turns) * 10, 2000)
    verbose = _opt_str(agent.get("verbose"))
    if verbose:
        defaults["verboseDefault"] = verbose
    effort = _opt_str(agent.get("reasoning_effort"))
    if effort and effort in REASONING_EFFORT_TO_THINKING:
        defaults["thinkingDefault"] = REASONING_EFFORT_TO_THINKING[effort]
    compression = _record(config.get("compression"))
    if compression.get("enabled") is False:
        defaults.setdefault("compaction", {})["mode"] = "off"
    elif compression.get("enabled") is True:
        defaults.setdefault("compaction", {})["mode"] = "auto"
    summary_model = _opt_str(compression.get("summary_model"))
    if summary_model:
        defaults.setdefault("compaction", {})["model"] = summary_model
    human_delay = _record(config.get("human_delay"))
    if human_delay:
        mapped: dict[str, Any] = {}
        mode = _opt_str(human_delay.get("mode"))
        if mode:
            mapped["mode"] = mode
        for key in ("min_ms", "max_ms"):
            value = human_delay.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mapped[key.replace("_ms", "Ms")] = value
        if mapped:
            defaults["humanDelay"] = mapped
    timezone = _opt_str(config.get("timezone"))
    if timezone:
        defaults["userTimezone"] = timezone
    terminal = _record(config.get("terminal"))
    timeout = terminal.get("timeout")
    if (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and timeout > 0
    ):
        defaults.setdefault("tools", {}).setdefault("exec", {})["timeoutSec"] = timeout
    if terminal.get("backend") == "docker":
        defaults.setdefault("sandbox", {})["backend"] = "docker"
        image = _opt_str(terminal.get("docker_image"))
        if image:
            defaults["sandbox"].setdefault("docker", {})["image"] = image
    workspace = _opt_str(terminal.get("cwd"))
    if workspace:
        defaults["workspace"] = workspace
    return defaults


def map_tts(config: dict[str, Any]) -> dict[str, Any]:
    """Hermes ``tts.*`` -> external-agent ``messages.tts.providers.*``."""
    tts = _record(config.get("tts"))
    if not tts:
        return {}
    providers: dict[str, Any] = {}
    provider = _opt_str(tts.get("provider"))
    elevenlabs = _record(tts.get("elevenlabs"))
    if elevenlabs.get("voice_id") or elevenlabs.get("model_id"):
        providers["elevenlabs"] = {
            **(
                {"voiceId": elevenlabs["voice_id"]}
                if _opt_str(elevenlabs.get("voice_id"))
                else {}
            ),
            **(
                {"modelId": elevenlabs["model_id"]}
                if _opt_str(elevenlabs.get("model_id"))
                else {}
            ),
        }
    openai = _record(tts.get("openai"))
    if openai.get("model") or openai.get("voice"):
        providers["openai"] = {
            **({"model": openai["model"]} if _opt_str(openai.get("model")) else {}),
            **({"voice": openai["voice"]} if _opt_str(openai.get("voice")) else {}),
        }
    edge = _record(tts.get("edge"))
    if edge.get("voice"):
        providers["microsoft"] = {"voice": edge["voice"]}
    # Any other configured TTS provider rides along with its voice/model keys.
    for name in TTS_PROVIDERS:
        if name in providers or name == "edge":
            continue
        section = _record(tts.get(name))
        voice = _opt_str(section.get("voice")) or _opt_str(section.get("voice_id"))
        model = _opt_str(section.get("model")) or _opt_str(section.get("model_id"))
        if voice or model:
            providers[name] = {
                **({"voice": voice} if voice else {}),
                **({"model": model} if model else {}),
            }
    if not providers:
        return {}
    out: dict[str, Any] = {"providers": providers}
    if provider:
        out["provider"] = provider
    return out


def map_approvals(config: dict[str, Any]) -> dict[str, Any]:
    """Hermes ``approvals.mode`` -> external-agent ``approvals.exec.mode``."""
    approvals = _record(config.get("approvals"))
    mode = _opt_str(approvals.get("mode"))
    if mode and mode in APPROVAL_MODE_TO_EXTERNAL_AGENT:
        return {"exec": {"mode": APPROVAL_MODE_TO_EXTERNAL_AGENT[mode]}}
    return {}


def map_browser(config: dict[str, Any]) -> dict[str, Any]:
    """Hermes ``browser.*`` -> external-agent ``browser.*``."""
    browser = _record(config.get("browser"))
    out: dict[str, Any] = {}
    cdp_url = _opt_str(browser.get("cdp_url"))
    if cdp_url:
        out["cdpUrl"] = cdp_url
    if browser.get("headless") is not None:
        out["headless"] = bool(browser.get("headless"))
    return out


# Hermes .env var -> external-agent channels path. Only the token reference is
# emitted (``${VAR}``); allowlists have no Hermes equivalent to read from
# and are a manual item in the report.
CHANNEL_TOKEN_VARS = (
    ("telegram", "TELEGRAM_BOT_TOKEN", "channels.telegram.botToken"),
    ("discord", "DISCORD_BOT_TOKEN", "channels.discord.token"),
    ("slack", "SLACK_BOT_TOKEN", "channels.slack.botToken"),
    ("slack-app", "SLACK_APP_TOKEN", "channels.slack.appToken"),
    ("matrix", "MATRIX_ACCESS_TOKEN", "channels.matrix.accessToken"),
    ("mattermost", "MATTERMOST_BOT_TOKEN", "channels.mattermost.botToken"),
)


def collect_channel_references(env: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Channel token references present in the Hermes ``.env``.

    Returns ``(channels, referenced_vars)``; values are never read beyond
    presence — the emitted config carries ``${VAR}`` templates only.
    """
    channels: dict[str, Any] = {}
    referenced: list[str] = []
    for _platform, var, path in CHANNEL_TOKEN_VARS:
        if not (env.get(var) or "").strip():
            continue
        referenced.append(var)
        keys = path.split(".")
        node = channels
        for key in keys[1:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = f"${{{var}}}"
    return channels, referenced


def build_export_config(
    config: dict[str, Any],
    *,
    model_ref: Optional[str],
    fallbacks: list[str],
    providers: dict[str, Any],
    mcp_servers: dict[str, Any],
    env: dict[str, str],
    has_memory_files: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Assemble the exported ``openclaw.json`` document.

    Returns ``(document, referenced_env_vars)``. Provider entries and MCP
    servers arrive pre-mapped; this function owns the sections that map
    straight off ``config.yaml``.
    """
    document: dict[str, Any] = {}
    agent_defaults = map_agent_defaults(config)
    if model_ref:
        agent_defaults["model"] = {"primary": model_ref}
        if fallbacks:
            agent_defaults["model"]["fallbacks"] = fallbacks
    if agent_defaults:
        document["agents"] = {"defaults": agent_defaults}
    if providers:
        document["models"] = {"mode": "merge", "providers": providers}
    if mcp_servers:
        document["mcp"] = {"servers": mcp_servers}
    memory = _record(config.get("memory"))
    memory_provider = _opt_str(memory.get("provider"))
    if has_memory_files or memory_provider:
        document["plugins"] = {"slots": {"memory": "memory-core"}}
    if memory_provider == "honcho":
        honcho = _record(memory.get("honcho"))
        document.setdefault("plugins", {}).setdefault("entries", {})["honcho"] = {
            "honcho": {"enabled": True, "config": honcho},
        }
    skill_entries = map_skill_entries(config)
    if skill_entries:
        document["skills"] = {"entries": skill_entries}
    tts = map_tts(config)
    if tts:
        document.setdefault("messages", {})["tts"] = tts
    approvals = map_approvals(config)
    if approvals:
        document["approvals"] = approvals
    browser = map_browser(config)
    if browser:
        document["browser"] = browser
    channels, channel_vars = collect_channel_references(env)
    if channels:
        document["channels"] = channels
    # A Hermes command allowlist is written as exec-approvals.json beside
    # openclaw.json (forward parity); it needs no marker in the document.
    return document, list(channel_vars)


def render_exec_approvals(allowlist: Any) -> Optional[str]:
    """The ``exec-approvals.json`` content for a Hermes command allowlist."""
    if not isinstance(allowlist, list) or not allowlist:
        return None
    patterns = sorted({str(entry).strip() for entry in allowlist if str(entry).strip()})
    if not patterns:
        return None
    return json.dumps({"allow": patterns}, indent=2, ensure_ascii=False) + "\n"
