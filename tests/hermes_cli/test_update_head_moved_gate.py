"""Tests for the post-pull HEAD-movement gate in ``hermes update``.

Issue #79678: a detached/pinned checkout can report "N new commit(s)"
against origin, run the ff-only merge successfully, and still sit on the
old commit afterward (the branch-switch step re-detaches to the raw SHA).
Before this guard ``hermes update`` printed "✓ Code updated!" and
reinstalled deps + rebuilt the desktop app against the stale tree — no
error, no warning. The gate compares the pre-pull and post-pull HEAD SHA
and fails loudly when the update was a no-op.
"""

from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import main_web_build
from hermes_cli import main_install_repair
from hermes_cli import update_cmd

_PINNED_SHA = "abc123" + "0" * 34
_MOVED_PRE_SHA = "abc123" + "0" * 34
_MOVED_POST_SHA = "def456" + "0" * 34


def _make_head_pinned_side_effect(sha=_PINNED_SHA):
    """Simulate a detached checkout pinned to ``sha``: HEAD never moves.

    The two HEAD-read forms disagree, as in the real #79678 scenario: the
    branch switch lands on the target (``rev-parse -q --verify HEAD`` sees
    the target right after checkout), but the pull's ``rev-parse HEAD``
    reads still see the pinned SHA — the switch re-detached to it.
    """

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        # The pull target: origin/main resolves to a newer commit the
        # pinned HEAD never reaches.
        if "rev-parse" in joined and "--verify" in joined and "origin/main" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{_MOVED_POST_SHA}\n", stderr="")

        # Landing check right after the switch: HEAD is on the target.
        if "rev-parse" in joined and "--verify" in joined and joined.endswith("HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{_MOVED_POST_SHA}\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _make_head_moved_side_effect(pre_sha=_MOVED_PRE_SHA, post_sha=_MOVED_POST_SHA):
    """Simulate git commands where HEAD advances from pre_sha to post_sha.

    HEAD reads are gated on the merge, not on a call count: several plain
    ``rev-parse HEAD`` reads (version stamping, branch checks) run before
    the pull and must all see pre_sha; only after ``merge --ff-only`` does
    HEAD report post_sha. The landing check (``--verify HEAD``) always sees
    the target.
    """

    state = {"merged": False}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        # git rev-list HEAD..origin/main --count  (behind count)
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        # git rev-parse -q --verify origin/main^{commit}  (pull target)
        if "rev-parse" in joined and "--verify" in joined and "origin/main" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # Landing check (--verify HEAD): HEAD is on the target.
        if "rev-parse" in joined and "--verify" in joined and joined.endswith("HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # git rev-parse HEAD  — pre_sha until the merge lands, post_sha after.
        if joined.endswith("rev-parse HEAD"):
            sha = post_sha if state["merged"] else pre_sha
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        # The pull itself: once the merge runs, HEAD is on the target.
        if "merge" in joined and "--ff-only" in joined:
            state["merged"] = True

        # Everything else (checkout, etc.) succeeds quietly.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch_update_deps(monkeypatch, tmp_path, run_side_effect):
    """Patch the hermes_cli.main helpers ``_cmd_update_impl`` touches.

    ``_m()`` in update_cmd.py lazily returns hermes_cli.main, so patching
    attributes on that module is the canonical test surface (matches
    tests/hermes_cli/test_cmd_update.py).
    """
    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    # cmd_update now retargets to the owning install first; the test venv
    # lives in-tree, so with PROJECT_ROOT patched to tmp_path the retarget
    # would re-exec this command under pytest's argv. Stub it: this file
    # tests the HEAD-movement gate, not the venv redirect.
    from hermes_cli import update_owning_install

    monkeypatch.setattr(
        update_owning_install, "retarget_to_owning_install", lambda *a, **k: None
    )
    (tmp_path / ".git").mkdir()  # pass the "is a git repo" gate
    monkeypatch.setattr(
        hermes_main, "_resolve_update_branch", lambda args: "main"
    )
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(
        hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        main_web_build, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_run_pre_update_backup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_pause_windows_gateways_for_update", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    # Short-circuit the long tail: dependency install + desktop build.
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(main_install_repair, "_clear_update_incomplete_marker", lambda: None)
    # Gateway restart path (called after a successful update).
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **k: None)
    # Keep the (now surfaced — #78574) gateway auto-restart phase away from
    # this machine's real gateways: discovery returns nothing, systemd is
    # unsupported, so the phase is a clean no-op for both snapshots.
    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda *a, **k: []
    )
    monkeypatch.setattr(
        hermes_gateway, "supports_systemd_services", lambda: False
    )
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: []
    )
    # The merged update spawns the new tree's hermes_cli/update_completion.py
    # as a subprocess; this test's tmp tree has no source, so the real child
    # exits 2 and the update ends in the lost-completion path without ever
    # printing "✓ Code updated!". Stub the completion as a clean success,
    # printing the line the child would print via _print_update_completion.
    def _fake_run_completion(*a, **k):
        print("✓ Code updated!")
        return {"exit_code": 0, "receipt": None, "windows_resume": None}

    monkeypatch.setattr(update_cmd, "run_completion", _fake_run_completion)

def test_update_success_when_head_moves(monkeypatch, tmp_path, capsys):
    """When the pull advances HEAD, the update proceeds normally."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    hermes_main.cmd_update(args)  # completes normally (no SystemExit)

    out = capsys.readouterr().out
    assert "✓ Code updated!" in out
    assert "Code did not move" not in out


def test_update_fails_loudly_when_head_pinned(monkeypatch, tmp_path, capsys):
    """A detached/pinned HEAD that never moves must fail loudly, not print
    '✓ Code updated!' against the stale tree."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_pinned_side_effect())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Code did not move" in out
    assert "✓ Code updated!" not in out
    assert "checkout main" in out
