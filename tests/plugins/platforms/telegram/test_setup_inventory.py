from __future__ import annotations

from plugins.platforms.telegram.setup_fields import FIELDS, HOME_ITEMS, HUB_COMMANDS, SECTIONS
from plugins.platforms.telegram.setup_inventory import (
    CLI_ONLY_SECTIONS,
    PORTABLE_AREAS,
    cli_command_children,
    cli_command_inventory,
    iter_cli_only_items,
    portable_field_keys,
)


def test_inventory_covers_requested_setup_areas_and_cli_only_boundaries():
    portable = set(PORTABLE_AREAS)
    assert {
        "model_provider", "tts", "terminal", "gateway_platforms", "tools_mcp",
        "agent_personal", "display_compression", "skills_memory", "browser_dashboard",
        "diagnostics",
    } <= portable

    flat = tuple(iter_cli_only_items())
    assert flat == cli_command_inventory()
    assert len(flat) > 25
    assert all(area and label and command.startswith("hermes ") and reason for area, label, command, reason in flat)
    assert len(CLI_ONLY_SECTIONS) >= 6
    for area, items in CLI_ONLY_SECTIONS:
        assert area
        assert items
        assert cli_command_children(area) == items
        assert all(label and command and reason for label, command, reason in items)
    assert cli_command_children("not a real area") == ()

    # These are render-only command paths, not callback action identifiers.
    commands = {command for _area, _label, command, _reason in flat}
    assert "hermes auth add PROVIDER" in commands
    assert "hermes mcp login / hermes mcp reauth" in commands
    assert "hermes gateway restart" in " / ".join(commands)
    assert "hermes browser close-profile [--browser BROWSER]" in commands
    assert "hermes doctor" in " / ".join(commands)


def test_every_portable_field_is_sectioned_and_registered_in_real_config_schema():
    sectioned = {key for _label, keys in SECTIONS.values() for key in keys}
    assert portable_field_keys() == frozenset(FIELDS)
    assert set(FIELDS) == sectioned

    supported_types = {"bool", "option", "path", "int", "float", "voice", "language"}
    from hermes_cli.config import _validate_config_key

    for key, field in FIELDS.items():
        assert field["type"] in supported_types, key
        assert field["section"] in SECTIONS, key
        assert _validate_config_key(key)[0], key
        if field["type"] == "option":
            assert field.get("options"), key
            assert len({repr(value) for value, _label in field["options"]}) == len(field["options"]), key
        if field["type"] in {"int", "float"}:
            assert field["min"] <= field["max"], key


def test_confirmed_provider_and_backend_selectors_mirror_supported_builtins_only():
    from hermes_cli.setup_tts import _TTS_PROVIDER_CHOICES
    from hermes_cli.setup_terminal import _BUILTIN_TERMINAL_BACKENDS

    tts = FIELDS["tts.provider"]
    terminal = FIELDS["terminal.backend"]
    assert tts["confirm"] is True and terminal["confirm"] is True
    assert tts["confirmation_help"] and terminal["confirmation_help"]
    assert {value for value, _label in tts["options"]} == {value for value, _label in _TTS_PROVIDER_CHOICES}
    assert {value for value, _label in _BUILTIN_TERMINAL_BACKENDS} <= {
        value for value, _label in terminal["options"]
    }
    assert "does not" in tts["help"] and "OAuth" in tts["help"]
    assert "does not" in terminal["help"] and "Plugin backends remain CLI-only" in terminal["help"]


def test_dashboard_and_webui_are_one_controller_action_and_cli_inventory_has_own_entry():
    dashboard_items = [item for item in HOME_ITEMS if "Dashboard" in item[0] or "Web UI" in item[0]]
    assert len(dashboard_items) == 1
    assert dashboard_items[0][2] == "nav:dashboard"
    assert not any(command == "webui" for _label, command in HUB_COMMANDS)
    inventory_items = [item for item in HOME_ITEMS if item[2] == "inventory:0"]
    assert len(inventory_items) == 1


def test_every_audited_preference_section_is_reachable_from_setup_home():
    actions = {action for _title, _description, action in HOME_ITEMS}
    assert {f"section:{key}" for key in SECTIONS} <= actions
