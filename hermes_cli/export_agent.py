"""Reverse migration: export a Hermes home into external-agent format.

``hermes export-agent external-agent`` writes a fresh external-agent workspace from
the active Hermes home (or ``--source``):

- ``openclaw.json`` — default model, providers, MCP servers, agent
  behaviour, TTS, approvals, browser, channel token *references*.
- ``agents/<agent-id>/models.json`` — the per-agent provider catalog,
  merged with ``openclaw.json`` providers.
- ``agents/<agent-id>/`` — ``SOUL.md``, ``AGENTS.md``, ``IDENTITY.md``,
  ``MEMORY.md``, ``USER.md``, ``TOOLS.md`` (when present) and ``skills/``.
- ``.env.example`` — every referenced env var, empty, never valued.
- ``exec-approvals.json`` — the command allowlist, when configured.
- ``archive/`` — Hermes plugins, sessions (index + state.db snapshot)
  and cron jobs, for manual review only, never activated.
- ``export-report.json`` + ``README.md`` — what moved, what needs a
  human, and what to do next.

The shape reference is the external agent's ``extensions/migrate-hermes`` (the
forward import); this module is its reverse. Secrets are never copied:
provider keys, channel tokens and MCP credentials leave as ``${VAR}``
references, and the post-write scan fails the export if any harvested
secret value lands in a live file.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shutil
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values

from hermes_cli.export_agent_archive import (
    apply_archive_item,
    list_sessions_index,
    load_cron_jobs,
    plan_archive,
    write_cron_readme,
    write_plugins_index,
    write_sessions_index,
)
from hermes_cli.export_agent_config import (
    build_export_config,
    collect_channel_references,
    render_exec_approvals,
)
from hermes_cli.export_agent_files import (
    copy_tree,
    plan_skills,
    plan_workspace_files,
    sanitize_name,
)
from hermes_cli.export_agent_mcp import collect_mcp_servers
from hermes_cli.export_agent_model import (
    add_selected_model_to_provider,
    collect_hermes_providers,
    provider_config_entry,
    resolve_hermes_model_ref,
    resolve_model_fallbacks,
    uses_retired_qwen_provider,
)
from hermes_cli.export_agent_report import (
    ExportItem,
    ExportPlan,
    ExportReport,
    manual_item,
)
from hermes_cli.export_agent_source import (
    discover_hermes_source,
    profile_name_for,
)
from hermes_cli.export_agent_validate import (
    harvest_secret_values,
    validate_export_tree,
)
from hermes_yaml import safe_load


@dataclass
class ExportOptions:
    """Options for one reverse-migration run."""

    source: Optional[str] = None
    out: Optional[str] = None
    agent_id: Optional[str] = None
    overwrite: bool = False
    dry_run: bool = False
    yes: bool = False
    zip: bool = False


def read_source_config(source_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse the source home's ``config.yaml`` and ``.env`` (reads only)."""
    config: dict[str, Any] = {}
    config_path = source_root / "config.yaml"
    if config_path.is_file():
        try:
            parsed = safe_load(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                f"Failed to parse Hermes config at {config_path}: {exc}"
            ) from exc
        if isinstance(parsed, dict):
            config = parsed
    env: dict[str, str] = {}
    env_path = source_root / ".env"
    if env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if value is not None:
                env[key] = value
    return config, env


def default_agent_id(source_root: Path) -> str:
    """External-agent id for a Hermes home: ``main`` for the default profile."""
    profile = profile_name_for(source_root)
    return "main" if profile == "default" else (sanitize_name(profile) or "main")


def default_output_dir(agent_id: str) -> Path:
    """Timestamped output directory under the current working directory."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return (Path.cwd() / f"agent-export-{agent_id}-{stamp}").resolve()


def _referenced_env_vars(document: dict[str, Any]) -> list[str]:
    """Every ``${VAR}`` referenced anywhere in the emitted config."""
    text = json.dumps(document, ensure_ascii=False)
    return sorted(set(re.findall(r"\$\{([^}]+)\}", text)))


def _env_example(vars_: list[str]) -> str:
    lines = [
        "# Environment variables referenced by this export.",
        "# Copy to .env in your external-agent home and fill in the values.",
        "# No secret value was exported; every variable below was empty here.",
        "",
    ]
    for name in vars_:
        lines.append(f"{name}=")
    return "\n".join(lines) + "\n"


def _export_readme(
    plan: ExportPlan, *, session_count: int, cron_count: int, plugin_count: int
) -> str:
    lines = [
        "# Hermes -> External Agent export",
        "",
        f"Exported from Hermes home `{plan.source_root}`",
        f"into agent `{plan.agent_id}` on {datetime.now(UTC).isoformat(timespec='seconds')}.",
        "",
        "## What moved",
        "",
        "- `openclaw.json` — default model, providers, MCP servers, agent behaviour, TTS, approvals, browser, channel token references.",
        "- `agents/<agent-id>/models.json` — the per-agent provider catalog (same providers as `openclaw.json`).",
        "- `agents/<agent-id>/` — SOUL.md, AGENTS.md, IDENTITY.md, MEMORY.md, USER.md, TOOLS.md (when present) and skills.",
        "- `.env.example` — every referenced environment variable, empty. Fill it in; no secret value was exported.",
        "- `exec-approvals.json` — the Hermes command allowlist, when one was configured.",
        "",
        "## What did NOT move (archive only)",
        "",
        f"- `archive/sessions/` — {session_count} session(s) as an index plus a state.db snapshot. Sessions are not loaded into the external agent.",
        f"- `archive/cron/` — {cron_count} cron job(s). Recreate the ones you still want with the external agent's scheduler; they were not activated.",
        f"- `archive/plugins/` — {plugin_count} installed plugin(s). Hermes plugins do not run under the external agent.",
        "- `archive/hermes-state/` — other Hermes-only state files, for manual review.",
        "",
        "The archive snapshot may contain secrets that were inside sessions or plugin state. It is chmod 0600 and outside the live external-agent tree; keep it that way.",
        "",
        "## Next steps",
        "",
    ]
    for step in plan.next_steps:
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


def build_export_plan(
    options: ExportOptions,
) -> tuple[ExportPlan, dict[str, Any], dict[str, str], dict[str, Any]]:
    """Build the export plan without writing anything.

    Returns ``(plan, config, env, external_agent_document)``; the document is
    returned so apply can write exactly what was planned.
    """
    source = discover_hermes_source(options.source)
    if not source.has_state:
        raise ValueError(
            f"No Hermes state found at {source.root}. Pass --source <path> if it lives elsewhere."
        )
    config, env = read_source_config(source.root)
    agent_id = options.agent_id or default_agent_id(source.root)
    out_dir = (
        Path(options.out).expanduser().resolve()
        if options.out
        else default_output_dir(agent_id)
    )
    workspace_dir = out_dir / "agents" / agent_id
    archive_dir = out_dir / "archive"

    model_ref = resolve_hermes_model_ref(config, env)
    fallbacks = resolve_model_fallbacks(config, env)
    providers, provider_manual = collect_hermes_providers(config, env)
    add_selected_model_to_provider(providers, model_ref)
    provider_entries = {entry.id: provider_config_entry(entry) for entry in providers}
    mcp_servers, mcp_manual = collect_mcp_servers(config, env)
    has_memory_files = bool(source.memory_path or source.user_path)
    document, channel_vars = build_export_config(
        config,
        model_ref=model_ref,
        fallbacks=fallbacks,
        providers=provider_entries,
        mcp_servers=mcp_servers,
        env=env,
        has_memory_files=has_memory_files,
    )
    referenced_vars = sorted(
        set(_referenced_env_vars(document))
        | set(channel_vars)
        | {entry.api_key_env for entry in providers if entry.api_key_env}
    )

    items: list[ExportItem] = []
    warnings: list[str] = []
    items.append(
        ExportItem(
            id="config:openclaw.json",
            kind="config",
            action="write",
            target=str(out_dir / "openclaw.json"),
            status="conflict"
            if (out_dir / "openclaw.json").exists() and not options.overwrite
            else "planned",
            reason="target already exists"
            if (out_dir / "openclaw.json").exists() and not options.overwrite
            else None,
            message="Exported external-agent config.",
        )
    )
    items.append(
        ExportItem(
            id="config:models.json",
            kind="config",
            action="write",
            target=str(workspace_dir / "models.json"),
            status="conflict"
            if (workspace_dir / "models.json").exists() and not options.overwrite
            else "planned",
            reason="target already exists"
            if (workspace_dir / "models.json").exists() and not options.overwrite
            else None,
            message="Per-agent provider catalog.",
        )
    )
    items.append(
        ExportItem(
            id="config:env-example",
            kind="config",
            action="write",
            target=str(out_dir / ".env.example"),
            message="Referenced environment variables, empty.",
        )
    )
    if render_exec_approvals(config.get("command_allowlist")) is not None:
        items.append(
            ExportItem(
                id="config:exec-approvals",
                kind="config",
                action="write",
                target=str(out_dir / "exec-approvals.json"),
                message="Hermes command allowlist as external-agent exec-approvals.json.",
            )
        )
    items.append(
        ExportItem(
            id="config:readme",
            kind="config",
            action="write",
            target=str(out_dir / "README.md"),
            message="Export readme: what moved, what was archived, next steps.",
        )
    )

    workspace_files: dict[str, Path] = {}
    for filename, path in (
        ("SOUL.md", source.soul_path),
        ("AGENTS.md", source.agents_path),
        ("IDENTITY.md", source.identity_path),
        ("MEMORY.md", source.memory_path),
        ("USER.md", source.user_path),
        ("TOOLS.md", source.tools_path),
    ):
        if path is not None:
            workspace_files[filename] = path
    items.extend(
        plan_workspace_files(
            source.root, workspace_dir, options.overwrite, workspace_files
        )
    )
    items.extend(plan_skills(source.skills_dirs, workspace_dir, options.overwrite))
    items.extend(
        plan_archive(source.root, archive_dir, source.archive, options.overwrite)
    )

    for entry in provider_manual:
        items.append(
            manual_item(
                entry["id"], entry["source"], entry["message"], entry["recommendation"]
            )
        )
    for entry in mcp_manual:
        items.append(
            manual_item(
                entry["id"], entry["source"], entry["message"], entry["recommendation"]
            )
        )
    memory = config.get("memory")
    memory_provider = memory.get("provider") if isinstance(memory, dict) else None
    if memory_provider == "honcho":
        items.append(
            manual_item(
                "manual:memory-provider:honcho",
                "config.yaml:memory.provider",
                "Hermes used Honcho memory. The external agent keeps built-in memory selected until the matching plugin is installed and reviewed.",
                "Install or review the Honcho memory plugin before selecting it for plugins.slots.memory.",
            )
        )
    elif memory_provider and memory_provider not in ("builtin", "file", "files"):
        items.append(
            manual_item(
                f"manual:memory-provider:{memory_provider}",
                "config.yaml:memory.provider",
                f'Hermes memory provider "{memory_provider}" does not have a known external-agent mapping.',
                "Install or configure an equivalent external-agent memory plugin manually.",
            )
        )
    _channels, _channel_vars = collect_channel_references(env)
    if _channel_vars:
        items.append(
            manual_item(
                "manual:channels-allowlist",
                "Hermes gateway channel config",
                "Channel allowlists were not exported: Hermes stores them per-adapter and the external agent reads them from credentials/<platform>-default-allowFrom.json.",
                "Recreate each channel's allowFrom list in the external agent after the gateway starts.",
            )
        )
    if uses_retired_qwen_provider(config):
        items.append(
            manual_item(
                "manual:auth-reauthenticate:qwen",
                str(source.config_path or source.root),
                "Hermes Qwen Portal OAuth and Qwen CLI credentials cannot be reused by the external agent.",
                "Authenticate qwen with an API key after migration: openclaw onboard --auth-choice qwen-api-key.",
            )
        )
        warnings.append(
            "A retired Qwen provider was referenced; its credentials cannot be reused."
        )
    for var in referenced_vars:
        items.append(
            ExportItem(
                id=f"secret-ref:{var}",
                kind="secret-ref",
                action="reference",
                target=f"${{{var}}}",
                status="planned",
                message=f"Environment variable {var} is referenced, never valued.",
            )
        )
    if not model_ref:
        warnings.append(
            "No default model found in Hermes config; agents.defaults.model was not set."
        )
    if not providers:
        warnings.append(
            "No Hermes providers could be exported; add a provider in the external agent after migration."
        )
    if any(item.status == "conflict" for item in items):
        warnings.append(
            "Conflicts were found. Re-run with --overwrite to replace conflicting targets."
        )
    if source.archive:
        warnings.append(
            "Some Hermes state is archive-only. It is copied into archive/ for manual review, not loaded into the external agent."
        )
    if any(item.kind == "manual" for item in items):
        warnings.append(
            "Some Hermes settings require manual review before they can be activated safely."
        )

    plan = ExportPlan(
        source_root=str(source.root),
        target_root=str(out_dir),
        agent_id=agent_id,
        items=items,
        warnings=warnings,
        env_vars=referenced_vars,
        next_steps=[
            "Run openclaw doctor after copying this tree into your external-agent home.",
            "Fill in .env.example values (or set them in your shell) before starting the gateway.",
            "Start a private session and test one harmless MCP server, then search memory for one known imported fact.",
            "Only then connect external channels or scheduled tasks.",
        ],
    )
    return plan, config, env, document


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def apply_export_plan(
    plan: ExportPlan,
    config: dict[str, Any],
    env: dict[str, str],
    document: dict[str, Any],
    *,
    options: ExportOptions,
) -> ExportReport:
    """Execute a plan and validate the result. Returns the export report."""
    out_dir = Path(plan.target_root)
    archive_dir = out_dir / "archive"
    out_dir.mkdir(parents=True, exist_ok=True)
    applied: list[ExportItem] = []
    source_files: dict[str, Path] = {}

    for item in plan.items:
        if item.status != "planned":
            applied.append(item)
            continue
        if item.kind == "manual" or item.kind == "secret-ref":
            applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            continue
        if item.id == "config:openclaw.json":
            _write_json(Path(item.target), document)
            applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            continue
        if item.id == "config:models.json":
            _write_json(
                Path(item.target),
                {"providers": document.get("models", {}).get("providers", {})},
            )
            applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            continue
        if item.id == "config:env-example":
            Path(item.target).write_text(_env_example(plan.env_vars), encoding="utf-8")
            applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            continue
        if item.id == "config:exec-approvals":
            content = render_exec_approvals(config.get("command_allowlist"))
            if content is not None:
                Path(item.target).write_text(content, encoding="utf-8")
            applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            continue
        if item.id == "config:readme":
            applied.append(item)  # written after the archive counts are known
            continue
        if item.kind == "archive":
            applied.append(apply_archive_item(item))
            continue
        # workspace / memory / skill copies.
        if item.source and item.target:
            try:
                copy_tree(
                    Path(item.source), Path(item.target), overwrite=options.overwrite
                )
                if item.kind in ("workspace", "memory"):
                    source_files[Path(item.target).name] = Path(item.source)
                applied.append(ExportItem(**{**item.__dict__, "status": "migrated"}))
            except OSError as exc:
                applied.append(
                    ExportItem(
                        **{**item.__dict__, "status": "error", "reason": str(exc)}
                    )
                )
            continue
        applied.append(
            ExportItem(
                **{**item.__dict__, "status": "error", "reason": "no source or target"}
            )
        )

    # Archive companions, written after the archive copies land.
    source_root = Path(plan.source_root)
    sessions_index = list_sessions_index(source_root)
    if sessions_index or (source_root / "state.db").is_file():
        write_sessions_index(archive_dir, sessions_index)
    cron_jobs = load_cron_jobs(source_root)
    if cron_jobs or (source_root / "cron").is_dir():
        write_cron_readme(archive_dir, cron_jobs)
    plugins_dir = source_root / "plugins"
    plugin_count = (
        len([p for p in plugins_dir.iterdir() if not p.name.startswith(".")])
        if plugins_dir.is_dir()
        else 0
    )
    if plugins_dir.is_dir():
        write_plugins_index(archive_dir, plugins_dir)
    readme_item = next((i for i in applied if i.id == "config:readme"), None)
    if readme_item is not None and readme_item.status == "planned":
        readme_plan = ExportPlan(**{**plan.__dict__, "items": applied})
        Path(readme_item.target).write_text(
            _export_readme(
                readme_plan,
                session_count=len(sessions_index),
                cron_count=len(cron_jobs),
                plugin_count=plugin_count,
            ),
            encoding="utf-8",
        )
        applied[applied.index(readme_item)] = ExportItem(
            **{**readme_item.__dict__, "status": "migrated"}
        )

    applied_plan = ExportPlan(**{**plan.__dict__, "items": applied})
    secret_values = harvest_secret_values(config, env)
    validation_errors = validate_export_tree(
        out_dir, source_files=source_files, secret_values=secret_values
    )
    report = ExportReport(
        plan=applied_plan,
        output_dir=str(out_dir),
        validated=not validation_errors,
        validation_errors=validation_errors,
    )
    report.write(out_dir / "export-report.json")
    if options.zip:
        zip_path = shutil.make_archive(
            str(out_dir), "zip", root_dir=out_dir.parent, base_dir=out_dir.name
        )
        report.zip_path = zip_path
        report.write(out_dir / "export-report.json")
    return report


def export_external_agent(options: ExportOptions) -> int:
    """Plan, confirm, apply and validate one reverse migration.

    Returns a process exit code: 0 on success (including a clean dry
    run), 1 when the export failed validation or the user declined.
    """
    try:
        plan, config, env, document = build_export_plan(options)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    summary = plan.summary
    print(f"Source: {plan.source_root}")
    print(f"Target: {plan.target_root} (agent: {plan.agent_id})")
    print(
        f"Plan: {summary['total']} items — {summary['planned']} to write, "
        f"{summary['conflict']} conflict, {sum(1 for i in plan.items if i.kind == 'manual')} manual review"
    )
    for warning in plan.warnings:
        print(f"Warning: {warning}")
    if options.dry_run:
        for item in plan.items:
            label = f"[{item.status}] {item.id}"
            if item.target:
                label += f" -> {item.target}"
            print(f"  {label}")
            if item.kind == "manual" and item.message:
                print(f"      {item.message}")
        print("Dry run: nothing was written.")
        return 0
    if not options.yes:
        answer = input("Export into the target directory? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Export cancelled.")
            return 1
    report = apply_export_plan(plan, config, env, document, options=options)
    print(report.render_summary())
    if report.validation_errors:
        return 1
    if report.zip_path:
        print(f"Zip: {report.zip_path}")
    return 0


def cmd_export_agent(args) -> int:
    """``hermes export-agent external-agent`` handler (argparse entry point)."""
    if getattr(args, "export_agent", None) not in (None, "external-agent"):
        print(f"Error: unknown export target: {getattr(args, 'export_agent', None)}")
        return 1
    return export_external_agent(
        ExportOptions(
            source=getattr(args, "source", None),
            out=getattr(args, "out", None),
            agent_id=getattr(args, "agent_id", None),
            overwrite=bool(getattr(args, "overwrite", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            yes=bool(getattr(args, "yes", False)),
            zip=bool(getattr(args, "zip", False)),
        )
    )
