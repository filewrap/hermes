"""Tests for tools/github_identity_status_tool.py (F8-thin).

Behaviour contracts: the `gh auth status` parser, the unauthenticated /
healthy shapes returned through real registry dispatch, and the exposure
contract (core list + named `github` toolset). No network, no `gh` binary:
the subprocess/connector probes are stubbed at the module boundary.
"""

import json

import model_tools
import tools.github_identity_status_tool as gh_status
from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

_LOGGED_IN = """github.com
  ✓ Logged in to github.com account filewrap (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_****
  - Token scopes: 'repo', 'read:org', 'workflow'
"""

_NOT_LOGGED_IN = (
    "You are not logged into any GitHub hosts. To log in, run: gh auth login\n"
)


def _dispatch(monkeypatch, *, gh, connector, live, author=("kei", "kei@example.com")):
    monkeypatch.setattr(gh_status, "_check_gh_cli", lambda: gh)
    monkeypatch.setattr(gh_status, "_connector_state", lambda: connector)
    monkeypatch.setattr(gh_status, "_verify_credential_live", lambda: live)
    monkeypatch.setattr(
        gh_status,
        "_git_config_value",
        lambda key: {"user.name": author[0], "user.email": author[1]}.get(key),
    )
    return json.loads(
        model_tools.handle_function_call(
            "github_identity_status",
            {},
            enabled_toolsets=["github"],
            session_id="s",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    )


class TestGhAuthStatusParser:
    def test_logged_in_sample(self):
        parsed = gh_status._parse_gh_auth_status(_LOGGED_IN)
        assert parsed["logged_in"] is True
        assert parsed["login"] == "filewrap"
        assert parsed["hostname"] == "github.com"
        assert parsed["protocol"] == "https"
        assert parsed["active_account"] is True
        assert parsed["scopes"] == ["repo", "read:org", "workflow"]

    def test_not_logged_in_sample(self):
        parsed = gh_status._parse_gh_auth_status(_NOT_LOGGED_IN)
        assert parsed["logged_in"] is False
        assert parsed["login"] is None
        assert parsed["scopes"] == []

    def test_raw_output_never_leaks_through_parser(self):
        # The Token line is parsed for nothing: no token-shaped value survives.
        parsed = gh_status._parse_gh_auth_status(_LOGGED_IN)
        assert "gho_" not in json.dumps(parsed)


class TestGithubIdentityStatusDispatch:
    _NO_GH = {
        "available": True,
        "logged_in": False,
        "hostname": "github.com",
        "login": None,
        "protocol": None,
        "active_account": None,
        "scopes": [],
    }
    _NO_CONNECTOR = {"method": "anonymous", "authenticated": False}
    _NO_LIVE = {"checked": False, "ok": False, "reason": "no-credential"}

    def test_unauthenticated_reports_unavailable(self, monkeypatch):
        result = _dispatch(
            monkeypatch,
            gh=self._NO_GH,
            connector=self._NO_CONNECTOR,
            live=self._NO_LIVE,
        )
        assert result["healthy"] is False
        assert result["effective"]["credentialState"] == "unavailable"
        assert result["effective"]["account"] is None
        assert result["effective"]["evidence"] == "none"
        assert result["effective"]["gitAuthor"] == {
            "name": "kei",
            "email": "kei@example.com",
        }
        assert "nextAction" in result
        assert "gho_" not in json.dumps(result)

    def test_healthy_credential_reports_available(self, monkeypatch):
        gh = {
            "available": True,
            "logged_in": True,
            "hostname": "github.com",
            "login": "filewrap",
            "protocol": "https",
            "active_account": True,
            "scopes": ["repo", "read:org"],
        }
        live = {
            "checked": True,
            "ok": True,
            "login": "filewrap",
            "scopes": ["repo", "read:org"],
        }
        result = _dispatch(
            monkeypatch,
            gh=gh,
            connector={"method": "gh-cli", "authenticated": True},
            live=live,
        )
        assert result["healthy"] is True
        assert result["effective"]["credentialState"] == "available"
        assert result["effective"]["account"] == {"login": "filewrap"}
        assert result["effective"]["credentialKind"] == "managed-oauth"
        assert result["effective"]["refreshState"] == "not_applicable"
        assert "nextAction" not in result

    def test_rejected_credential_reports_configured_unavailable(self, monkeypatch):
        gh = {
            "available": True,
            "logged_in": True,
            "hostname": "github.com",
            "login": "filewrap",
            "protocol": "https",
            "active_account": True,
            "scopes": ["repo"],
        }
        live = {"checked": True, "ok": False, "reason": "rejected"}
        result = _dispatch(
            monkeypatch,
            gh=gh,
            connector={"method": "gh-cli", "authenticated": True},
            live=live,
        )
        assert result["healthy"] is False
        assert result["effective"]["credentialState"] == "configured_unavailable"
        assert result["effective"]["account"] == {"login": "filewrap"}


class TestGithubIdentityStatusExposure:
    def test_tool_is_core_and_resolves_via_github_toolset(self):
        assert "github_identity_status" in _HERMES_CORE_TOOLS
        assert "github_identity_status" in resolve_toolset("github")
