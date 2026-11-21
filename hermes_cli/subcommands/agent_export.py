"""``hermes export-agent`` subcommand parser."""

from __future__ import annotations


def build_export_agent_parser(subparsers) -> None:
    """Attach the ``export-agent`` subcommand to ``subparsers``."""
    from hermes_cli.export_agent import cmd_export_agent

    export_parser = subparsers.add_parser(
        "export-agent",
        help="Export this Hermes setup into another agent's format",
        description="Export the active Hermes home into another agent's format. "
        "Currently only the external agent format is supported: model, providers, skills, "
        "workspace files and memory become openclaw.json + agents/<id>/, "
        "while plugins, sessions and cron jobs are archived, never activated. "
        "Secrets are never copied — keys and tokens leave as ${VAR} references.",
    )
    export_subparsers = export_parser.add_subparsers(dest="export_agent")

    external = export_subparsers.add_parser(
        "external-agent",
        help="Export this Hermes setup into an external agent's format",
        description="Write a fresh external-agent workspace (openclaw.json, "
        "agents/<id>/models.json + workspace files, .env.example) from the "
        "active Hermes home, with plugins, sessions and cron jobs archived "
        "for manual review. Always previews the plan first and asks before "
        "writing anything.",
    )
    external.add_argument(
        "--source", help="Hermes home to export (default: the active profile home)"
    )
    external.add_argument(
        "--out",
        help="Output directory (default: ./agent-export-<agent>-<timestamp>)",
    )
    external.add_argument(
        "--agent-id", help="External agent id (default: main for the default profile)"
    )
    external.add_argument(
        "--dry-run", action="store_true", help="Preview only, write nothing"
    )
    external.add_argument(
        "--overwrite", action="store_true", help="Replace existing files on conflicts"
    )
    external.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    external.add_argument(
        "--zip",
        action="store_true",
        help="Also write the export as a .zip beside the directory",
    )
    external.set_defaults(func=cmd_export_agent)
    export_parser.set_defaults(func=cmd_export_agent)
