"""GitHub publish (F8-heavy): managed-worktree publisher.

Binds a session's workspace to a draft GitHub PR: ``prepare`` records the
binding (head/base commits), ``publish`` pushes the head branch and creates
the draft PR, ``refresh_base`` integrates the newer remote base instead of
overwriting it (a newer remote head is integrated by ``prepare``/``publish``,
never by ``refresh_base`` — the external agent: "Base refresh preserves the accepted
PR head and never imports another writer's newer PR head"). ``inspect``/``edit``/``review``/``evidence`` read and update
the bound PR; ``readiness`` reports whether the workspace is publishable
without changing anything.

PR-creation transport (traced in the external agent's source — the audit's UNVERIFIED is now closed): the external agent
creates the PR with the ``gh`` CLI, ``gh api --hostname <host> --method
POST repos/{owner}/{repo}/pulls --input -`` with JSON ``{title, body,
head: "{pushOwner}:{branch}", base, draft}``
(``src/gateway/github-publication-executor.ts:641``), and pushes with git
using ``credential.helper=!gh auth git-credential``
(``src/gateway/github-publication-git-transport.ts:583``); the Gateway
holds the ``GH_TOKEN`` in the command env. Hermes deliberately uses the
same REST endpoint and payload shape directly (``POST
/repos/{owner}/{repo}/pulls``, ``draft: true``) via the skills-hub
credential chain (``tools/skills_hub_github.py`` ``GitHubAuth``: PAT ->
``gh auth token`` -> GitHub App -> anonymous), the chain F8-thin verified
for reads: a ``gh`` login is not guaranteed on a Hermes host, and the
chain is the established Hermes credential source. ``gh pr create
--draft`` is used by neither side; ``git push`` uses the repo's own
remote/auth — SSH remotes work under the non-interactive git env (agent
auth + ``BatchMode``); HTTPS remotes need a repo-local credential helper
because the hardening blanks global config
(``hermes_cli._subprocess_compat._GIT_CONFIG_OVERRIDES``).

Traced divergences (ported, wave 5): ``evidence`` uploads the inspected
file bytes to ``uploads.github.com/user-attachments/assets`` and embeds
the returned URL in the PR body (``src/gateway/github-publication-evidence.ts``),
and ``refresh_base`` refuses only files with both staged and unstaged
changes (the external agent's ``partiallyStaged``), not any dirty tree
(``src/gateway/github-publication-workspace-review.ts``).

External-agent ref: ``src/agents/tools/github-publish-tool.ts`` (actions
readiness/inspect/prepare/publish/refresh_base/edit/review/evidence);
server ``src/gateway/server-methods/sessions-github.ts:176`` (identity
re-validation + workspace-exclusion lock). The binding lives in
``<home>/github-publish.json`` (one entry per workspace path) — SessionDB
rows carry no worktree/PR field, so this sidecar is the Hermes equivalent
of the external agent's ``worktree.githubPullRequest`` on the session entry.

Ruled refusals (user, 2026-11-11): ported as-is, never warnings, no force
path. A dirty/untracked/staged/unpublished tree refuses prepare/publish:
"Initial preparation ... requires clean source without dirty, untracked,
staged, or unpublished work". A busy session (running delegate children or
background terminal processes) refuses with a no-effect busy refusal.
Publish order the model must follow: commit, push the head branch,
prepare, publish. ``publish`` never force-pushes: a newer remote head is
merged in (a merge conflict aborts with the tree left clean), a push
rejected as non-fast-forward is reported, never forced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from tools.registry import registry

logger = logging.getLogger("tools.github_publish")

_GIT_TIMEOUT_S = 30
_GIT_NET_TIMEOUT_S = 120
_API_TIMEOUT_S = 30
_API_BASE = "https://api.github.com"
_STORE_NAME = "github-publish.json"
_LOCK_DIR_NAME = "github-publish-locks"
_LOCK_STALE_S = 600
_LOCK_WAIT_S = 10
_LOCK_POLL_S = 0.25

# The external agent's refusal wording (plan §4.3), kept verbatim as the message core.
_DIRTY_REFUSAL = (
    "Initial preparation of this workspace for publication requires clean "
    "source without dirty, untracked, staged, or unpublished work."
)
_BUSY_REFUSAL = (
    "Publish refused while the session is busy: background work, children, "
    "or terminals are still running. This refusal has no effect — nothing "
    "was changed."
)

# The external agent's refresh_base wording (github-publication-base-refresh.ts):
# only files with both staged and unstaged changes block a refresh.
_PARTIALLY_STAGED_REFUSAL = (
    "Commit the staged changes in files that also have unstaged changes "
    "before refreshing this PR; its existing files and index were preserved."
)

# The external agent's upload_evidence limits (github-publication-evidence.ts):
# at most 10 MB, image/video only, raw bytes to the uploads host.
_EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
_UPLOADS_BASE = "https://uploads.github.com"
_UPLOAD_TIMEOUT_S = 60
_EVIDENCE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_EVIDENCE_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ATTACHMENT_URL_RE = re.compile(
    r"^https://github\.com/user-attachments/assets/"
    r"[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$"
)

_GITHUB_SSH_RE = re.compile(
    r"^git@(?:ssh\.)?github\.com:([^/\s]+)/(.+?)(?:\.git)?$"
)
_GITHUB_SSH_URL_RE = re.compile(
    r"^ssh://git@(?:ssh\.)?github\.com(?::\d+)?/([^/\s]+)/(.+?)(?:\.git)?$"
)
_GITHUB_HTTPS_RE = re.compile(
    r"^https://(?:[^@\s]+@)?github\.com/([^/\s]+)/(.+?)(?:\.git)?$"
)


def _ok(**payload) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _fail(error: str, **extra) -> str:
    return json.dumps({"success": False, "error": error, **extra}, ensure_ascii=False)


def _refuse(kind: str, message: str, **extra) -> str:
    """A ruled refusal: a result, not an exception — and it changed nothing."""
    return json.dumps(
        {"success": False, "refusal": kind, "effect": "none", "message": message, **extra},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# git plumbing (non-interactive, fail-closed on filter discovery)
# ---------------------------------------------------------------------------


def _run_git(workspace: str, args: list[str], timeout: int = _GIT_TIMEOUT_S):
    """Run git in *workspace*; ``(returncode, stdout, stderr)``.

    ``returncode`` is None when the call never ran (hardened env unavailable,
    spawn failure, timeout) — callers report that instead of running git with
    ambient filters/hooks. Mirrors ``tools/subagent_worktree.py::_run_git``.
    """
    try:
        from hermes_cli._subprocess_compat import (
            harden_git_argv,
            noninteractive_repo_git_env,
        )

        env = noninteractive_repo_git_env(workspace)
    except Exception as exc:  # health: allow BLE001 -- env-helper failure means "do not run git"
        logger.debug("github_publish: git env failed: %s", exc)
        return None, "", "git environment unavailable"
    if env is None:
        return None, "", "git filter discovery failed; refusing to run git"
    try:
        result = subprocess.run(
            ["git", *harden_git_argv(args)],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("github_publish: git %s failed: %s", args[:1], exc)
        return None, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def _parse_github_remote(url: str) -> Optional[tuple[str, str]]:
    """``(owner, repo)`` for a GitHub remote URL (SSH/HTTPS, incl. the
    ``ssh.github.com:443`` form the sandbox uses), else None. Pure function."""
    url = (url or "").strip()
    for pattern in (_GITHUB_SSH_RE, _GITHUB_SSH_URL_RE, _GITHUB_HTTPS_RE):
        match = pattern.match(url)
        if match:
            return match.group(1), match.group(2)
    return None


# ---------------------------------------------------------------------------
# binding store (per-profile home, resolved at call time)
# ---------------------------------------------------------------------------


def _store_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / _STORE_NAME


def _load_bindings() -> dict[str, Any]:
    """All workspace -> binding rows; a missing/corrupt file reads as empty
    (a corrupt file is logged, never fatal to a publish call)."""
    try:
        raw = _store_path().read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("github_publish: cannot read binding store: %s", exc)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("github_publish: binding store is corrupt; treating as empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save_bindings(bindings: dict[str, Any]) -> bool:
    """Atomic save (temp file + rename); False when the store is unwritable."""
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{_STORE_NAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(bindings, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("github_publish: cannot save binding store: %s", exc)
        return False


def _binding_key(workspace: str) -> str:
    return os.path.realpath(workspace)


# ---------------------------------------------------------------------------
# workspace resolution
# ---------------------------------------------------------------------------


def _recorded_cwd(key: Optional[str]) -> str:
    try:
        from tools.terminal_tool import get_session_cwd

        return (get_session_cwd(key) or "").strip()
    except Exception:  # health: allow BLE001 -- an unreadable cwd record means "no record"
        return ""


def _resolve_workspace(session_id: Optional[str], task_id: Optional[str],
                       workspace_path: Optional[str]):
    """(toplevel, error): the git worktree to publish.

    Explicit path wins; then the session's recorded cwd (gateway session key,
    agent session id, task id), the scoped session cwd, then the process cwd.
    The worktree root (``rev-parse --show-toplevel``) is the workspace — a
    publish operates on the whole tree, never a subdirectory.
    """
    candidates: list[str] = []
    if workspace_path:
        candidates.append(workspace_path)
    try:
        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:  # health: allow BLE001 -- no session env means no key to try
        session_key = ""
    for key in (session_key, session_id, task_id):
        if key and (recorded := _recorded_cwd(key)):
            candidates.append(recorded)
    try:
        from agent.runtime_cwd import scoped_session_cwd

        if scoped := (scoped_session_cwd() or "").strip():
            candidates.append(scoped)
    except Exception as exc:  # health: allow BLE001 -- no scoped cwd means "no record"
        logger.debug("github_publish: no scoped session cwd: %s", exc)
    candidates.append(os.getcwd())
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        rc, out, _err = _run_git(candidate, ["rev-parse", "--show-toplevel"])
        if rc == 0 and out.strip():
            return out.strip(), None
    return None, "not-a-git-repo"


def _repo_of(workspace: str):
    """(owner, repo, remote_url, error) for the workspace's ``origin``."""
    rc, out, err = _run_git(workspace, ["remote", "get-url", "origin"])
    if rc is None:
        return None, None, None, err or "git failed"
    if rc != 0:
        return None, None, None, "no-remote"
    remote_url = out.strip().splitlines()[0] if out.strip() else ""
    parsed = _parse_github_remote(remote_url)
    if not parsed:
        return None, None, remote_url, "not-github-remote"
    return parsed[0], parsed[1], remote_url, None


def _default_base(workspace: str) -> Optional[str]:
    """The remote's default branch: ``ls-remote --symref`` first, the local
    ``origin/HEAD`` pointer as fallback. None when neither answers."""
    rc, out, _err = _run_git(
        workspace, ["ls-remote", "--symref", "origin", "HEAD"], timeout=_GIT_NET_TIMEOUT_S
    )
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("ref: refs/heads/"):
                name = line.split("\t")[0][len("ref: refs/heads/"):]
                if name:
                    return name
    rc, out, _err = _run_git(workspace, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip():
        short = out.strip()
        return short.split("/", 1)[1] if "/" in short else short
    return None


# ---------------------------------------------------------------------------
# identity (re-validation, the external agent's sessions-github.ts:176)
# ---------------------------------------------------------------------------


def _current_identity() -> dict[str, Any]:
    """Live GitHub identity from the F8-thin probe: ``login`` (or None),
    ``healthy`` (the credential answered api.github.com), ``reason``."""
    try:
        from tools.github_identity_status_tool import _verify_credential_live

        live = _verify_credential_live()
    except Exception as exc:  # health: allow BLE001 -- probe failure reads as unverified
        logger.debug("github_publish: identity probe failed: %s", exc)
        return {"login": None, "healthy": False, "reason": "unverified"}
    return {
        "login": live.get("login"),
        "healthy": bool(live.get("ok")),
        "reason": "ok" if live.get("ok") else (live.get("reason") or "unverified"),
    }


# ---------------------------------------------------------------------------
# busy check (ruled refusal: children / background work / terminals)
# ---------------------------------------------------------------------------


def _busy(session_id: Optional[str], task_id: Optional[str]) -> dict[str, Any]:
    """Running work owned by this session: delegate children (by
    ``owner_agent_session_id``) and background terminal processes (by gateway
    session key / spawning task id). Probes degrade to "unscoped" notes when
    the call carries no session context — never to a process-wide refusal
    for another session's children."""
    children: list[dict[str, Any]] = []
    processes: list[dict[str, Any]] = []
    notes: list[str] = []
    if session_id:
        try:
            from tools.delegate_tool_registry import list_active_subagents

            children = [
                {"subagent_id": r.get("subagent_id"), "goal": r.get("goal")}
                for r in list_active_subagents()
                if r.get("owner_agent_session_id") == session_id
            ]
        except Exception as exc:  # health: allow BLE001 -- a failed probe is reported, not fatal
            notes.append(f"children-probe-failed: {exc}")
    else:
        notes.append("children-unscoped: no session id on this call")
    session_key = ""
    try:
        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception as exc:  # health: allow BLE001 -- no session env means no key to probe
        logger.debug("github_publish: no session key for busy check: %s", exc)
    try:
        from tools.process_registry import process_registry

        seen: set[str] = set()
        if session_key:
            for entry in process_registry.list_sessions(session_key=session_key):
                if entry.get("status") == "running" and entry.get("session_id") not in seen:
                    seen.add(str(entry.get("session_id")))
                    processes.append(
                        {"process_id": entry.get("session_id"),
                         "command": entry.get("command"), "pid": entry.get("pid")}
                    )
        if task_id and process_registry.has_active_processes(task_id):
            for entry in process_registry.list_sessions(task_id=task_id):
                if entry.get("status") == "running" and entry.get("session_id") not in seen:
                    seen.add(str(entry.get("session_id")))
                    processes.append(
                        {"process_id": entry.get("session_id"),
                         "command": entry.get("command"), "pid": entry.get("pid")}
                    )
    except Exception as exc:  # health: allow BLE001 -- a failed probe is reported, not fatal
        notes.append(f"processes-probe-failed: {exc}")
    if not session_key and not task_id:
        notes.append("processes-unscoped: no session key or task id on this call")
    return {"children": children, "processes": processes, "notes": notes}


# ---------------------------------------------------------------------------
# tree state
# ---------------------------------------------------------------------------


def _tree_state(workspace: str) -> dict[str, Any]:
    """Branch, head sha, dirty/untracked/staged counts, upstream ahead/behind.
    ``unpublished`` is True when the branch has commits the remote does not
    (ahead > 0, or no upstream at all with commits present)."""
    state: dict[str, Any] = {
        "branch": None, "head_sha": None, "dirty": False,
        "untracked": 0, "staged": 0, "modified": 0,
        # Paths whose index differs from BOTH head and the working file
        # (the external agent's partiallyStaged): the only dirty state refresh_base
        # refuses. Unmerged paths land here too — both status chars are set.
        "partially_staged": [],
        "upstream": None, "ahead": 0, "behind": 0, "unpublished": False,
        "commits": 0, "error": None,
    }
    rc, out, err = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc is None:
        state["error"] = err or "git failed"
        return state
    if rc != 0:
        state["error"] = "no-commits"
        return state
    branch = out.strip()
    if branch == "HEAD":
        # Unborn (no commits yet) or detached: tell them apart by whether
        # HEAD resolves at all.
        rc2, _out2, _err2 = _run_git(workspace, ["rev-parse", "--verify", "--quiet", "HEAD"])
        state["error"] = "detached-head" if rc2 == 0 else "no-commits"
        return state
    state["branch"] = branch
    rc, out, _err = _run_git(workspace, ["rev-parse", "HEAD"])
    if rc == 0:
        state["head_sha"] = out.strip()
    rc, out, _err = _run_git(workspace, ["rev-list", "--count", "HEAD"])
    if rc == 0 and out.strip().isdigit():
        state["commits"] = int(out.strip())
    rc, out, err = _run_git(workspace, ["status", "--porcelain=v1", "--untracked-files=normal"])
    if rc is None:
        state["error"] = err or "git failed"
        return state
    if rc == 0:
        for line in out.splitlines():
            if not line:
                continue
            code = line[:2]
            if code == "??":
                state["untracked"] += 1
            elif code[0] not in " ?":
                state["staged"] += 1
            elif code[1] not in " ?":
                state["modified"] += 1
            if code[0] not in " ?" and code[1] not in " ?":
                path = line[3:]
                if " -> " in path:  # rename: the new path is the live one
                    path = path.split(" -> ", 1)[1]
                if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
                    path = path[1:-1]
                state["partially_staged"].append(path)
            state["dirty"] = True
    rc, out, _err = _run_git(
        workspace, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    )
    if rc == 0 and out.strip():
        state["upstream"] = out.strip()
        rc, out, _err = _run_git(workspace, ["rev-list", "--count", "--left-right", "@{upstream}...HEAD"])
        if rc == 0 and out.strip():
            parts = out.strip().split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                state["behind"], state["ahead"] = int(parts[0]), int(parts[1])
    else:
        state["unpublished"] = state["commits"] > 0
    if state["ahead"] > 0:
        state["unpublished"] = True
    return state


# ---------------------------------------------------------------------------
# GitHub API (REST, via the skills-hub credential chain)
# ---------------------------------------------------------------------------


def _github_api(method: str, path: str, payload: Optional[dict] = None):
    """(status, data, error): one REST call, no retries. 401 rejects the token
    so the chain falls through on the next call; 429 is reported, never
    retried. ``error`` is None on 2xx. No credential -> "no-credential"."""
    try:
        from tools.skills_hub_github import GitHubAuth

        auth = GitHubAuth()
        if not auth.is_authenticated():
            return None, None, "no-credential"
        headers = auth.get_headers()
    except Exception as exc:  # health: allow BLE001 -- auth-chain failure reads as no credential
        logger.debug("github_publish: GitHub auth unavailable: %s", exc)
        return None, None, "no-credential"
    try:
        import httpx

        resp = httpx.request(
            method, f"{_API_BASE}{path}", headers=headers,
            json=payload, timeout=_API_TIMEOUT_S,
        )
    except Exception as exc:  # health: allow BLE001 -- transport failure is an error, not a retry
        logger.debug("github_publish: GitHub API %s %s failed: %s", method, path, exc)
        return None, None, "github-api-unreachable"
    try:
        data = resp.json() if resp.content else None
    except ValueError:
        data = None
    if 200 <= resp.status_code < 300:
        # Data is returned as decoded: a dict for single resources, a list
        # for list endpoints (the publish 422-adopt path reads a list).
        return resp.status_code, data, None
    if resp.status_code == 401:
        token = headers.get("Authorization", "").split(" ", 1)[-1]
        if token:
            auth.reject(token)
        return resp.status_code, data, "credential-rejected"
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        return resp.status_code, data, "rate-limited"
    if resp.status_code == 429:
        return resp.status_code, data, "rate-limited"
    if resp.status_code == 404:
        return resp.status_code, data, "not-found"
    if resp.status_code == 422:
        return resp.status_code, data, "validation-failed"
    return resp.status_code, data, f"http-{resp.status_code}"


# ---------------------------------------------------------------------------
# workspace-exclusion lock (the external agent's sessions-github.ts:176)
# ---------------------------------------------------------------------------


def _lock_path(workspace: str) -> Path:
    from hermes_constants import get_hermes_home

    digest = hashlib.sha1(_binding_key(workspace).encode("utf-8")).hexdigest()[:16]
    return get_hermes_home() / _LOCK_DIR_NAME / f"{digest}.lock"


@contextmanager
def _workspace_lock(workspace: str):
    """One publisher at a time per workspace (file lock, stale after 10 min
    so a crashed holder never wedges publishes). Yields True when held."""
    path = _lock_path(workspace)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("github_publish: cannot create lock dir: %s", exc)
        yield False
        return
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _LOCK_STALE_S:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(_LOCK_POLL_S)
        except OSError as exc:
            logger.warning("github_publish: cannot acquire workspace lock: %s", exc)
            yield False
            return
    try:
        yield True
    finally:
        try:
            if fd is not None:
                os.close(fd)
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# action plumbing
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _session_of(args: dict, kw: dict):
    """(session_id, task_id) for this call, from dispatcher kwargs or args."""
    session_id = kw.get("current_session_id") or kw.get("session_id") or args.get("session_id")
    return (str(session_id) if session_id else None, kw.get("task_id"))


def _binding_public(binding: dict[str, Any]) -> dict[str, Any]:
    return {k: binding.get(k) for k in (
        "session_id", "repo", "remote_url", "base_branch", "head_branch",
        "pr_number", "pr_url", "head_sha", "base_sha", "identity_login", "updated_at",
    )}


def _save_binding(workspace: str, binding: dict[str, Any]) -> bool:
    bindings = _load_bindings()
    bindings[_binding_key(workspace)] = binding
    return _save_bindings(bindings)


def _dirty_detail(state: dict[str, Any]) -> str:
    parts = []
    if state["staged"]:
        parts.append(f"{state['staged']} staged")
    if state["modified"]:
        parts.append(f"{state['modified']} modified")
    if state["untracked"]:
        parts.append(f"{state['untracked']} untracked")
    if state["unpublished"]:
        parts.append(
            f"{state['ahead']} commit(s) not pushed"
            if state["upstream"] else "branch never pushed"
        )
    return ", ".join(parts) or "uncommitted changes"


def _write_preamble(ctx: dict[str, Any], action: str):
    """Identity + repo + tree + busy checks shared by the mutating actions.
    Returns an error/refusal JSON string, or None when the workspace may be
    published. Fills ``ctx`` with identity/repo/tree/busy on success."""
    workspace = ctx["workspace"]
    identity = _current_identity()
    ctx["identity"] = identity
    if not identity["healthy"]:
        return _fail(
            "identity-unavailable", action=action, reason=identity["reason"],
            next_action="Ask the operator to run `gh auth login` (or set "
            "GITHUB_TOKEN / GH_TOKEN), then retry.",
        )
    owner, repo, remote_url, err = _repo_of(workspace)
    if err:
        return _fail(err, action=action, workspace=workspace)
    ctx.update(owner=owner, repo=repo, remote_url=remote_url)
    tree = _tree_state(workspace)
    ctx["tree"] = tree
    if tree.get("error"):
        return _fail(tree["error"], action=action, workspace=workspace)
    if tree["dirty"] or tree["unpublished"]:
        return _refuse(
            "dirty", _DIRTY_REFUSAL, action=action, workspace=workspace,
            detail=_dirty_detail(tree),
            fix="Commit the changes, push the head branch, then retry.",
            tree={k: tree[k] for k in (
                "branch", "dirty", "untracked", "staged", "modified",
                "upstream", "ahead", "behind", "unpublished")},
        )
    busy = _busy(ctx["session_id"], ctx["task_id"])
    ctx["busy"] = busy
    if busy["children"] or busy["processes"]:
        return _refuse(
            "busy", _BUSY_REFUSAL, action=action, workspace=workspace,
            children=busy["children"], processes=busy["processes"],
            fix="Wait for the background work to finish (or stop it), then retry.",
        )
    return None


def _check_identity_match(ctx: dict[str, Any], action: str):
    """The external agent's identity re-validation: a bound workspace whose publisher
    identity changed aborts with "retry refresh_base"."""
    binding = ctx.get("binding") or {}
    bound_login = binding.get("identity_login")
    current_login = (ctx.get("identity") or {}).get("login")
    if bound_login and current_login and bound_login != current_login:
        return _fail(
            "identity-changed", action=action, workspace=ctx["workspace"],
            was=bound_login, now=current_login,
            fix="Run github_publish with action=refresh_base to rebind this "
            "workspace to the current GitHub identity, then retry.",
        )
    return None


def _fetch_origin(workspace: str):
    rc, _out, err = _run_git(workspace, ["fetch", "origin"], timeout=_GIT_NET_TIMEOUT_S)
    if rc is None:
        return err or "git fetch failed"
    if rc != 0:
        return (err.strip().splitlines() or ["git fetch failed"])[-1][:500]
    return None


def _merge_origin_ref(workspace: str, ref: str):
    """Merge ``origin/<ref>`` into the current branch. A conflict aborts the
    merge (the tree is left clean) and reports it — never auto-resolved."""
    rc, _out, err = _run_git(workspace, ["merge", "--no-edit", f"origin/{ref}"])
    if rc is None:
        return err or "git merge failed"
    if rc != 0:
        _run_git(workspace, ["merge", "--abort"])
        detail = (err.strip().splitlines() or ["merge conflict"])[-1][:500]
        return f"merge-conflict with origin/{ref}: {detail} (merge aborted; integrate manually)"
    return None


def _push_head(workspace: str, head_branch: str):
    """Push the head branch. Never force-pushes: a non-fast-forward rejection
    is reported, never overridden."""
    rc, _out, err = _run_git(
        workspace, ["push", "-u", "origin", head_branch], timeout=_GIT_NET_TIMEOUT_S
    )
    if rc is None:
        return err or "git push failed"
    if rc != 0:
        return (err.strip().splitlines() or ["git push failed"])[-1][:500]
    return None


def _rev_parse(workspace: str, ref: str) -> Optional[str]:
    rc, out, _err = _run_git(workspace, ["rev-parse", "--verify", "--quiet", ref])
    return out.strip() if rc == 0 and out.strip() else None


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def _action_readiness(args: dict, kw: dict) -> str:
    """Report whether the workspace is publishable. Read-only: never refuses,
    never mutates — every finding is a reason, not an error."""
    session_id, task_id = _session_of(args, kw)
    workspace, err = _resolve_workspace(session_id, task_id, args.get("workspace_path"))
    if err:
        return _ok(action="readiness", ready=False, reasons=["workspace-unresolvable"],
                   workspace=args.get("workspace_path"))
    identity = _current_identity()
    owner, repo, _remote_url, repo_err = _repo_of(workspace)
    tree = _tree_state(workspace)
    busy = _busy(session_id, task_id)
    reasons: list[str] = []
    if not identity["healthy"]:
        reasons.append(f"identity-unavailable: {identity['reason']}")
    if repo_err:
        reasons.append(repo_err)
    if tree.get("error"):
        reasons.append(tree["error"])
    elif tree["dirty"] or tree["unpublished"]:
        reasons.append(f"dirty-tree: {_dirty_detail(tree)}")
    if busy["children"] or busy["processes"]:
        reasons.append("busy")
    return _ok(
        action="readiness", ready=not reasons, reasons=reasons,
        workspace=workspace, repo=f"{owner}/{repo}" if owner else None,
        identity={"login": identity["login"], "healthy": identity["healthy"]},
        tree={k: tree[k] for k in (
            "branch", "head_sha", "dirty", "untracked", "staged", "modified",
            "upstream", "ahead", "behind", "unpublished")},
        busy=busy,
        binding=_binding_public(_load_bindings().get(_binding_key(workspace), {}))
        or None,
    )


def _action_prepare(args: dict, kw: dict) -> str:
    """Bind the workspace to a (future) PR: record head/base commits. The
    head branch must already exist on the remote (commit + push first) — the
    ruled dirty refusal covers unpublished work."""
    session_id, task_id = _session_of(args, kw)
    workspace, err = _resolve_workspace(session_id, task_id, args.get("workspace_path"))
    if err:
        return _fail(err, action="prepare", workspace=args.get("workspace_path"))
    ctx = {"workspace": workspace, "session_id": session_id, "task_id": task_id}
    with _workspace_lock(workspace) as locked:
        if not locked:
            return _fail("workspace-locked", action="prepare", workspace=workspace,
                         fix="Another publish operation holds this workspace; retry shortly.")
        if (refusal := _write_preamble(ctx, "prepare")) is not None:
            return refusal
        bindings = _load_bindings()
        binding = bindings.get(_binding_key(workspace), {})
        ctx["binding"] = binding
        if (mismatch := _check_identity_match(ctx, "prepare")) is not None:
            return mismatch
        if (fetch_err := _fetch_origin(workspace)) is not None:
            return _fail("fetch-failed", action="prepare", workspace=workspace, detail=fetch_err)
        head_branch = args.get("head_branch") or binding.get("head_branch") or ctx["tree"]["branch"]
        base_branch = args.get("base_branch") or binding.get("base_branch") or _default_base(workspace)
        if not base_branch:
            return _fail("base-unknown", action="prepare", workspace=workspace,
                         fix="Pass base_branch explicitly.")
        if _rev_parse(workspace, f"refs/remotes/origin/{head_branch}") is None:
            return _refuse(
                "dirty", _DIRTY_REFUSAL, action="prepare", workspace=workspace,
                detail="head branch never pushed",
                fix=f"Push the head branch first (`git push -u origin {head_branch}`), then retry.",
            )
        base_sha = _rev_parse(workspace, f"refs/remotes/origin/{base_branch}")
        if base_sha is None:
            return _fail("base-not-on-remote", action="prepare", workspace=workspace,
                         base_branch=base_branch)
        binding.update(
            session_id=session_id, repo=f"{ctx['owner']}/{ctx['repo']}",
            remote_url=ctx["remote_url"], base_branch=base_branch,
            head_branch=head_branch, head_sha=ctx["tree"]["head_sha"],
            base_sha=base_sha, identity_login=ctx["identity"]["login"],
            updated_at=_utcnow(),
        )
        binding.setdefault("pr_number", None)
        binding.setdefault("pr_url", None)
        if not _save_binding(workspace, binding):
            return _fail("binding-store-unwritable", action="prepare", workspace=workspace)
        remote_newer = _rev_parse(workspace, "HEAD") != _rev_parse(
            workspace, f"refs/remotes/origin/{head_branch}")
        return _ok(action="prepare", workspace=workspace, binding=_binding_public(binding),
                   remote_newer=remote_newer,
                   note=("The remote head is newer; publish will integrate it."
                         if remote_newer else None))


def _publish_integrate_and_push(ctx: dict[str, Any], workspace: str, head_branch: str):
    """Fetch, merge a newer remote head, push. Returns ``(integrated,
    head_sha, error_json)`` — ``error_json`` is None on success. Never
    force-pushes; a merge conflict or a rejected push aborts here."""
    if (fetch_err := _fetch_origin(workspace)) is not None:
        return False, None, _fail("fetch-failed", action="publish", workspace=workspace,
                                  detail=fetch_err)
    integrated = False
    if ctx["tree"]["behind"] or _rev_parse(workspace, "HEAD") != _rev_parse(
            workspace, f"refs/remotes/origin/{head_branch}"):
        before_merge = _rev_parse(workspace, "HEAD")
        if (merge_err := _merge_origin_ref(workspace, head_branch)) is not None:
            return False, None, _fail("merge-conflict", action="publish", workspace=workspace,
                                      detail=merge_err)
        integrated = before_merge != _rev_parse(workspace, "HEAD")
    tree = _tree_state(workspace)
    if tree.get("error"):
        return False, None, _fail(tree["error"], action="publish", workspace=workspace)
    if tree["ahead"] > 0 or integrated:
        if (push_err := _push_head(workspace, head_branch)) is not None:
            binding = ctx["binding"]
            binding.update(head_sha=tree["head_sha"], updated_at=_utcnow())
            _save_binding(workspace, binding)
            return False, None, _fail("push-failed", action="publish", workspace=workspace,
                                      detail=push_err)
        tree = _tree_state(workspace)
        if tree.get("error"):
            return False, None, _fail(tree["error"], action="publish", workspace=workspace)
    return integrated, tree.get("head_sha"), None


def _publish_update_existing(ctx: dict[str, Any], workspace: str, args: dict,
                             head_sha: Optional[str], base_sha: str, integrated: bool) -> str:
    """The workspace is already bound to a PR: refresh its head/base and
    apply a title/body patch when one was passed."""
    owner, repo = ctx["owner"], ctx["repo"]
    binding = ctx["binding"]
    pr_number = binding["pr_number"]
    _status, pr, api_err = _github_api("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
    if api_err:
        return _fail(api_err, action="publish", workspace=workspace, pr_number=pr_number)
    updated = False
    patch: dict[str, Any] = {}
    if args.get("title") and args["title"] != pr.get("title"):
        patch["title"] = args["title"]
    if args.get("body") is not None and args["body"] != pr.get("body"):
        patch["body"] = args["body"]
    if patch:
        _status, pr, api_err = _github_api(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", patch)
        if api_err:
            return _fail(api_err, action="publish", workspace=workspace, pr_number=pr_number)
        updated = True
    binding.update(head_sha=head_sha, base_sha=base_sha, updated_at=_utcnow())
    _save_binding(workspace, binding)
    return _ok(action="publish", workspace=workspace, pr_url=binding.get("pr_url"),
               pr_number=pr_number, head_sha=head_sha, base_sha=base_sha,
               integrated=integrated, updated=updated, already_published=True)


def _publish_create_pr(ctx: dict[str, Any], workspace: str, args: dict, session_id: Optional[str],
                       head_branch: str, base_branch: str, head_sha: Optional[str],
                       base_sha: str, integrated: bool) -> str:
    """Create the draft PR and bind it. A 422 (a PR already exists for this
    head branch) adopts the existing open PR instead of failing."""
    owner, repo = ctx["owner"], ctx["repo"]
    binding = ctx["binding"]
    title = args.get("title")
    if not title:
        rc, out, _err = _run_git(workspace, ["log", "-1", "--format=%s"])
        title = out.strip() if rc == 0 and out.strip() else f"Publish {head_branch}"
    _status, pr, api_err = _github_api("POST", f"/repos/{owner}/{repo}/pulls", {
        "title": title, "head": head_branch, "base": base_branch,
        "body": args.get("body") or "", "draft": True,
    })
    if api_err == "validation-failed":
        _status, found, find_err = _github_api(
            "GET", f"/repos/{owner}/{repo}/pulls?head={ctx['identity']['login']}:"
            f"{head_branch}&state=open")
        if not find_err and isinstance(found, list) and found:
            pr = found[0]
            api_err = None
    if api_err:
        return _fail(api_err, action="publish", workspace=workspace,
                     detail=(pr.get("message") if isinstance(pr, dict) else None))
    binding.update(
        session_id=session_id, repo=f"{owner}/{repo}", remote_url=ctx["remote_url"],
        base_branch=base_branch, head_branch=head_branch, head_sha=head_sha,
        base_sha=base_sha, identity_login=ctx["identity"]["login"],
        pr_number=pr.get("number"), pr_url=pr.get("html_url"), updated_at=_utcnow(),
    )
    if not _save_binding(workspace, binding):
        return _fail("binding-store-unwritable", action="publish", workspace=workspace)
    return _ok(action="publish", workspace=workspace, pr_url=binding.get("pr_url"),
               pr_number=binding.get("pr_number"), head_sha=head_sha,
               base_sha=base_sha, integrated=integrated, already_published=False)


def _action_publish(args: dict, kw: dict) -> str:
    """Push the head branch and create (or update) the draft PR. A newer
    remote head is merged in first; a merge conflict or a rejected push
    aborts without force-pushing. An existing PR for the head branch is
    adopted instead of failing on the duplicate."""
    session_id, task_id = _session_of(args, kw)
    workspace, err = _resolve_workspace(session_id, task_id, args.get("workspace_path"))
    if err:
        return _fail(err, action="publish", workspace=args.get("workspace_path"))
    ctx = {"workspace": workspace, "session_id": session_id, "task_id": task_id}
    with _workspace_lock(workspace) as locked:
        if not locked:
            return _fail("workspace-locked", action="publish", workspace=workspace,
                         fix="Another publish operation holds this workspace; retry shortly.")
        if (refusal := _write_preamble(ctx, "publish")) is not None:
            return refusal
        bindings = _load_bindings()
        binding = bindings.get(_binding_key(workspace), {})
        ctx["binding"] = binding
        if (mismatch := _check_identity_match(ctx, "publish")) is not None:
            return mismatch
        head_branch = args.get("head_branch") or binding.get("head_branch") or ctx["tree"]["branch"]
        base_branch = args.get("base_branch") or binding.get("base_branch") or _default_base(workspace)
        if not base_branch:
            return _fail("base-unknown", action="publish", workspace=workspace,
                         fix="Pass base_branch explicitly.")
        if _rev_parse(workspace, f"refs/remotes/origin/{head_branch}") is None:
            return _refuse(
                "dirty", _DIRTY_REFUSAL, action="publish", workspace=workspace,
                detail="head branch never pushed",
                fix=f"Push the head branch first (`git push -u origin {head_branch}`), then retry.",
            )
        integrated, head_sha, err_json = _publish_integrate_and_push(ctx, workspace, head_branch)
        if err_json is not None:
            return err_json
        base_sha = _rev_parse(workspace, f"refs/remotes/origin/{base_branch}")
        if base_sha is None:
            return _fail("base-not-on-remote", action="publish", workspace=workspace,
                         base_branch=base_branch)
        if binding.get("pr_number"):
            return _publish_update_existing(ctx, workspace, args, head_sha, base_sha, integrated)
        return _publish_create_pr(ctx, workspace, args, session_id, head_branch,
                                  base_branch, head_sha, base_sha, integrated)


def _bound_ctx(args: dict, kw: dict, action: str):
    """(ctx, error_json) for the read/update actions: resolve the workspace
    and load its binding; the workspace must already be bound."""
    session_id, task_id = _session_of(args, kw)
    workspace, err = _resolve_workspace(session_id, task_id, args.get("workspace_path"))
    if err:
        return None, _fail(err, action=action, workspace=args.get("workspace_path"))
    binding = _load_bindings().get(_binding_key(workspace))
    if not binding or not binding.get("pr_number"):
        return None, _fail("not-bound", action=action, workspace=workspace,
                           fix="Run github_publish with action=prepare, then publish, first.")
    repo = binding.get("repo") or ""
    if "/" not in repo:
        return None, _fail("not-bound", action=action, workspace=workspace,
                           fix="The stored binding has no repository; run prepare again.")
    owner, repo_name = repo.split("/", 1)
    return {
        "workspace": workspace, "session_id": session_id, "task_id": task_id,
        "binding": binding, "owner": owner, "repo": repo_name,
    }, None


def _action_inspect(args: dict, kw: dict) -> str:
    """Read-only: the bound PR from the API plus local branch state."""
    ctx, err = _bound_ctx(args, kw, "inspect")
    if err:
        return err
    owner, repo, binding = ctx["owner"], ctx["repo"], ctx["binding"]
    _status, pr, api_err = _github_api("GET", f"/repos/{owner}/{repo}/pulls/{binding['pr_number']}")
    if api_err:
        return _fail(api_err, action="inspect", workspace=ctx["workspace"],
                     pr_number=binding["pr_number"])
    tree = _tree_state(ctx["workspace"])
    return _ok(action="inspect", workspace=ctx["workspace"],
               binding=_binding_public(binding),
               pr={"number": pr.get("number"), "url": pr.get("html_url"),
                   "state": pr.get("state"), "draft": pr.get("draft"),
                   "title": pr.get("title"), "head_sha": (pr.get("head") or {}).get("sha"),
                   "base_sha": (pr.get("base") or {}).get("sha"),
                   "mergeable": pr.get("mergeable")},
               local={"branch": tree.get("branch"), "head_sha": tree.get("head_sha"),
                      "dirty": tree.get("dirty"), "ahead": tree.get("ahead"),
                      "behind": tree.get("behind"),
                      "head_matches_pr": tree.get("head_sha") == (pr.get("head") or {}).get("sha")})


def _action_refresh_base(args: dict, kw: dict) -> str:
    """Fetch and integrate the newer remote base into the bound branch
    (merge, never rebase/force-push), then push. A newer remote head is
    NOT merged here — the external agent: base refresh "never imports another
    writer's newer PR head"; that integration belongs to prepare/publish.
    Also the remedy for an identity change: it rebinds to the current
    identity instead of aborting."""
    session_id, task_id = _session_of(args, kw)
    workspace, err = _resolve_workspace(session_id, task_id, args.get("workspace_path"))
    if err:
        return _fail(err, action="refresh_base", workspace=args.get("workspace_path"))
    ctx = {"workspace": workspace, "session_id": session_id, "task_id": task_id}
    with _workspace_lock(workspace) as locked:
        if not locked:
            return _fail("workspace-locked", action="refresh_base", workspace=workspace,
                         fix="Another publish operation holds this workspace; retry shortly.")
        identity = _current_identity()
        if not identity["healthy"]:
            return _fail("identity-unavailable", action="refresh_base",
                         reason=identity["reason"],
                         next_action="Ask the operator to run `gh auth login` (or set "
                         "GITHUB_TOKEN / GH_TOKEN), then retry.")
        ctx["identity"] = identity
        bindings = _load_bindings()
        binding = bindings.get(_binding_key(workspace))
        if not binding:
            return _fail("not-bound", action="refresh_base", workspace=workspace,
                         fix="Run github_publish with action=prepare first.")
        ctx["binding"] = binding
        tree = _tree_state(workspace)
        if tree.get("error"):
            return _fail(tree["error"], action="refresh_base", workspace=workspace)
        # The external agent's rule: a refresh preserves local edits and refuses only
        # files with BOTH staged and unstaged changes — a dirty tree that
        # touches only other files (unstaged-only, staged-only, untracked)
        # proceeds; a merge conflict still aborts cleanly below.
        partially_staged = tree.get("partially_staged") or []
        if partially_staged:
            return _refuse("dirty", _PARTIALLY_STAGED_REFUSAL, action="refresh_base",
                           workspace=workspace,
                           detail=f"{len(partially_staged)} file(s) with staged and "
                           f"unstaged changes: {', '.join(partially_staged)}",
                           files=partially_staged,
                           fix="Commit the staged changes in those files, then retry.")
        busy = _busy(session_id, task_id)
        if busy["children"] or busy["processes"]:
            return _refuse("busy", _BUSY_REFUSAL, action="refresh_base",
                           workspace=workspace, children=busy["children"],
                           processes=busy["processes"],
                           fix="Wait for the background work to finish (or stop it), then retry.")
        if (fetch_err := _fetch_origin(workspace)) is not None:
            return _fail("fetch-failed", action="refresh_base", workspace=workspace,
                         detail=fetch_err)
        base_branch = args.get("base_branch") or binding.get("base_branch")
        head_branch = binding.get("head_branch") or tree.get("branch")
        merged: list[str] = []
        identity_rebound = False
        if binding.get("identity_login") and identity["login"] \
                and binding["identity_login"] != identity["login"]:
            binding["identity_login"] = identity["login"]
            identity_rebound = True
        # Base only: the external agent's refresh_base "never imports another writer's
        # newer PR head" — that integration belongs to prepare/publish.
        for ref in (base_branch,):
            if not ref:
                continue
            remote_sha = _rev_parse(workspace, f"refs/remotes/origin/{ref}")
            if remote_sha is None or remote_sha == _rev_parse(workspace, "HEAD"):
                continue
            rc, _out, _err = _run_git(
                workspace, ["merge-base", "--is-ancestor", f"origin/{ref}", "HEAD"])
            if rc == 0:
                continue
            if (merge_err := _merge_origin_ref(workspace, ref)) is not None:
                return _fail("merge-conflict", action="refresh_base", workspace=workspace,
                             detail=merge_err)
            merged.append(ref)
        tree = _tree_state(workspace)
        pushed = False
        if tree.get("ahead", 0) > 0:
            if (push_err := _push_head(workspace, head_branch)) is not None:
                binding.update(head_sha=tree.get("head_sha"), updated_at=_utcnow())
                _save_binding(workspace, binding)
                return _fail("push-failed", action="refresh_base", workspace=workspace,
                             detail=push_err)
            pushed = True
        binding.update(
            head_sha=tree.get("head_sha"),
            base_sha=_rev_parse(workspace, f"refs/remotes/origin/{base_branch}")
            if base_branch else binding.get("base_sha"),
            updated_at=_utcnow(),
        )
        if not _save_binding(workspace, binding):
            return _fail("binding-store-unwritable", action="refresh_base", workspace=workspace)
        return _ok(action="refresh_base", workspace=workspace,
                   binding=_binding_public(binding), merged=merged, pushed=pushed,
                   identity_rebound=identity_rebound)


def _action_edit(args: dict, kw: dict) -> str:
    """PATCH the bound PR's title/body."""
    ctx, err = _bound_ctx(args, kw, "edit")
    if err:
        return err
    patch: dict[str, Any] = {}
    if args.get("title"):
        patch["title"] = args["title"]
    if args.get("body") is not None:
        patch["body"] = args["body"]
    if not patch:
        return _fail("missing-title-or-body", action="edit", workspace=ctx["workspace"])
    _status, pr, api_err = _github_api(
        "PATCH", f"/repos/{ctx['owner']}/{ctx['repo']}/pulls/{ctx['binding']['pr_number']}", patch)
    if api_err:
        return _fail(api_err, action="edit", workspace=ctx["workspace"],
                     pr_number=ctx["binding"]["pr_number"])
    return _ok(action="edit", workspace=ctx["workspace"], pr_url=pr.get("html_url"),
               pr_number=pr.get("number"), title=pr.get("title"))


def _action_review(args: dict, kw: dict) -> str:
    """Request reviewers on the bound PR (the external agent's request_review)."""
    ctx, err = _bound_ctx(args, kw, "review")
    if err:
        return err
    reviewers = [r for r in (args.get("reviewers") or []) if isinstance(r, str) and r.strip()]
    if not reviewers:
        return _fail("missing-reviewers", action="review", workspace=ctx["workspace"])
    _status, _data, api_err = _github_api(
        "POST", f"/repos/{ctx['owner']}/{ctx['repo']}/pulls/{ctx['binding']['pr_number']}/requested_reviewers",
        {"reviewers": reviewers})
    if api_err:
        return _fail(api_err, action="review", workspace=ctx["workspace"],
                     pr_number=ctx["binding"]["pr_number"])
    return _ok(action="review", workspace=ctx["workspace"],
               pr_number=ctx["binding"]["pr_number"], reviewers=reviewers)


# ---------------------------------------------------------------------------
# evidence upload (the external agent's upload_evidence)
# ---------------------------------------------------------------------------


def _evidence_media_type(filename: str) -> Optional[str]:
    """MIME from the extension, or None when the type is not uploadable."""
    return _EVIDENCE_MEDIA_TYPES.get(Path(filename).suffix.lower())


def _evidence_sniffed_mime(data: bytes) -> Optional[str]:
    """MIME from magic bytes (the external agent's detectMime on the same bytes)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"  # ftyp container; see _evidence_bytes_match_mime
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


def _evidence_bytes_match_mime(data: bytes, mime: str) -> bool:
    """True when the bytes are the claimed image/video type. MP4 and MOV
    share the ftyp container, so the sniffer cannot tell them apart and
    either video type accepts ftyp bytes."""
    sniffed = _evidence_sniffed_mime(data)
    if sniffed is None:
        return False
    if sniffed == mime:
        return True
    return {sniffed, mime} <= {"video/mp4", "video/quicktime"}


def _resolve_evidence_path(workspace: str, rel: str):
    """(path, error): the workspace file for a workspace-relative evidence
    path, or None + an error string. Mirrors the external agent's path checks: no
    absolute paths, no ``..`` escapes, no drive/UNC separators."""
    rel = (rel or "").strip()
    if not rel or rel.startswith("~") or rel.startswith("/"):
        return None, "evidence-path-invalid"
    if "\\" in rel or ":" in rel or any(ord(c) < 32 for c in rel):
        return None, "evidence-path-invalid"
    if any(not part or part in (".", "..") for part in rel.split("/")):
        return None, "evidence-path-invalid"
    root = Path(workspace).resolve()
    resolved = (root / rel).resolve()
    if resolved != root and root not in resolved.parents:
        return None, "evidence-path-invalid"
    return resolved, None


def _upload_asset(filename: str, mime: str, repository_id: int, data: bytes):
    """(url, error, detail): POST the bytes to the uploads host.

    The external agent's upload_evidence transport: ``POST
    uploads.github.com/user-attachments/assets?name=..&content_type=..&repository_id=..``
    with ``Authorization: Bearer <token>`` and an octet-stream body; the
    reply JSON carries the attachment URL, validated against the
    ``github.com/user-attachments/assets/<uuid>`` shape. After the request
    starts, any failure is "unconfirmed" — the attachment may exist, so the
    upload is never repeated blindly. A failure to start is "not-attempted".
    """
    import httpx

    try:
        from tools.skills_hub_github import GitHubAuth

        auth = GitHubAuth()
        if not auth.is_authenticated():
            return None, "no-credential", None
        token = auth.get_headers().get("Authorization", "").split(" ", 1)[-1]
    except Exception as exc:  # health: allow BLE001 -- auth-chain failure reads as no credential
        logger.debug("github_publish: evidence upload auth unavailable: %s", exc)
        return None, "no-credential", None
    if not token or any(c.isspace() for c in token):
        return None, "no-credential", None
    query = urlencode(
        {"name": filename, "content_type": mime, "repository_id": str(repository_id)}
    )
    try:
        resp = httpx.request(
            "POST", f"{_UPLOADS_BASE}/user-attachments/assets?{query}",
            headers={"Accept": "application/json",
                     "Content-Type": "application/octet-stream",
                     "Authorization": f"Bearer {token}"},
            content=data, timeout=_UPLOAD_TIMEOUT_S,
        )
    except Exception as exc:  # health: allow BLE001 -- transport failure is an error, not a retry
        logger.debug("github_publish: evidence upload failed: %s", exc)
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return None, "upload-not-attempted", str(exc)[:200]
        return None, "upload-unconfirmed", (
            "GitHub has not confirmed the attachment. It may have been created; "
            "do not repeat the upload automatically. Reconcile the original upload "
            "before retrying.")
    if resp.status_code not in (200, 201):
        return None, "upload-unconfirmed", (
            "GitHub has not confirmed the attachment. It may have been created; "
            "do not repeat the upload automatically. Reconcile the original upload "
            "before retrying.")
    try:
        result = resp.json() if resp.content else None
    except ValueError:
        result = None
    if not isinstance(result, dict) or not _ATTACHMENT_URL_RE.match(str(result.get("url") or "")):
        return None, "upload-unconfirmed", "Upload returned no safe attachment reference."
    return str(result["url"]), None, None


def _embed_evidence_url(ctx: dict[str, Any], url: str, filename: str,
                        mime: str, caption: str, evidence: dict[str, Any]) -> str:
    """PATCH the bound PR's body to embed an already-uploaded *url*. A
    failure here still reports the URL: the file uploaded, so the caller
    can add it with action=edit instead of uploading again."""
    pr_number = ctx["binding"]["pr_number"]
    _status, pr, api_err = _github_api(
        "GET", f"/repos/{ctx['owner']}/{ctx['repo']}/pulls/{pr_number}")
    if api_err:
        return _fail(api_err, action="evidence", workspace=ctx["workspace"],
                     pr_number=pr_number, url=url, status="uploaded",
                     fix="The file uploaded; use action=edit to add the URL to the PR body.")
    body = (pr or {}).get("body") or ""
    if url in body:
        return _ok(action="evidence", workspace=ctx["workspace"],
                   pr_number=pr_number, status="uploaded", url=url,
                   public_access="unverified", embedded=True,
                   note="The URL was already in the PR body; nothing was patched.",
                   **evidence)
    embed = f"![{filename}]({url})" if mime.startswith("image/") else f"[{filename}]({url})"
    block = embed if not caption else f"{embed}\n\n{caption}"
    new_body = f"{body.rstrip()}\n\n{block}" if body.strip() else block
    _status, _pr, api_err = _github_api(
        "PATCH", f"/repos/{ctx['owner']}/{ctx['repo']}/pulls/{pr_number}",
        {"body": new_body})
    if api_err:
        return _fail(api_err, action="evidence", workspace=ctx["workspace"],
                     pr_number=pr_number, url=url, status="uploaded",
                     fix="The file uploaded; use action=edit to add the URL to the PR body.")
    return _ok(action="evidence", workspace=ctx["workspace"],
               pr_number=pr_number, status="uploaded", url=url,
               public_access="unverified", embedded=True, **evidence)


def _action_evidence(args: dict, kw: dict) -> str:
    """Upload a workspace file as PR evidence (the external agent's upload_evidence)
    and embed the returned URL in the bound PR's body.

    Only the inspected bytes are sent: the caller passes the
    workspace-relative path plus the inspected SHA-256 and byte count when
    it has them, and the bytes are verified against both before any upload.
    The old PR-comment mapping is gone — a text-only call has no file to
    upload. An unconfirmed upload is never repeated blindly: the result
    says so, and the URL (when known) is returned for a manual edit."""
    ctx, err = _bound_ctx(args, kw, "evidence")
    if err:
        return err
    rel = args.get("evidence_path")
    if not isinstance(rel, str) or not rel.strip():
        return _fail("missing-evidence-path", action="evidence", workspace=ctx["workspace"],
                     fix="Pass evidence_path (a workspace-relative image/video path).")
    path, path_err = _resolve_evidence_path(ctx["workspace"], rel)
    if path_err:
        return _fail(path_err, action="evidence", workspace=ctx["workspace"])
    filename = path.name
    mime = _evidence_media_type(filename)
    if not mime:
        return _fail("evidence-type-unsupported", action="evidence", workspace=ctx["workspace"],
                     fix="Evidence must be a PNG, JPEG, GIF, WebP, MP4, MOV or WebM image/video.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _fail("evidence-unreadable", action="evidence", workspace=ctx["workspace"],
                     detail=str(exc)[:200])
    if size < 1:
        return _fail("evidence-size-invalid", action="evidence", workspace=ctx["workspace"],
                     detail="Evidence must be at least 1 byte.")
    if size > _EVIDENCE_MAX_BYTES:
        return _fail("evidence-too-large", action="evidence", workspace=ctx["workspace"],
                     detail=f"{size} bytes; at most {_EVIDENCE_MAX_BYTES} bytes.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return _fail("evidence-unreadable", action="evidence", workspace=ctx["workspace"],
                     detail=str(exc)[:200])
    expected_sha = args.get("evidence_sha256")
    if expected_sha is not None:
        if not isinstance(expected_sha, str) or not _EVIDENCE_SHA256_RE.match(expected_sha):
            return _fail("evidence-sha256-invalid", action="evidence",
                         workspace=ctx["workspace"],
                         fix="Pass the inspected lowercase SHA-256 of the file.")
    expected_size = args.get("evidence_size_bytes")
    if expected_size is not None and (
            not isinstance(expected_size, int) or isinstance(expected_size, bool)):
        return _fail("evidence-size-invalid", action="evidence", workspace=ctx["workspace"],
                     fix="Pass the inspected byte count of the file.")
    actual_sha = hashlib.sha256(data).hexdigest()
    if len(data) != size:
        return _fail("evidence-changed", action="evidence", workspace=ctx["workspace"],
                     detail="Evidence bytes changed since inspection; no upload was attempted.")
    if (expected_sha is not None and actual_sha != expected_sha) or (
            expected_size is not None and size != expected_size):
        return _fail("evidence-changed", action="evidence", workspace=ctx["workspace"],
                     detail="Evidence bytes changed since inspection; no upload was attempted.")
    if not _evidence_bytes_match_mime(data, mime):
        return _fail("evidence-type-mismatch", action="evidence", workspace=ctx["workspace"],
                     detail="Evidence bytes do not match the image/video filename; "
                     "no upload was attempted.")
    _status, repo, api_err = _github_api("GET", f"/repos/{ctx['owner']}/{ctx['repo']}")
    repo_id = (repo or {}).get("id") if isinstance(repo, dict) else None
    if api_err or not isinstance(repo_id, int) or isinstance(repo_id, bool) or repo_id <= 0:
        return _fail(api_err or "repo-unverifiable", action="evidence",
                     workspace=ctx["workspace"], pr_number=ctx["binding"]["pr_number"],
                     detail="The published PR repository could not be verified; "
                     "no upload was attempted.")
    url, upload_err, upload_detail = _upload_asset(filename, mime, repo_id, data)
    if upload_err:
        payload = {"action": "evidence", "workspace": ctx["workspace"],
                   "pr_number": ctx["binding"]["pr_number"], "detail": upload_detail}
        if upload_err == "upload-unconfirmed":
            payload["fix"] = ("Reconcile the original upload before retrying; do not "
                              "repeat the upload blindly.")
        return _fail(upload_err, **payload)
    return _embed_evidence_url(
        ctx, url, filename, mime, (args.get("evidence") or "").strip(),
        {"sha256": actual_sha, "size_bytes": size, "mime_type": mime})


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_ACTIONS = {
    "readiness": _action_readiness,
    "prepare": _action_prepare,
    "publish": _action_publish,
    "inspect": _action_inspect,
    "refresh_base": _action_refresh_base,
    "edit": _action_edit,
    "review": _action_review,
    "evidence": _action_evidence,
}

_GITHUB_PUBLISH_SCHEMA = {
    "name": "github_publish",
    "description": (
        "Publish a session workspace as a draft GitHub PR. "
        "Manage the bound PR afterwards. Order: commit the work, push the head branch, "
        "then action=prepare, then action=publish (creates the draft PR and "
        "returns its URL). A second publish after a newer remote head merges "
        "that head in instead of overwriting it. Actions: readiness (report "
        "publishability, changes nothing), prepare (bind workspace to a PR), "
        "publish (push + create/update the draft PR), inspect (bound PR + "
        "local state), refresh_base (merge newer remote base, rebind "
        "after an identity change; refuses only files with both staged "
        "and unstaged changes), edit (PR title/body), review (request "
        "reviewers), evidence (upload a workspace image/video as PR "
        "evidence and embed its URL in the PR body). Prepare/publish "
        "refuse with no effect on a dirty/untracked/staged/unpublished "
        "tree and while the session has running children or background "
        "processes; never force-pushes. Needs a GitHub credential (gh "
        "login or GITHUB_TOKEN) for the PR itself; the push uses the "
        "repo's own remote auth."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which publish step to run.",
            },
            "session_id": {
                "type": "string",
                "description": "Session whose workspace to publish; defaults to the current session.",
            },
            "workspace_path": {
                "type": "string",
                "description": "Explicit workspace path (a git worktree root). Defaults to the session's recorded working directory.",
            },
            "title": {
                "type": "string",
                "description": "PR title for publish (defaults to the head commit subject) or edit.",
            },
            "body": {
                "type": "string",
                "description": "PR body for publish or edit.",
            },
            "base_branch": {
                "type": "string",
                "description": "Base branch for the PR; defaults to the remote's default branch.",
            },
            "head_branch": {
                "type": "string",
                "description": "Head branch to publish; defaults to the current branch.",
            },
            "reviewers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "GitHub logins to request a review from (review action).",
            },
            "evidence": {
                "type": "string",
                "description": "Optional caption text embedded under the uploaded file in the PR body (evidence action).",
            },
            "evidence_path": {
                "type": "string",
                "description": "Workspace-relative path of the image/video to upload as PR evidence (evidence action; PNG, JPEG, GIF, WebP, MP4, MOV, WebM, at most 10 MB).",
            },
            "evidence_sha256": {
                "type": "string",
                "description": "Inspected lowercase SHA-256 of the evidence file; when passed, the bytes are verified against it before any upload (evidence action).",
            },
            "evidence_size_bytes": {
                "type": "integer",
                "description": "Inspected byte count of the evidence file; when passed, the bytes are verified against it before any upload (evidence action).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _handle_github_publish(args: dict, current_session_id=None, session_id=None,
                           task_id=None) -> str:
    """Dispatch one publish action. Refusals are results (``success: false``,
    ``effect: none``); hard errors are ``success: false`` with ``error``."""
    action = (args or {}).get("action") or ""
    fn = _ACTIONS.get(action)
    if fn is None:
        return _fail("unknown-action", action=action, known=sorted(_ACTIONS))
    kw = {"current_session_id": current_session_id, "session_id": session_id,
          "task_id": task_id}
    return fn(args, kw)


registry.register(
    name="github_publish",
    toolset="github",
    schema=_GITHUB_PUBLISH_SCHEMA,
    handler=_handle_github_publish,
    emoji="📤",
)
