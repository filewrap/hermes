"""Checked-in argparse inventory for Telegram; browsing never executes CLI code.

Regenerate in an isolated HOME/HERMES_HOME with:
python -m plugins.platforms.telegram.setup_cli_inventory --write
"""
from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path

INVENTORY_PATH = Path(__file__).with_name("setup_cli_tree.json")


def extract_commands(parser: argparse.ArgumentParser) -> list[dict]:
    """Traverse parsers and aliases, never parse arguments or invoke a handler."""
    commands: list[dict] = []

    def visit(current, path, parent, aliases=(), help_text=""):
        index = len(commands)
        record = {
            "id": index, "parent": parent, "path": path, "aliases": list(aliases),
            "description": str(current.description or help_text or ""),
            "options": [], "children": [],
        }
        commands.append(record)
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                descriptions = {item.dest: item.help for item in action._choices_actions}
                seen = set()
                for name, child in action.choices.items():
                    if id(child) in seen:
                        continue
                    seen.add(id(child))
                    child_aliases = [alias for alias, candidate in action.choices.items()
                                     if candidate is child and alias != name]
                    child_index = visit(child, [*path, name], index, child_aliases,
                                        descriptions.get(name, ""))
                    record["children"].append(child_index)
            elif action.help != argparse.SUPPRESS:
                choices = action.choices
                record["options"].append({
                    "flags": list(action.option_strings), "name": action.dest,
                    "metavar": str(action.metavar or action.dest),
                    "required": bool(action.required), "nargs": action.nargs,
                    "help": str(action.help or ""),
                    "choices": [str(choice) for choice in choices] if choices is not None else [],
                })
        return index

    visit(parser, [], None)
    return commands


@lru_cache(maxsize=1)
def command_inventory() -> list[dict]:
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return data["commands"]


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("--write", action="store_true", required=True)
    arg_parser.parse_args()
    from hermes_cli.main import _build_cli_parser
    root, _ = _build_cli_parser()
    tree = extract_commands(root)
    INVENTORY_PATH.write_text(json.dumps({"commands": tree}, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"Recorded {len(tree)} command branches (including root); no command handlers invoked.")
