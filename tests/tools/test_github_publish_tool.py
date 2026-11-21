"""Tests for tools/github_publish_tool.py (F8-heavy).

Behaviour contracts: the remote-URL parser, the ruled dirty/busy refusals
(with the ported wording), the binding store, and a full publish cycle
against REAL local git remotes (bare repo + worktree clone: real push,
fetch, merge, head tracking) with only the GitHub REST transport stubbed
at the ``_github_api`` boundary — the sandbox has no GitHub credential
(``gh`` is not logged in), so a real draft PR is the host live check, not
something this file can prove. Exposure contract (core list + ``github``
toolset) mirrors the F8-thin test.
"""

import hashlib
import json
import os
import subprocess
import threading
from urllib.parse import parse_qs, urlparse

import pytest

import model_tools
import tools.github_publish_tool as gh_pub
from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

FAKE_HOME_MARKER = "github-publish-test-home"


def _run(*args, cwd=None, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    result = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True,
        env=merged, check=False, timeout=60,
    )
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result.stdout.strip()


def _is_ancestor(ws, sha):
    """True when ``sha`` is an ancestor of the worktree HEAD (rc 0, no assert)."""
    result = subprocess.run(
        ["git", "-C", str(ws), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result.returncode == 0


def _make_repo(tmp_path, name="ws"):
    """(bare, worktree): a clone with one pushed commit on main, origin = the
    local bare repo. Repo-local git identity is set (the module's hardened
    git env blanks global config, so commits need it in-repo)."""
    bare = tmp_path / "remote.git"
    _run("git", "init", "--bare", "--initial-branch=main", str(bare))
    ws = tmp_path / name
    _run("git", "clone", str(bare), str(ws))
    _run("git", "-C", str(ws), "config", "user.name", "Test User")
    _run("git", "-C", str(ws), "config", "user.email", "test@example.com")
    (ws / "README.md").write_text("hello\n", encoding="utf-8")
    _run("git", "-C", str(ws), "add", "README.md")
    _run("git", "-C", str(ws), "-c", "user.name=Test User",
         "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _run("git", "-C", str(ws), "push", "-u", "origin", "main")
    return bare, ws


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point the binding store + locks at a temp home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    return home


@pytest.fixture
def healthy_identity(monkeypatch):
    monkeypatch.setattr(
        gh_pub, "_current_identity",
        lambda: {"login": "filewrap", "healthy": True, "reason": "ok"},
    )


@pytest.fixture
def github_remote(monkeypatch):
    """Accept the local test remote as a GitHub repo (the URL parser itself
    is unit-tested below with real github.com forms)."""
    monkeypatch.setattr(gh_pub, "_parse_github_remote", lambda url: ("owner", "repo"))


def _dispatch(monkeypatch, args, **stubs):
    for name, fn in stubs.items():
        monkeypatch.setattr(gh_pub, name, fn)
    return json.loads(
        model_tools.handle_function_call(
            "github_publish",
            args,
            enabled_toolsets=["github"],
            session_id="s-test",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    )


class TestParseGithubRemote:
    @pytest.mark.parametrize("url,expected", [
        ("git@github.com:filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
        ("git@github.com:filewrap/hermes-agent", ("filewrap", "hermes-agent")),
        ("ssh://git@github.com/filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
        # The ssh.github.com:443 form the sandbox uses for clones.
        ("ssh://git@ssh.github.com:443/filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
        ("git@ssh.github.com:filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
        ("https://github.com/filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
        ("https://github.com/filewrap/hermes-agent", ("filewrap", "hermes-agent")),
        ("https://user@github.com/filewrap/hermes-agent.git", ("filewrap", "hermes-agent")),
    ])
    def test_github_forms(self, url, expected):
        assert gh_pub._parse_github_remote(url) == expected

    @pytest.mark.parametrize("url", [
        "git@gitlab.com:owner/repo.git",
        "https://gitlab.com/owner/repo.git",
        "/tmp/remote.git",
        "file:///tmp/remote.git",
        "",
        "git@github.com:owner.git",
    ])
    def test_non_github(self, url):
        assert gh_pub._parse_github_remote(url) is None


class TestBindingStore:
    def test_round_trip(self, fake_home):
        assert gh_pub._load_bindings() == {}
        binding = {"repo": "owner/repo", "head_branch": "main", "pr_number": 7}
        assert gh_pub._save_binding("/tmp/ws", binding) is True
        assert gh_pub._load_bindings() == {gh_pub._binding_key("/tmp/ws"): binding}

    def test_corrupt_store_reads_empty(self, fake_home):
        gh_pub._store_path().write_text("{not json", encoding="utf-8")
        assert gh_pub._load_bindings() == {}


class TestReadiness:
    def test_ready(self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        _bare, ws = _make_repo(tmp_path)
        result = _dispatch(monkeypatch, {"action": "readiness", "workspace_path": str(ws)})
        assert result["success"] is True
        assert result["ready"] is True
        assert result["reasons"] == []
        assert result["repo"] == "owner/repo"

    def test_dirty_tree_not_ready(self, monkeypatch, tmp_path, fake_home, healthy_identity,
                                  github_remote):
        _bare, ws = _make_repo(tmp_path)
        (ws / "README.md").write_text("changed\n", encoding="utf-8")
        result = _dispatch(monkeypatch, {"action": "readiness", "workspace_path": str(ws)})
        assert result["success"] is True
        assert result["ready"] is False
        assert any(r.startswith("dirty-tree:") for r in result["reasons"])
        assert result["tree"]["modified"] == 1

    def test_no_identity_not_ready(self, monkeypatch, tmp_path, fake_home, github_remote):
        _bare, ws = _make_repo(tmp_path)
        monkeypatch.setattr(
            gh_pub, "_current_identity",
            lambda: {"login": None, "healthy": False, "reason": "no-credential"},
        )
        result = _dispatch(monkeypatch, {"action": "readiness", "workspace_path": str(ws)})
        assert result["ready"] is False
        assert result["reasons"] == ["identity-unavailable: no-credential"]


class TestRefusals:
    def test_dirty_refusal_is_no_effect(self, monkeypatch, tmp_path, fake_home,
                                        healthy_identity, github_remote):
        _bare, ws = _make_repo(tmp_path)
        (ws / "README.md").write_text("changed\n", encoding="utf-8")
        result = _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        assert result["success"] is False
        assert result["refusal"] == "dirty"
        assert result["effect"] == "none"
        assert "requires clean source without dirty, untracked, staged, or unpublished work" in result["message"]
        assert result["detail"] == "1 modified"
        assert gh_pub._load_bindings() == {}

    def test_untracked_refusal(self, monkeypatch, tmp_path, fake_home, healthy_identity,
                               github_remote):
        _bare, ws = _make_repo(tmp_path)
        (ws / "scratch.txt").write_text("x", encoding="utf-8")
        result = _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        assert result["refusal"] == "dirty"
        assert result["detail"] == "1 untracked"

    def test_unpublished_refusal(self, monkeypatch, tmp_path, fake_home, healthy_identity,
                                 github_remote):
        _bare, ws = _make_repo(tmp_path)
        (ws / "new.txt").write_text("x", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "new.txt")
        _run("git", "-C", str(ws), "-c", "user.name=Test User",
             "-c", "user.email=test@example.com", "commit", "-m", "unpushed")
        result = _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        assert result["refusal"] == "dirty"
        assert "not pushed" in result["detail"]

    def test_busy_refusal_is_no_effect(self, monkeypatch, tmp_path, fake_home,
                                       healthy_identity, github_remote):
        _bare, ws = _make_repo(tmp_path)
        monkeypatch.setattr(
            gh_pub, "_busy",
            lambda session_id, task_id: {
                "children": [{"subagent_id": "sa-1", "goal": "still running"}],
                "processes": [], "notes": [],
            },
        )
        result = _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws)})
        assert result["success"] is False
        assert result["refusal"] == "busy"
        assert result["effect"] == "none"
        assert "no effect" in result["message"]
        assert result["children"] == [{"subagent_id": "sa-1", "goal": "still running"}]
        assert gh_pub._load_bindings() == {}

    def test_unknown_action(self, monkeypatch, fake_home):
        result = _dispatch(monkeypatch, {"action": "bogus"})
        assert result["success"] is False
        assert result["error"] == "unknown-action"


class TestWorkspaceLock:
    def test_second_acquire_fails_fast(self, monkeypatch, tmp_path, fake_home):
        monkeypatch.setattr(gh_pub, "_LOCK_WAIT_S", 0)
        monkeypatch.setattr(gh_pub, "_LOCK_POLL_S", 0.01)
        with gh_pub._workspace_lock("/tmp/ws") as first:
            assert first is True
            with gh_pub._workspace_lock("/tmp/ws") as second:
                assert second is False
        with gh_pub._workspace_lock("/tmp/ws") as third:
            assert third is True


class TestPublishCycle:
    """Prepare -> publish (draft PR) -> newer remote head -> publish again
    (integrates, does not overwrite) -> inspect/edit/review/evidence."""

    def test_full_cycle(self, monkeypatch, tmp_path, fake_home, healthy_identity,
                        github_remote):
        bare, ws = _make_repo(tmp_path)
        # A feature branch, pushed: prepare requires a pushed head branch.
        _run("git", "-C", str(ws), "checkout", "-b", "feature/one")
        (ws / "feature.txt").write_text("feature\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "feature.txt")
        _run("git", "-C", str(ws), "-c", "user.name=Test User",
             "-c", "user.email=test@example.com", "commit", "-m", "feature work")
        _run("git", "-C", str(ws), "push", "-u", "origin", "feature/one")

        api_calls: list[tuple] = []
        pr_state = {"number": 42, "html_url": "https://github.com/owner/repo/pull/42",
                    "title": "Feature one", "body": "", "state": "open", "draft": True,
                    "head": {"sha": ""}, "base": {"sha": ""}, "mergeable": True}

        def fake_api(method, path, payload=None):
            api_calls.append((method, path, payload))
            if method == "GET" and path == "/repos/owner/repo":
                return 200, {"id": 123456}, None
            if method == "POST" and path.endswith("/pulls"):
                assert payload["draft"] is True
                assert payload["head"] == "feature/one"
                assert payload["base"] == "main"
                head_sha = _run("git", "-C", str(ws), "rev-parse", "HEAD")
                pr_state["head"]["sha"] = head_sha
                base_sha = _run("git", "-C", str(ws), "rev-parse", "origin/main")
                pr_state["base"]["sha"] = base_sha
                return 201, dict(pr_state), None
            if method == "GET" and path.endswith("/pulls/42"):
                return 200, dict(pr_state), None
            if method == "PATCH" and path.endswith("/pulls/42"):
                pr_state.update(payload)
                return 200, dict(pr_state), None
            if method == "POST" and path.endswith("/requested_reviewers"):
                return 201, {}, None
            raise AssertionError(f"unexpected API call {method} {path}")

        monkeypatch.setattr(gh_pub, "_github_api", fake_api)

        upload_calls: list[tuple] = []

        def fake_upload(filename, mime, repository_id, data):
            upload_calls.append((filename, mime, repository_id, data))
            return ("https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789012",
                    None, None)

        monkeypatch.setattr(gh_pub, "_upload_asset", fake_upload)

        prepared = _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        assert prepared["success"] is True, prepared
        assert prepared["binding"]["head_branch"] == "feature/one"
        assert prepared["binding"]["base_branch"] == "main"
        assert prepared["binding"]["pr_number"] is None

        published = _dispatch(
            monkeypatch,
            {"action": "publish", "workspace_path": str(ws), "title": "Feature one"},
        )
        assert published["success"] is True, published
        assert published["pr_url"] == "https://github.com/owner/repo/pull/42"
        assert published["pr_number"] == 42
        assert published["already_published"] is False
        assert published["integrated"] is False
        assert ("POST", "/repos/owner/repo/pulls", {
            "title": "Feature one", "head": "feature/one", "base": "main",
            "body": "", "draft": True}) in api_calls

        # A newer remote head lands (another clone pushes to feature/one).
        other = tmp_path / "other"
        _run("git", "clone", str(bare), str(other))
        _run("git", "-C", str(other), "config", "user.name", "Other")
        _run("git", "-C", str(other), "config", "user.email", "other@example.com")
        _run("git", "-C", str(other), "checkout", "feature/one")
        (other / "other.txt").write_text("from elsewhere\n", encoding="utf-8")
        _run("git", "-C", str(other), "add", "other.txt")
        _run("git", "-C", str(other), "-c", "user.name=Other",
             "-c", "user.email=other@example.com", "commit", "-m", "remote head moves")
        _run("git", "-C", str(other), "push", "origin", "feature/one")

        republished = _dispatch(
            monkeypatch,
            {"action": "publish", "workspace_path": str(ws), "title": "Feature one"},
        )
        assert republished["success"] is True, republished
        assert republished["already_published"] is True
        assert republished["integrated"] is True
        # The merge landed locally and was pushed: the worktree now contains it.
        assert (ws / "other.txt").read_text(encoding="utf-8") == "from elsewhere\n"
        assert _run("git", "-C", str(ws), "rev-parse", "HEAD") == _run(
            "git", "-C", str(ws), "rev-parse", "origin/feature/one")
        # No second PR was created.
        assert len([c for c in api_calls if c[0] == "POST" and c[1].endswith("/pulls")]) == 1

        inspected = _dispatch(monkeypatch, {"action": "inspect", "workspace_path": str(ws)})
        assert inspected["success"] is True, inspected
        assert inspected["pr"]["number"] == 42
        assert inspected["pr"]["draft"] is True
        assert inspected["local"]["head_matches_pr"] is False  # fake PR head is stale
        assert inspected["local"]["dirty"] is False

        edited = _dispatch(monkeypatch, {"action": "edit", "workspace_path": str(ws),
                                         "title": "Feature one (updated)"})
        assert edited["success"] is True, edited
        assert edited["title"] == "Feature one (updated)"
        assert ("PATCH", "/repos/owner/repo/pulls/42",
                {"title": "Feature one (updated)"}) in api_calls

        reviewed = _dispatch(monkeypatch, {"action": "review", "workspace_path": str(ws),
                                           "reviewers": ["octocat"]})
        assert reviewed["success"] is True, reviewed
        assert ("POST", "/repos/owner/repo/pulls/42/requested_reviewers",
                {"reviewers": ["octocat"]}) in api_calls

        # Evidence uploads the file bytes and embeds the URL in the PR body
        # (the old PR-comment mapping is gone: no comment is ever posted).
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        (ws / "screenshot.png").write_bytes(png)
        evidenced = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                            "evidence_path": "screenshot.png",
                                            "evidence_sha256": hashlib.sha256(png).hexdigest(),
                                            "evidence_size_bytes": len(png),
                                            "evidence": "test run: green"})
        assert evidenced["success"] is True, evidenced
        assert evidenced["status"] == "uploaded"
        assert evidenced["url"] == "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789012"
        assert evidenced["embedded"] is True
        assert evidenced["sha256"] == hashlib.sha256(png).hexdigest()
        assert evidenced["mime_type"] == "image/png"
        assert upload_calls == [("screenshot.png", "image/png", 123456, png)]
        assert pr_state["body"].count(evidenced["url"]) == 1
        assert "![screenshot.png](https://github.com/user-attachments/assets/" in pr_state["body"]
        assert "test run: green" in pr_state["body"]
        assert not [c for c in api_calls if "/comments" in c[1]]

    def test_publish_adopts_existing_pr_on_422(self, monkeypatch, tmp_path, fake_home,
                                               healthy_identity, github_remote):
        _bare, ws = _make_repo(tmp_path)
        calls: list[tuple] = []

        def fake_api(method, path, payload=None):
            calls.append((method, path))
            if method == "POST" and path.endswith("/pulls"):
                return 422, {"message": "Validation Failed"}, "validation-failed"
            if method == "GET" and path.startswith("/repos/owner/repo/pulls?head="):
                return 200, [{"number": 9, "html_url": "https://github.com/owner/repo/pull/9"}], None
            raise AssertionError(f"unexpected API call {method} {path}")

        monkeypatch.setattr(gh_pub, "_github_api", fake_api)
        _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        published = _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws),
                                            "title": "Adopt me"})
        assert published["success"] is True, published
        assert published["pr_number"] == 9
        assert published["pr_url"] == "https://github.com/owner/repo/pull/9"

    def test_identity_change_aborts_then_refresh_rebinds(
            self, monkeypatch, tmp_path, fake_home, github_remote):
        _bare, ws = _make_repo(tmp_path)
        monkeypatch.setattr(gh_pub, "_current_identity",
                            lambda: {"login": "filewrap", "healthy": True, "reason": "ok"})
        monkeypatch.setattr(gh_pub, "_github_api",
                            lambda m, p, payload=None: (201, {
                                "number": 5,
                                "html_url": "https://github.com/owner/repo/pull/5"}, None))
        _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws),
                                "title": "T"})
        monkeypatch.setattr(gh_pub, "_current_identity",
                            lambda: {"login": "someone-else", "healthy": True, "reason": "ok"})
        republished = _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws)})
        assert republished["success"] is False
        assert republished["error"] == "identity-changed"
        assert "refresh_base" in republished["fix"]
        refreshed = _dispatch(monkeypatch, {"action": "refresh_base",
                                            "workspace_path": str(ws)})
        assert refreshed["success"] is True, refreshed
        assert refreshed["identity_rebound"] is True
        assert refreshed["binding"]["identity_login"] == "someone-else"

    def test_refresh_base_conflict_leaves_tree_clean(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        bare, ws = _make_repo(tmp_path)
        monkeypatch.setattr(gh_pub, "_github_api",
                            lambda m, p, payload=None: (201, {
                                "number": 6,
                                "html_url": "https://github.com/owner/repo/pull/6"}, None))
        _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws), "title": "T"})
        # The remote moves the README line; locally a different change to the
        # same line is committed (not pushed — the push would be rejected).
        other = tmp_path / "other"
        _run("git", "clone", str(bare), str(other))
        _run("git", "-C", str(other), "config", "user.name", "Other")
        _run("git", "-C", str(other), "config", "user.email", "other@example.com")
        (other / "README.md").write_text("theirs\n", encoding="utf-8")
        _run("git", "-C", str(other), "add", "README.md")
        _run("git", "-C", str(other), "-c", "user.name=Other",
             "-c", "user.email=other@example.com", "commit", "-m", "their line")
        _run("git", "-C", str(other), "push", "origin", "main")
        (ws / "README.md").write_text("ours\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "README.md")
        _run("git", "-C", str(ws), "-c", "user.name=Test User",
             "-c", "user.email=test@example.com", "commit", "-m", "our line")
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is False
        assert result["error"] == "merge-conflict"
        # The tree is left clean: the merge was aborted, no MERGE_HEAD remains,
        # and the local commit is intact.
        assert _run("git", "-C", str(ws), "status", "--porcelain") == ""
        assert not (ws / ".git" / "MERGE_HEAD").exists()
        assert (ws / "README.md").read_text(encoding="utf-8") == "ours\n"

    def test_refresh_base_merges_base_not_newer_head(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        bare, ws = _make_repo(tmp_path)
        monkeypatch.setattr(gh_pub, "_github_api",
                            lambda m, p, payload=None: (201, {
                                "number": 6,
                                "html_url": "https://github.com/owner/repo/pull/6"}, None))
        # Work happens on a feature branch against main as base.
        _run("git", "-C", str(ws), "checkout", "-b", "feature")
        (ws / "feature.txt").write_text("feature\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "feature.txt")
        _run("git", "-C", str(ws), "-c", "user.name=Test User",
             "-c", "user.email=test@example.com", "commit", "-m", "feature work")
        _run("git", "-C", str(ws), "push", "-u", "origin", "feature")
        _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
        _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws), "title": "T"})
        # Another writer moves the base (main) first: refresh integrates it.
        other = tmp_path / "other"
        _run("git", "clone", str(bare), str(other))
        _run("git", "-C", str(other), "config", "user.name", "Other")
        _run("git", "-C", str(other), "config", "user.email", "other@example.com")
        (other / "base.txt").write_text("base\n", encoding="utf-8")
        _run("git", "-C", str(other), "add", "base.txt")
        _run("git", "-C", str(other), "-c", "user.name=Other",
             "-c", "user.email=other@example.com", "commit", "-m", "their base")
        _run("git", "-C", str(other), "push", "origin", "main")
        base_sha = _run("git", "-C", str(other), "rev-parse", "HEAD")
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is True, result
        assert result["merged"] == ["main"]
        assert _is_ancestor(ws, base_sha) is True
        # Then the same writer moves the head (feature): refresh must NOT
        # import it — that integration belongs to prepare/publish — so a
        # second refresh merges nothing and the head stays unimported.
        # (Fetch first: the first refresh pushed a merge commit to the head,
        # so the clone's view of origin/feature is stale.)
        _run("git", "-C", str(other), "fetch", "origin")
        _run("git", "-C", str(other), "checkout", "feature")
        (other / "head.txt").write_text("head\n", encoding="utf-8")
        _run("git", "-C", str(other), "add", "head.txt")
        _run("git", "-C", str(other), "-c", "user.name=Other",
             "-c", "user.email=other@example.com", "commit", "-m", "their head")
        _run("git", "-C", str(other), "push", "origin", "feature")
        head_sha = _run("git", "-C", str(other), "rev-parse", "HEAD")
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is True, result
        assert result["merged"] == []
        assert _is_ancestor(ws, head_sha) is False


def _evidence_api(pr_state, calls=None):
    """GitHub REST stub for evidence tests: repo id, PR create/read/patch."""

    def fake_api(method, path, payload=None):
        if calls is not None:
            calls.append((method, path))
        if method == "GET" and path == "/repos/owner/repo":
            return 200, {"id": 123456}, None
        if method == "POST" and path.endswith("/pulls"):
            return 201, {"number": 6, "html_url": "https://github.com/owner/repo/pull/6"}, None
        if method == "GET" and path.endswith("/pulls/6"):
            return 200, dict(pr_state), None
        if method == "PATCH" and path.endswith("/pulls/6"):
            pr_state.update(payload)
            return 200, dict(pr_state), None
        raise AssertionError(f"unexpected API call {method} {path}")

    return fake_api


def _pr_state(body=""):
    return {"number": 6, "html_url": "https://github.com/owner/repo/pull/6",
            "title": "T", "body": body, "state": "open", "draft": True,
            "head": {"sha": ""}, "base": {"sha": ""}, "mergeable": True}


def _bind_workspace(monkeypatch, tmp_path, fake_api):
    """A repo with a bound PR 6 on main; returns the worktree."""
    _bare, ws = _make_repo(tmp_path)
    monkeypatch.setattr(gh_pub, "_github_api", fake_api)
    prepared = _dispatch(monkeypatch, {"action": "prepare", "workspace_path": str(ws)})
    assert prepared["success"] is True, prepared
    published = _dispatch(monkeypatch, {"action": "publish", "workspace_path": str(ws),
                                        "title": "T"})
    assert published["success"] is True, published
    return ws


class TestRefreshBaseDirtyGate:
    """refresh_base refuses only files with BOTH staged and unstaged
    changes (the upstream partiallyStaged) — a dirty tree that touches
    only other files proceeds, and the dirty entries survive untouched."""

    def test_proceeds_with_unrelated_dirty_tree(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        # Three dirty kinds at once, none of them partially staged: an
        # unstaged edit, a staged-only add, and an untracked file.
        (ws / "README.md").write_text("changed\n", encoding="utf-8")
        (ws / "staged.txt").write_text("staged\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "staged.txt")
        (ws / "scratch.txt").write_text("x", encoding="utf-8")
        before = _run("git", "-C", str(ws), "status", "--porcelain")
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is True, result
        assert result["merged"] == []
        assert _run("git", "-C", str(ws), "status", "--porcelain") == before
        assert (ws / "README.md").read_text(encoding="utf-8") == "changed\n"

    def test_proceeds_with_staged_only_changes(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        (ws / "README.md").write_text("staged line\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "README.md")
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is True, result
        assert _run("git", "-C", str(ws), "status", "--porcelain") == "M  README.md"

    def test_refuses_partially_staged_file(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        (ws / "README.md").write_text("staged line\n", encoding="utf-8")
        _run("git", "-C", str(ws), "add", "README.md")
        (ws / "README.md").write_text("staged line\nunstaged line\n", encoding="utf-8")
        assert _run("git", "-C", str(ws), "status", "--porcelain") == "MM README.md"
        result = _dispatch(monkeypatch, {"action": "refresh_base", "workspace_path": str(ws)})
        assert result["success"] is False
        assert result["refusal"] == "dirty"
        assert result["effect"] == "none"
        assert "Commit the staged changes in files that also have unstaged changes" in result["message"]
        assert result["files"] == ["README.md"]
        # Nothing was touched: the staged entry and the unstaged edit remain.
        assert _run("git", "-C", str(ws), "status", "--porcelain") == "MM README.md"
        assert (ws / "README.md").read_text(encoding="utf-8") == "staged line\nunstaged line\n"
        assert "staged line" in _run("git", "-C", str(ws), "diff", "--cached")


class TestEvidence:
    """upload_evidence at the tool level: inspection, verification, and
    embedding. The upload transport itself is proven against a local stub
    server in TestEvidenceUploadTransport; here it is a stubbed boundary."""

    _URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789012"

    def _fake_upload(self, calls):
        def fake_upload(filename, mime, repository_id, data):
            calls.append((filename, mime, repository_id, data))
            return (self._URL, None, None)

        return fake_upload

    def test_sha_mismatch_uploads_nothing(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        (ws / "shot.png").write_bytes(png)

        def boom(*args):
            raise AssertionError("upload must not run after a sha mismatch")

        monkeypatch.setattr(gh_pub, "_upload_asset", boom)
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "shot.png",
                                         "evidence_sha256": "0" * 64,
                                         "evidence_size_bytes": len(png)})
        assert result["success"] is False
        assert result["error"] == "evidence-changed"

    def test_non_media_file_rejected(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        (ws / "notes.txt").write_text("hello\n", encoding="utf-8")

        def boom(*args):
            raise AssertionError("upload must not run for a non-media file")

        monkeypatch.setattr(gh_pub, "_upload_asset", boom)
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "notes.txt"})
        assert result["success"] is False
        assert result["error"] == "evidence-type-unsupported"

    def test_path_escape_rejected(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))

        def boom(*args):
            raise AssertionError("upload must not run for an escaping path")

        monkeypatch.setattr(gh_pub, "_upload_asset", boom)
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "../outside.png"})
        assert result["success"] is False
        assert result["error"] == "evidence-path-invalid"

    def test_oversize_rejected(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        (ws / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024))

        def boom(*args):
            raise AssertionError("upload must not run for an oversize file")

        monkeypatch.setattr(gh_pub, "_upload_asset", boom)
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "big.png"})
        assert result["success"] is False
        assert result["error"] == "evidence-too-large"

    def test_video_embeds_as_link(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        pr_state = _pr_state()
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(pr_state))
        mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 10
        (ws / "clip.mp4").write_bytes(mp4)
        calls: list[tuple] = []
        monkeypatch.setattr(gh_pub, "_upload_asset", self._fake_upload(calls))
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "clip.mp4"})
        assert result["success"] is True, result
        assert result["mime_type"] == "video/mp4"
        assert calls == [("clip.mp4", "video/mp4", 123456, mp4)]
        assert f"[clip.mp4]({self._URL})" in pr_state["body"]
        assert f"![clip.mp4]({self._URL})" not in pr_state["body"]

    def test_unconfirmed_upload_is_not_repeated_blindly(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(_pr_state()))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        (ws / "shot.png").write_bytes(png)
        monkeypatch.setattr(
            gh_pub, "_upload_asset",
            lambda *args: (None, "upload-unconfirmed",
                           "GitHub has not confirmed the attachment."))
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "shot.png"})
        assert result["success"] is False
        assert result["error"] == "upload-unconfirmed"
        assert "do not repeat the upload blindly" in result["fix"].lower()

    def test_url_already_in_body_skips_patch(
            self, monkeypatch, tmp_path, fake_home, healthy_identity, github_remote):
        pr_state = _pr_state(body=f"before\n\n![shot.png]({self._URL})")
        calls: list[tuple] = []
        ws = _bind_workspace(monkeypatch, tmp_path, _evidence_api(pr_state, calls))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        (ws / "shot.png").write_bytes(png)
        monkeypatch.setattr(gh_pub, "_upload_asset", self._fake_upload([]))
        result = _dispatch(monkeypatch, {"action": "evidence", "workspace_path": str(ws),
                                         "evidence_path": "shot.png"})
        assert result["success"] is True, result
        assert result["embedded"] is True
        assert not [c for c in calls if c[0] == "PATCH"]
        assert pr_state["body"].count(self._URL) == 1


class _FakeGitHubAuth:
    """GitHubAuth stand-in: one fixed token, no credential-chain probing."""

    def __init__(self, token="test-token"):
        self._token = token

    def is_authenticated(self):
        return self._token is not None

    def get_headers(self):
        return {"Authorization": f"token {self._token}"} if self._token else {}


def _upload_stub_server(status, payload):
    """A local uploads-host stand-in on 127.0.0.1; returns (server, seen)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": self.rfile.read(length),
            })
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


class TestEvidenceUploadTransport:
    """The upload itself, against a real local HTTP server standing in for
    uploads.github.com: request shape, raw bytes, and URL validation."""

    _URL = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789012"

    def test_posts_bytes_and_validates_url(self, monkeypatch):
        server, seen = _upload_stub_server(201, {"url": self._URL})
        try:
            monkeypatch.setattr(
                gh_pub, "_UPLOADS_BASE", f"http://127.0.0.1:{server.server_address[1]}")
            monkeypatch.setattr("tools.skills_hub_github.GitHubAuth", _FakeGitHubAuth)
            data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
            url, err, detail = gh_pub._upload_asset("shot.png", "image/png", 123456, data)
        finally:
            server.shutdown()
            server.server_close()
        assert err is None, detail
        assert url == self._URL
        assert len(seen) == 1
        request = seen[0]
        assert urlparse(request["path"]).path == "/user-attachments/assets"
        assert parse_qs(urlparse(request["path"]).query) == {
            "name": ["shot.png"], "content_type": ["image/png"],
            "repository_id": ["123456"]}
        assert request["authorization"] == "Bearer test-token"
        assert request["content_type"] == "application/octet-stream"
        assert request["body"] == data

    def test_server_error_is_unconfirmed(self, monkeypatch):
        server, _seen = _upload_stub_server(500, {"message": "boom"})
        try:
            monkeypatch.setattr(
                gh_pub, "_UPLOADS_BASE", f"http://127.0.0.1:{server.server_address[1]}")
            monkeypatch.setattr("tools.skills_hub_github.GitHubAuth", _FakeGitHubAuth)
            url, err, detail = gh_pub._upload_asset("shot.png", "image/png", 123456, b"data")
        finally:
            server.shutdown()
            server.server_close()
        assert url is None
        assert err == "upload-unconfirmed"
        assert "may have been created" in (detail or "")


class TestExposure:
    def test_in_core_and_github_toolset(self):
        assert "github_publish" in _HERMES_CORE_TOOLS
        assert "github_publish" in resolve_toolset("github")
        assert "github_identity_status" in resolve_toolset("github")
