"""The Telegram CLI catalogue tracks the canonical nested argparse contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.setup_cli_inventory import command_inventory, extract_commands
from plugins.platforms.telegram.setup_ui import TelegramSetupUI


def test_checked_in_cli_tree_tracks_parser_paths_aliases_and_options():
    from hermes_cli.main import _build_cli_parser
    parser, _ = _build_cli_parser()
    actual = extract_commands(parser)
    recorded = command_inventory()
    signature = lambda node: (tuple(node["path"]), tuple(node["aliases"]),
                              tuple((tuple(option["flags"]), option["name"], tuple(option["choices"]))
                                    for option in node["options"]))
    assert {signature(node) for node in recorded} == {signature(node) for node in actual}
    for node in recorded:
        for child in node["children"]:
            assert recorded[child]["parent"] == node["id"]
            assert recorded[child]["path"][:-1] == node["path"]


@pytest.mark.asyncio
async def test_inventory_browsing_covers_all_branches_without_loading_live_config(tmp_path):
    source = SimpleNamespace(chat_type="dm", user_id="101", chat_id="101", profile="alpha", thread_id=None)
    adapter = SimpleNamespace(
        _settings_home=lambda _source: tmp_path,
        _authorized=lambda _source: True,
        _is_owner_user=lambda _user, source=None: True,
        _client=object(), _peer_for=lambda _chat: "peer",
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(id=501)),
    )
    ui = TelegramSetupUI(adapter)
    ui._load_config = lambda _state: (_ for _ in ()).throw(AssertionError("CLI browsing must not read live config"))
    state = ui._new_state(source, tmp_path, "inventory")
    state["message_id"] = "501"
    for node in command_inventory():
        state.update(inventory_node=node["id"], page=1)
        html, markup = ui._view(state)
        assert "CLI command inventory" in html
        assert html.endswith("</i></p>")
        backs = [button for row in markup.rows for button in row.buttons if button.text.startswith("← Back")]
        assert len(backs) == 1
        for row in markup.rows:
            for button in row.buttons:
                if b":inventory:" in button.type.data:
                    assert not any((button.style.bg_primary, button.style.bg_success, button.style.bg_danger))
    state.update(inventory_node=0, page=2)
    event = SimpleNamespace(message_id=501, answer=AsyncMock())
    child = command_inventory()[0]["children"][0]
    await ui.handle_callback(event, source, f"hm:setup:{state['id']}:inventory:{child}")
    current = next(iter(ui._states.values()))
    await ui.handle_callback(event, source, f"hm:setup:{current['id']}:back:previous")
    assert current["inventory_node"] == 0 and current["page"] == 2
    assert all(call.args[0].id == 501 for call in adapter._mtproto_rich_request.await_args_list)
