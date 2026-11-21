"""GitHub identity status (F8-thin): effective GitHub identity + credential health.

Thin read-only tool over the credential sources Hermes already has — the ``gh``
CLI (``skills/software-development/github/SKILL.md`` preflights on
``gh auth status``) and the skills-hub connector auth chain
(``tools/skills_hub_github.py`` ``GitHubAuth``). No worktree machinery, no
publish flow — that is F8-heavy, a later step.

External-agent ref: ``src/agents/tools/github-identity-status-tool.ts``
(gateway ``tools.github.status``, ``credentialState``/``refreshState``). The
``effective`` block mirrors the external agent's ``GitHubIdentityFactsSchema``
(``packages/gateway-protocol/src/schema/agents-models-skills.ts``) where Hermes
has an equivalent; the external agent's ``agentId``/``selectedScope`` gateway concepts
have no Hermes equivalent and are omitted. Hermes has no token-refresh
mechanism, so ``refreshState`` is always ``not_applicable`` and
``accessExpiresAtMs`` is null — ``gh``/PAT expiry is not exposed to us.

Secret safety: the raw ``gh auth status`` output is never echoed (newer ``gh``
prints a masked ``Token:`` line) — only parsed fields are reported, and the
``Token scopes`` line is the only token-adjacent line read. The connector
token itself is never read out of ``GitHubAuth``; only its method label and
whether the live API check accepted it. No check_fn: the tool must stay
visible precisely when no credential exists, otherwise it could never report
``unavailable``.
"""

import json
import logging
import re
import shutil
import subprocess
from typing import Any, Optional

from tools.registry import registry

logger = logging.getLogger("tools.github_identity_status")

_GH_TIMEOUT_S = 20
_API_TIMEOUT_S = 10
_GIT_TIMEOUT_S = 5
_API_USER_URL = "https://api.github.com/user"

_GITHUB_IDENTITY_STATUS_SCHEMA = {
    "name": "github_identity_status",
    "description": (
        "Inspect the effective GitHub account and credential health. "
        "Secret-free: reports the Git author and scopes, what `gh auth "
        "status` and the connector credential chain say, plus whether the "
        "credential actually answers the GitHub API. If the credential is not "
        "healthy, ask the operator to run `gh auth login` (or set GITHUB_TOKEN / "
        "GH_TOKEN), then check github_identity_status again."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# `gh auth status` writes one block per host; only the parsed fields below are
# ever reported, never the raw output (see module docstring).
_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")
_ACTIVE_ACCOUNT_RE = re.compile(r"Active account:\s*(true|false)", re.IGNORECASE)
_PROTOCOL_RE = re.compile(r"Git operations protocol:\s*(\S+)", re.IGNORECASE)
_SCOPES_RE = re.compile(r"Token scopes:\s*(.+)", re.IGNORECASE)


def _parse_gh_auth_status(text: str) -> dict[str, Any]:
    """Parse `gh auth status` output into hostname/login/protocol/scopes.

    Pure function (unit-tested without a `gh` binary). The first host block
    wins; a host with no `Logged in` line is reported as not logged in.
    """
    hostname: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if (
            stripped
            and not line.startswith((" ", "\t"))
            and "." in stripped
            and " " not in stripped
        ):
            hostname = stripped
            break
    match = _LOGGED_IN_RE.search(text)
    login = match.group(2) if match else None
    host = match.group(1) if match else hostname
    active_match = _ACTIVE_ACCOUNT_RE.search(text)
    protocol_match = _PROTOCOL_RE.search(text)
    scopes_match = _SCOPES_RE.search(text)
    scopes = (
        [s.strip().strip("'\"") for s in scopes_match.group(1).split(",") if s.strip()]
        if scopes_match
        else []
    )
    return {
        "hostname": host,
        "logged_in": login is not None,
        "login": login,
        "protocol": protocol_match.group(1) if protocol_match else None,
        "active_account": (active_match.group(1).lower() == "true")
        if active_match
        else None,
        "scopes": scopes,
    }


def _gh_env() -> dict[str, str]:
    """Env for `gh` calls that must never prompt.

    Mirrors `hermes_cli.git_credentials._gh_cli_token`: strip GH_TOKEN /
    GITHUB_TOKEN so `gh` reports its keyring login instead of echoing back an
    env token (a dead env token would hide a login that still works). The env
    token is still covered separately by the connector chain below.
    """
    try:
        from hermes_cli._subprocess_compat import noninteractive_git_env

        env = noninteractive_git_env()
        env["GH_PROMPT_DISABLED"] = "1"
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        return env
    except Exception:  # health: allow BLE001 -- env-helper failure falls back to a non-prompting env, never fails the probe
        from tools.environments.local import served_profile_child_env

        env = served_profile_child_env()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_PROMPT_DISABLED"] = "1"
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        return env


def _check_gh_cli() -> dict[str, Any]:
    """`gh auth status` state: available/logged-in/hostname/protocol/scopes."""
    gh = shutil.which("gh")
    if not gh:
        return {
            "available": False,
            "logged_in": False,
            "hostname": None,
            "login": None,
            "protocol": None,
            "active_account": None,
            "scopes": [],
        }
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        creationflags = windows_hide_flags()
    except Exception:  # health: allow BLE001 -- hide-flags helper unavailable means flags 0, never fails the probe
        creationflags = 0
    try:
        result = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GH_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env=_gh_env(),
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("gh auth status failed: %s", exc)
        return {
            "available": True,
            "logged_in": False,
            "hostname": None,
            "login": None,
            "protocol": None,
            "active_account": None,
            "scopes": [],
            "error": "gh auth status did not complete",
        }
    # `gh auth status` exits non-zero and prints "You are not logged into any
    # GitHub hosts" when no host is authenticated; its stdout is empty then.
    parsed = _parse_gh_auth_status(f"{result.stdout}\n{result.stderr}")
    return {"available": True, **parsed}


def _connector_state() -> dict[str, Any]:
    """Connector credential chain state (method label only — never the token).

    Reuses the skills-hub `GitHubAuth` chain (PAT -> `gh auth token` ->
    GitHub App -> anonymous) so the status agrees with what skills-hub API
    calls would actually send.
    """
    try:
        from tools.skills_hub_github import GitHubAuth

        auth = GitHubAuth()
        return {"method": auth.auth_method(), "authenticated": auth.is_authenticated()}
    except Exception as exc:  # health: allow BLE001 -- any connector failure means "unknown", never fails the probe  # pragma: no cover
        logger.debug("GitHub connector state unavailable: %s", exc)
        return {"method": "unknown", "authenticated": False}


def _git_config_value(key: str) -> Optional[str]:
    """Effective `git config` value in the agent's working directory (or None).

    Plain env on purpose: unlike the `gh` call above this read must see the
    user's real git config, and `git config` never prompts.
    """
    try:
        result = subprocess.run(
            ["git", "config", key],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _verify_credential_live() -> dict[str, Any]:
    """Live health probe: does the connector credential answer api.github.com?

    Returns the API login + scopes on success; on failure only the failure
    kind (never a body or a header value that could carry a credential).
    """
    try:
        from tools.skills_hub_github import GitHubAuth

        auth = GitHubAuth()
        if not auth.is_authenticated():
            return {"checked": False, "ok": False, "reason": "no-credential"}
        import httpx

        resp = httpx.get(
            _API_USER_URL, headers=auth.get_headers(), timeout=_API_TIMEOUT_S
        )
    except ImportError:
        return {"checked": False, "ok": False, "reason": "unverified"}
    except Exception as exc:  # health: allow BLE001 -- any probe failure means "unverified", never fails the status call
        logger.debug("GitHub API health probe failed: %s", exc)
        return {"checked": True, "ok": False, "reason": "unverified"}
    if resp.status_code == 200:
        try:
            login = resp.json().get("login")
        except ValueError:
            login = None
        scopes = [
            s.strip()
            for s in resp.headers.get("x-oauth-scopes", "").split(",")
            if s.strip()
        ]
        return {"checked": True, "ok": True, "login": login, "scopes": scopes}
    if resp.status_code == 401:
        return {"checked": True, "ok": False, "reason": "rejected"}
    if resp.status_code == 429 or (
        resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"
    ):
        return {"checked": True, "ok": False, "reason": "rate-limited"}
    return {"checked": True, "ok": False, "reason": f"http-{resp.status_code}"}


_CREDENTIAL_KIND_BY_METHOD = {
    "gh-cli": "managed-oauth",  # `gh auth login` is an OAuth flow
    "pat": "managed-pat",
    "github-app": "app-installation",
}


def _handle_github_identity_status(args: dict, **kw) -> str:
    """Report effective GitHub identity + credential health (read-only)."""
    gh = _check_gh_cli()
    connector = _connector_state()
    live = _verify_credential_live()
    git_author = {
        "name": _git_config_value("user.name"),
        "email": _git_config_value("user.email"),
    }

    # Effective login: the live API answer wins (it is the credential that
    # actually works); fall back to the `gh` keyring login.
    login = live.get("login") or gh.get("login")
    scopes = live.get("scopes") or gh.get("scopes") or []

    if live.get("ok"):
        credential_state, evidence = "available", "github-api"
    elif live.get("reason") == "rejected":
        credential_state, evidence = "configured_unavailable", "unverified"
    elif live.get("reason") == "rate-limited":
        credential_state, evidence = "rate_limited", "rate-limited"
    elif not gh.get("logged_in") and not connector.get("authenticated"):
        credential_state, evidence = "unavailable", "none"
    else:
        credential_state, evidence = "unverified", "unverified"

    healthy = credential_state == "available"
    method = connector.get("method") or "anonymous"
    source = "system-detected" if gh.get("logged_in") else "system-configured"
    result: dict[str, Any] = {
        "healthy": healthy,
        "effective": {
            "source": source,
            "credentialKind": _CREDENTIAL_KIND_BY_METHOD.get(method, "native"),
            "credentialState": credential_state,
            "account": {"login": login} if login else None,
            "gitAuthor": git_author,
            "evidence": evidence,
            "accessExpiresAtMs": None,
            "refreshState": "not_applicable",
            "oauthScopes": scopes,
            "repositoryGrants": "unknown",
        },
        "ghCli": {
            "available": gh.get("available"),
            "loggedIn": gh.get("logged_in"),
            "hostname": gh.get("hostname"),
            "protocol": gh.get("protocol"),
            "activeAccount": gh.get("active_account"),
        },
        "connector": connector,
        "summary": (
            f"GitHub identity {login} (via {method}): credential healthy."
            if healthy and login
            else "GitHub identity healthy, but no account login could be determined."
            if healthy
            else "No GitHub credential: `gh` is not logged in and no token is configured."
            if credential_state == "unavailable"
            else f"GitHub credential is {credential_state.replace('_', ' ')}"
            + (f" for account {login}." if login else ".")
        ),
    }
    if not healthy:
        result["nextAction"] = (
            "Ask the operator to run `gh auth login` (or set GITHUB_TOKEN / "
            "GH_TOKEN), then check github_identity_status again."
        )
    return json.dumps(result, indent=2)


registry.register(
    name="github_identity_status",
    toolset="github",
    schema=_GITHUB_IDENTITY_STATUS_SCHEMA,
    handler=_handle_github_identity_status,
    emoji="🐙",
)
