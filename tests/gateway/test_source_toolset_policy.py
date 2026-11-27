"""Source-local tool policy tests, including plugin and MCP default-on boundaries."""
from types import SimpleNamespace

from hermes_cli import tools_config
from gateway.run import GatewayRunner


class _Adapter:
    def __init__(self, override=None, denied_toolsets=None, denied_tools=None):
        self.override = override
        self.denied_toolsets = denied_toolsets
        self.denied_tools = denied_tools

    def toolsets_for_source(self, _source):
        return self.override

    def disabled_toolsets_for_source(self, _source):
        return self.denied_toolsets

    def disabled_tools_for_source(self, _source):
        return self.denied_tools


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda _source: adapter
    return runner


def test_source_override_disables_implicit_plugin_toolsets(monkeypatch):
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: {"naina_plugin"})
    adapter = _Adapter(["safe", "no_mcp"])
    config = {"platform_toolsets": {"telegram": ["web"]}}

    enabled = GatewayRunner._resolve_enabled_toolsets_for_source(
        _runner(adapter), config, SimpleNamespace(chat_type="group"), "telegram"
    )

    assert "safe" in enabled
    assert "naina_plugin" not in enabled
    assert config["platform_toolsets"]["telegram"] == ["web"]


def test_source_override_can_explicitly_opt_in_a_plugin_toolset(monkeypatch):
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: {"naina_plugin"})
    adapter = _Adapter(["safe", "no_mcp", "naina_plugin"])

    enabled = GatewayRunner._resolve_enabled_toolsets_for_source(
        _runner(adapter), {}, SimpleNamespace(chat_type="group"), "telegram"
    )

    assert "naina_plugin" in enabled


def test_source_denylists_merge_with_global_disabled_toolsets(monkeypatch):
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: set())
    adapter = _Adapter(["safe", "no_mcp"], ["context_engine"], ["plugin_web_tool"])
    config = {
        "platform_toolsets": {"telegram": ["web"]},
        "agent": {"disabled_toolsets": ["memory"]},
    }

    enabled, disabled = GatewayRunner._resolve_turn_toolsets(
        _runner(adapter), config, SimpleNamespace(chat_type="group"), "telegram"
    )

    assert "safe" in enabled
    assert {"memory", "context_engine", "tool:plugin_web_tool"} <= set(disabled)


def test_exact_tool_denial_preserves_safe_tools_in_a_plugin_extended_toolset():
    from model_tools import _apply_toolset_selection
    from toolsets import resolve_toolset

    static_safe = set(resolve_toolset("safe", include_registry=False))
    available = static_safe | {"plugin_web_tool"}

    _apply_toolset_selection(available, ["tool:plugin_web_tool"], quiet_mode=True, disable=True)

    assert available == static_safe


def test_no_mcp_sentinel_suppresses_enabled_mcp_servers(monkeypatch):
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: set())
    monkeypatch.setattr(tools_config, "enabled_mcp_server_names", lambda _config: {"mcp-private"})
    config = {
        "platform_toolsets": {"telegram": ["safe", "no_mcp"]},
        "mcp_servers": {"mcp-private": {"enabled": True}},
    }

    enabled = tools_config._get_platform_tools(config, "telegram")

    assert "mcp-private" not in enabled
