"""Traceable inventory of Telegram setup controls and their CLI-only boundaries.

The CLI tree below is a read-only catalogue of the setup/parser flows; it does
not dispatch commands. Telegram-editable fields are profile-scoped, audited
existing config values. Operations requiring credentials, installs, explicit
security consent, destructive state changes, or service lifecycle actions stay
in their established CLI/controller flows.
"""
from __future__ import annotations

from .setup_fields import FIELDS, SECTIONS

# Crosswalk for setup renderers and regression coverage. Navigation goes to an
# existing owner-only controller; field controls use the shared profile setter.
PORTABLE_AREAS = {
    "model_provider": {
        "label": "Models and providers", "action": "nav:model / nav:providers", "fields": (),
        "note": "Selection reuses the existing model/provider controllers; credentials and provider-specific account setup stay in CLI.",
    },
    "tts": {
        "label": "Text to speech", "action": "section:voice",
        "fields": ("tts.provider", "tts.edge.voice", "tts.openai.voice"),
        "note": "Provider preference is explicitly confirmed, but no credentials, OAuth, installs, downloads or audio generation occur.",
    },
    "terminal": {
        "label": "Terminal", "action": "section:terminal", "fields": tuple(SECTIONS["terminal"][1]),
        "note": "Built-in backend preference is explicitly confirmed; no connection/probe/install runs, and plugin backends are not offered.",
    },
    "gateway_platforms": {"label": "Gateway and platform preferences", "action": "section:gateway", "fields": tuple(SECTIONS["gateway"][1])},
    "tools_mcp": {"label": "Tools and MCP", "action": "nav:setuptools", "fields": ()},
    "agent_personal": {"label": "Agent and personal settings", "action": "section:agent / nav:settings", "fields": tuple(SECTIONS["agent"][1])},
    "display_compression": {"label": "Display and compression", "action": "section:display / section:compression", "fields": tuple(SECTIONS["display"][1]) + tuple(SECTIONS["compression"][1])},
    "skills_memory": {"label": "Skills and memory", "action": "section:skills / section:memory / nav:skills", "fields": tuple(SECTIONS["skills"][1]) + tuple(SECTIONS["memory"][1])},
    "browser_dashboard": {"label": "Browser and dashboard", "action": "nav:browser / nav:dashboard", "fields": tuple(SECTIONS["browser"][1])},
    "diagnostics": {"label": "Diagnostics", "action": "nav:logs", "fields": ()},
}

# Read-only nested-command catalogue. Records are (label, exact command path, why
# the action is kept out of Telegram setup). Paths reflect Hermes' actual parser
# names and are intentionally not arbitrary executable callback values.
CLI_ONLY_SECTIONS = (
    ("Model and provider credentials", (
        ("Add a pooled provider credential", "hermes auth add PROVIDER", "Credential capture, OAuth consent and API-key entry stay in the CLI's secure auth flow."),
        ("List provider credential entries", "hermes auth list [PROVIDER]", "Auth pool management can reveal account metadata; use the CLI's masked auth view."),
        ("Remove or reorder a credential", "hermes auth remove PROVIDER TARGET / hermes auth priority PROVIDER TARGET PRIORITY", "Changes credential fallback and priority state rather than ordinary model preference."),
        ("Reset a credential cooldown", "hermes auth reset PROVIDER [TARGET]", "Mutates auth-pool recovery state and remains an explicit CLI action."),
        ("Refresh OAuth credentials", "hermes auth refresh PROVIDER [TARGET]", "Refresh-token handling is an account-auth operation, not a Telegram config edit."),
        ("Inspect provider auth status", "hermes auth status PROVIDER", "Credential/auth details stay behind the existing CLI masking and profile context."),
        ("Configure provider-specific account or endpoint setup", "hermes setup model", "The model/provider selector is linked from Telegram; account authorization, key setup, custom endpoints and provider-specific validation use the complete CLI flow."),
        ("Nous Portal OAuth and managed inference opt-in", "hermes setup --portal", "Interactive OAuth consent and managed service opt-in require the existing CLI flow."),
    )),
    ("TTS credentials and local prerequisites", (
        ("Configure TTS provider prerequisites", "hermes setup tts", "Telegram can change the confirmed provider preference only; this CLI flow securely collects keys/OAuth and offers local-engine installs."),
        ("Use provider pooled credentials", "hermes auth add PROVIDER", "Provider auth is independent of the TTS preference and must not be collected by setup callbacks."),
        ("Run declared installer hooks", "hermes tools post-setup KEY", "Runs npm/pip/binary setup hooks or downloads; setup callbacks do not install software or models."),
    )),
    ("Terminal backend prerequisites", (
        ("Configure backend-specific resources, SSH connection, or plugin backend", "hermes setup terminal", "Telegram exposes only a confirmed built-in backend selector plus cwd/timeout. This CLI flow captures SSH details, sets backend-specific resources, discovers plugin backends and offers setup probes/install steps."),
        ("Inspect terminal backend choices and environment", "hermes config get terminal.backend / hermes status", "The config value is editable from Telegram, but diagnosing missing backends or environment bridges remains a CLI/status task."),
    )),
    ("Gateway, messaging platforms and service lifecycle", (
        ("Configure Telegram/platform credentials", "hermes gateway setup", "Platform credentials and adapter authorization are secrets and platform-specific; Telegram setup never prompts for them."),
        ("Run gateway in foreground", "hermes gateway run", "Starts message processing and can affect live sessions; never start a service from setup UI."),
        ("Start, stop, restart or inspect gateway service", "hermes gateway start / hermes gateway stop / hermes gateway restart / hermes gateway status", "Lifecycle operations affect live conversations and require explicit CLI authorization."),
        ("Install or uninstall gateway service", "hermes gateway install / hermes gateway uninstall", "Service installation/removal affects the host and can require OS-level permissions."),
        ("List profile gateway services", "hermes gateway list", "Fleet/profile service inventory is a read-only CLI operation; no hidden profile switching from setup."),
    )),
    ("Tools and MCP", (
        ("Edit cross-platform standard toolsets", "hermes tools", "Telegram has a staged per-Telegram editor for visible standard toolsets; this is the full cross-platform CLI editor."),
        ("List tools or toggle named toolsets", "hermes tools list [--platform PLATFORM] / hermes tools enable NAME / hermes tools disable NAME", "Telegram limits edits to visible standard toolsets for the current platform; arbitrary MCP/plugin names remain in the CLI."),
        ("Add/remove/list/test/configure MCP servers", "hermes mcp add / hermes mcp remove / hermes mcp list / hermes mcp test / hermes mcp configure", "MCP servers can launch external processes or connect to arbitrary services; do not probe or execute them during setup."),
        ("Authenticate or reauthenticate MCP server", "hermes mcp login / hermes mcp reauth", "OAuth consent and credential capture must remain in the CLI."),
        ("Inspect catalog or install MCP integration", "hermes mcp catalog / hermes mcp install", "Catalog installation can change tools and execute server setup; no installer is invoked by Telegram."),
        ("Serve Hermes over MCP", "hermes mcp serve", "Starts a network service and is not a profile preference."),
        ("Run a provider-declared tool installer", "hermes tools post-setup KEY", "Runs installation hooks and may modify the host; it is never a setup button."),
    )),
    ("Agent, personal settings and profiles", (
        ("Edit other registered profile config paths", "hermes config get KEY / hermes config set KEY VALUE / hermes config unset KEY", "Telegram only exposes individually audited existing fields; arbitrary values and unknown paths are not writable through callbacks."),
        ("Set or remove secret-routed settings", "hermes config set NAME VALUE / hermes config unset NAME", "Secret-shaped keys route to the profile .env or auth lifecycle; CLI handles masking and secret policy."),
        ("Change global security policy or approval transports", "hermes config set security.KEY VALUE", "Network access, credential redaction, approval transport and safety gates are high-impact decisions, not generic toggles."),
        ("List, select, create, clone, delete or migrate profiles", "hermes profile list / hermes profile use NAME / hermes profile create NAME / hermes profile delete NAME / hermes profile migrate-identity OLD_NAME NEW_NAME", "Profile operations affect isolation, active routing and profile identity; keep them in the dedicated CLI."),
        ("Export, import, install or update profile distribution", "hermes profile export NAME / hermes profile import FILE / hermes profile install NAME / hermes profile update NAME", "Distribution import/update can replace profile-owned files and requires explicit review."),
    )),
    ("Skills, memory and persisted state", (
        ("Browse, search, install or inspect skills", "hermes skills browse / hermes skills search QUERY / hermes skills install SKILL / hermes skills inspect SKILL", "A skill may contain executable instructions; the CLI trust/source flow is retained. Telegram offers write-approval preference and existing skills controller."),
        ("Trust or untrust a skill", "hermes skills trust SKILL / hermes skills untrust SKILL", "Trust changes execution/instruction policy and needs explicit review."),
        ("List/check/update/audit/uninstall/reset skills", "hermes skills list / hermes skills check / hermes skills update / hermes skills audit / hermes skills uninstall SKILL / hermes skills reset", "These mutate or scan prompt-bound files; use the existing skill manager/CLI rather than executing source changes from setup."),
        ("Manage skill sources, registry or snapshots", "hermes skills tap list / hermes skills tap add REPO / hermes skills tap remove NAME / hermes skills snapshot export FILE / hermes skills snapshot import FILE", "Changes trusted skill sources or installs imported content; stays in CLI."),
        ("Configure or inspect memory provider", "hermes memory setup [PROVIDER] / hermes memory status", "External providers can require credentials and post-setup; Telegram exposes only built-in memory preferences."),
        ("Disable or reset memory provider/data", "hermes memory off / hermes memory reset [--target all|memory|user]", "Reset can erase persistent user data; keep destructive commands in the CLI."),
    )),
    ("Browser, dashboard, Web UI and diagnostics", (
        ("Close browser holding a real profile", "hermes browser close-profile [--browser BROWSER]", "Terminates a browser process tree and loses unsaved tabs; requires the user's explicit OK in its own CLI flow."),
        ("Start Dashboard/Web UI", "hermes dashboard", "Starts a listener and web application; Telegram routes to the existing Dashboard/Web UI controller and never starts services itself."),
        ("Start headless dashboard backend", "hermes serve", "Starts a long-running server process and must remain an explicit service action."),
        ("Register dashboard endpoint", "hermes dashboard register", "Changes endpoint registration and is not a display/config preference."),
        ("Run system diagnostics", "hermes doctor", "Performs environment checks; setup does not run probes or surface their potentially sensitive output."),
        ("Run plugin diagnostics", "hermes plugins doctor TARGET", "Plugin doctor can inspect plugin code/config; use the CLI's explicit diagnostic command."),
        ("Inspect logs", "hermes logs [--follow] [--level LEVEL] [--session SESSION]", "Telegram links to its existing private logs controller; richer log/diagnostic access remains in the CLI."),
    )),
)


def iter_cli_only_items():
    """Yield ``(area, label, exact command path, reason)`` records in display order."""
    for area, items in CLI_ONLY_SECTIONS:
        for label, command, reason in items:
            yield area, label, command, reason


def cli_command_inventory() -> tuple[tuple[str, str, str, str], ...]:
    """Return all flat (area, label, command, reason) records for UI rendering."""
    return tuple(iter_cli_only_items())


def cli_command_children(area: str) -> tuple[tuple[str, str, str], ...]:
    """Return (label, command, reason) leaves for a named CLI-only area."""
    for area_label, items in CLI_ONLY_SECTIONS:
        if area_label == area:
            return items
    return ()


def portable_field_keys() -> frozenset[str]:
    """Return the audited setting keys available through Telegram setup."""
    return frozenset(FIELDS)
