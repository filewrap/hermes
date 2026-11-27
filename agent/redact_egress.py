"""Terminal / egress-output redaction for ``agent.redact`` (moved from agent.redact).

One scrub per output surface: derived text (strict, always on), egress text
leaving the process, terminal output (env-dump vs code-file policy), and
execute_code output (env-dump lines inside script stdout). The core scrubber
(``redact_sensitive_text``) and its policy helpers stay on the facade; this
sibling imports them lazily so the facade can re-export this module's names.
"""

from __future__ import annotations

import os
import re
import shlex

# Shared with agent/file_safety's read-block list so the two defenses can't
# drift: a blocked file_tools read that falls back to ``cat`` is still caught.
from agent.file_safety import HOME_CREDENTIAL_FILES, _BLOCKED_PROJECT_ENV_BASENAMES as _ENV_FILE_BASENAMES
from agent.redact_command_segments import _command_segments


def redact_derived_text(text: object) -> str:
    """Strictly scrub model-visible text derived from stored conversation data.

    Derived summaries and search metadata can re-enter unrelated prompts, so
    the user's ordinary redaction opt-out must not expose secrets through them.
    """
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(
        "" if text is None else str(text),
        force=True,
        redact_url_credentials=True,
    )


# Commands whose stdout is an env-var dump: terminal redaction runs the
# ENV-assignment pass (code_file=False) for these so opaque tokens with no vendor
# prefix are masked; everything else uses code_file=True (``MAX_TOKENS=100``).
# Commands whose stdout is an environment-variable dump (KEY=value lines), NOT source code.
# ``MY_SERVICE_TOKEN=abc123randomstring``) are still masked. For all other commands, code_file=True is used
# to avoid mangling legitimate source/config dumps (``MAX_TOKENS=100``, ``"apiKey": "x"`` fixtures,
# ``postgresql://{user}`` f-string templates). See issue #43025.
_ENV_DUMP_COMMANDS = frozenset({"env", "printenv", "set", "export", "declare"})

# Commands that read file contents to stdout, plus the filter readers (``grep``/``awk``/``sed``)
# the model reaches for on config files. A secret-bearing target (``.env`` per AGENTS.md,
# a shell rc/profile, Hermes' own ``config.yaml`` where ``hermes mcp add --env`` writes
# tokens) is a credential dump, so the ENV/YAML assignment pass must run. Arbitrary
# ``config.yaml`` / source files stay on the code_file path (``MAX_TOKENS: 100``).
_FILE_READ_COMMANDS = frozenset({
    "cat", "head", "tail", "type", "bat", "less", "more", "nl",
    "zcat", "tac", "view", "batcat", "grep", "awk", "sed", "get-content", "gc",
})
# PowerShell binds a parameter value with a colon: ``Get-Content -Path:~\_netrc`` (also via the
# ``cat``/``type``/``gc`` aliases). Not applied to the pattern-first filters, whose positional count matters.
_PS_COLON_PARAM_RE = re.compile(r"-[A-Za-z]+:(.+)")
_SHELL_RC_BASENAMES = frozenset({
    ".bashrc", ".bash_profile", ".bash_login", ".profile",
    ".zshrc", ".zprofile", ".zlogin", ".zshenv",
})
# Filter readers take a PATTERN/program as their first positional; only the operands after
# it are files, so ``grep .bashrc app.py`` must not gate on the pattern.
_PATTERN_FIRST_COMMANDS = frozenset({"grep", "awk", "sed"})
_HERMES_HOME_PREFIXES = ("$HERMES_HOME/", "${HERMES_HOME}/")
# ``$HOME/.hermes/config.yaml`` keeps the ``.hermes`` segment, so stripping the prefix is
# enough to gate it; ``~/`` already survives the ``$``-bearing-path bail-out.
_HOME_PREFIXES = ("$HOME/", "${HOME}/")


def _is_under_hermes_home(path: str) -> bool:
    """True when an absolute ``config.yaml`` path sits under the active Hermes home or root.

    The default home's basename is an installation detail — ``.hermes`` on POSIX, ``hermes``
    under ``AppData/Local`` on Windows — and a resolved path never spells ``$HERMES_HOME``,
    so the literal-segment test in ``_is_secret_file_arg`` cannot see a native Windows path.
    Compare against the resolved homes instead. Only reached for a ``config.yaml`` basename,
    so the resolve cost stays off the per-token command scan.
    """
    from agent.file_safety import _hermes_dirs
    from tools.environments.sandbox_scope import sandbox_lexical_path
    # A hosted path names a file in the sandbox: judged as written (never empty), not resolved on this host.
    try:
        target = os.path.normcase(sandbox_lexical_path(path) or os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return False
    for home in _hermes_dirs():
        try:
            base = os.path.normcase(os.path.realpath(str(home)))
        except (OSError, ValueError):
            continue
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _is_secret_file_arg(arg: str) -> bool:
    """``.env``-style, shell rc or credential-store basename (``.netrc``, ``.pgpass``, ...)
    anywhere; ``credentials`` / ``config`` only under ``.aws`` (the bare names are too generic), and
    ``config.yaml`` only under a ``.hermes`` directory, ``$HERMES_HOME``, or the resolved
    Hermes home (never arbitrary YAML). The resolved-home arm is what covers native Windows,
    where the home directory is ``%LOCALAPPDATA%\\hermes`` and carries no ``.hermes`` segment."""
    path = arg.strip("\"'").replace("\\", "/")
    hermes_home = False
    for prefix in _HERMES_HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            hermes_home = True
            break
    for prefix in _HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if "$" in path:
        return False
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    if parts[-1] in _ENV_FILE_BASENAMES or parts[-1] in _SHELL_RC_BASENAMES:
        return True
    if parts[-1] in HOME_CREDENTIAL_FILES or parts[-2:] in ([".aws", "credentials"], [".aws", "config"]):
        return True
    # ``config.yaml`` plus the ``config.yaml.good.<stamp>`` / ``.corrupt.<stamp>`` copies Hermes
    # writes under ``backups/config/`` — same contents, same secrets.
    if parts[-1] != "config.yaml" and not parts[-1].startswith(("config.yaml.good.", "config.yaml.corrupt.")):
        return False
    return hermes_home or ".hermes" in parts[:-1] or _is_under_hermes_home(path)


def _command_reads_secret_file(command: str | None) -> bool:
    """True if ``command`` reads a secret-bearing file (see ``_is_secret_file_arg``) to
    stdout. Defense-in-depth, not a boundary: indirect reads (``sudo cat .env``, ``$(cat
    .env)``, unresolved variable paths) are not detected, matching ``is_env_dump_command``."""
    if not command or not isinstance(command, str):
        return False
    for seg in _command_segments(command):
        tokens = seg.split()  # not shlex: it mangles Windows paths (``C:\Users\...\.env``)
        if not tokens:
            continue
        reader = tokens[0].rsplit("/", 1)[-1].lower()
        if reader not in _FILE_READ_COMMANDS:
            continue
        positional = [
            arg if not arg.startswith("-") else m.group(1)
            for arg in tokens[1:]
            if not arg.startswith("-")
            or (reader not in _PATTERN_FIRST_COMMANDS and (m := _PS_COLON_PARAM_RE.fullmatch(arg)))
        ]
        if reader in _PATTERN_FIRST_COMMANDS:
            positional = positional[1:]
        if any(_is_secret_file_arg(arg) for arg in positional):
            return True
    return False


def is_env_dump_command(command: str | None) -> bool:
    """True if any pipeline/sequence segment starts with an _ENV_DUMP_COMMANDS
    token. Conservative: unrecognized → False (callers fall back to code_file=True)."""
    if not command or not isinstance(command, str):
        return False
    for seg in _command_segments(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens and tokens[0] in _ENV_DUMP_COMMANDS:
            return True
    return False


REDACTION_UNAVAILABLE = "[redaction-unavailable]"
# The opaque branch needs a 20-char floor (the floor the gateway/A2A sweeps always had): without it the
# English word "bearer" turns "the bearer of bad news" into "Bearer [redacted] bad news" on every chat
# reply. The bracket branch folds an already-masked residue ("Bearer [redacted-jwt]") to one marker.
# ASCII lookbehind (not Unicode ``\b``): a Unicode word neighbor can hide ``Bearer <opaque>``
# the same way prefix tokens used to hide behind ``\b``.
_BEARER_RESIDUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])Bearer\s+(?:\[[^\]]+\]|[A-Za-z0-9._~+/-]{20,}=*)",
    re.IGNORECASE,
)


def redact_for_egress(text: str, *, urls: bool = False, keep_phone_numbers: bool = False) -> str:
    """The one scrub for text leaving the process for a remote reader (chat platforms, A2A peers,
    telemetry). ``redact_sensitive_text(force=True)`` — the only secret-pattern list — plus a bearer
    sweep, because a ``Bearer <opaque>`` value with no vendor prefix carries no shape the prefix
    matcher can key on. Fails CLOSED: if the redactor raises, the raw text is never returned.
    ``keep_phone_numbers`` skips only the E.164 pass (``redact_for_chat_egress``'s opt-in).

    ``urls=True`` also masks credential-named query params and URL userinfo — for archival sinks
    (memory providers, telemetry) where no one follows the URL. Chat delivery leaves it off so a magic
    link or OAuth URL the agent hands the user still works."""
    from agent.redact import redact_sensitive_text

    text = str(text or "")
    try:
        text = redact_sensitive_text(text, force=True, redact_url_credentials=urls,
                                     keep_phone_numbers=keep_phone_numbers)
    except Exception:  # health: allow BLE001 -- redaction boundary: any scrub failure degrades to REDACTION_UNAVAILABLE, never raises into telemetry/memory callers
        return REDACTION_UNAVAILABLE
    if "earer" in text.lower():  # IGNORECASE regex; the gate must fold case too (#108807)
        text = _BEARER_RESIDUE_RE.sub("Bearer [redacted]", text)
    return text


def redact_terminal_output(output: str, command: str | None = None, *, force: bool = False) -> str:
    """Single redaction policy for ALL terminal-output surfaces: the ENV/YAML-assignment
    pass runs only when ``command`` is an env dump or reads a secret-bearing file (``.env``,
    shell rc, a credential store like ``.netrc``, Hermes ``config.yaml``); otherwise
    code_file=True avoids false positives on source/config dumps."""
    from agent.redact import (
        _redact_enabled,
        _redact_python_diagnostic_repr_fields,
        redact_sensitive_text,
    )

    if not output:
        return output
    secret_file = _command_reads_secret_file(command)
    code_file = not (secret_file or is_env_dump_command(command))
    redacted = redact_sensitive_text(output, force=force, code_file=code_file, secret_file=secret_file)
    # Source-preserving output still gets the Python-repr pass on high-confidence
    # diagnostic lines (pytest ``E   `` introspection, final exception lines): that is
    # where {'BRAVE_API_KEY': '…'} leaks, not in source dumps.
    if code_file and (force or _redact_enabled()) and ":" in redacted and "'" in redacted:
        redacted = _redact_python_diagnostic_repr_fields(redacted)
    return redacted



# execute_code output is script stdout, not a command line, so redact_terminal_output's command
# classifier never sees an env dump there (#95509). A whole line of ``NAME=value`` (optionally
# ``export``-prefixed, no space around ``=``) is the ``.env`` / ``env`` shape; source assignments
# (``MAX_TOKENS = 100``, indented or embedded code) do not match and keep the code_file behaviour.
_ENV_DUMP_LINE_RE = re.compile(r"^((?:export[ \t]+)?)([A-Z][A-Z0-9_]*)=(\S.*)$", re.MULTILINE)


def _mask_env_dump_line(m: re.Match[str], mask_env) -> str:
    from agent.redact import _ENV_ASSIGN_RE, _mask_token, _should_redact_assignment

    # The whole line is one assignment, so its value is the rest of the line: ``env`` prints
    # ``DB_PASSWORD=first second`` raw and a .env quotes it. _ENV_ASSIGN_RE's ``\S+`` would stop at
    # the first space and leave the remaining words visible.
    prefix, key, value = m.groups()
    quote = value[0] if len(value) > 1 and value[0] in "'\"" and value[-1] == value[0] else ""
    inner = value[1:-1] if quote else value
    if _should_redact_assignment(key, inner, check_keyword=True):
        return f"{prefix}{key}={quote}{_mask_token(inner)}{quote}"
    return _ENV_ASSIGN_RE.sub(mask_env, m.group(0))


def redact_code_output(output: str, *, force: bool = False) -> str:
    """Redaction for execute_code stdout/stderr: code_file=True, plus the ENV-assignment pass on
    lines shaped like an env dump so opaque prefix-less credentials are masked."""
    from agent.redact import _assignment_sub, _mask_token, _redact_enabled, redact_sensitive_text

    redacted = redact_sensitive_text(output, force=force, code_file=True)
    if not redacted or "=" not in redacted or not (force or _redact_enabled()):
        return redacted
    mask_env = _assignment_sub(lambda g: f"{g[0]}={g[1]}{_mask_token(g[2])}{g[1]}", check_keyword=True)
    return _ENV_DUMP_LINE_RE.sub(lambda m: _mask_env_dump_line(m, mask_env), redacted)
