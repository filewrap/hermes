from __future__ import annotations

import yaml

from hermes_cli import config
from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
from tools import approval, approval_context
from tools.approval import check_all_command_guards, check_dangerous_command


def _under(home, fn, *args):
    token = set_hermes_home_override(str(home))
    try:
        return fn(*args)
    finally:
        reset_hermes_home_override(token)


def _make_profile(path):
    path.mkdir()
    (path / "config.yaml").write_text(
        "approvals:\n  mode: manual\n  timeout: 30\n"
        "command_allowlist: []\nsecurity:\n  tirith_enabled: false\n",
        encoding="utf-8",
    )


def test_gateway_always_grant_survives_new_session_and_is_profile_scoped(tmp_path, monkeypatch):
    profile_a, profile_b = tmp_path / "profile-a", tmp_path / "profile-b"
    _make_profile(profile_a)
    _make_profile(profile_b)
    session = "telegram:chat-1"
    command = "rm -rf /tmp/persisted-approval-test"
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    config._LOAD_CONFIG_CACHE.clear()

    def run_gateway(profile, active_session, choice):
        profile_token = set_hermes_home_override(str(profile))
        session_token = approval_context.set_current_session_key(active_session)
        notified = []

        def notify(data):
            notified.append(data)
            assert approval.resolve_gateway_approval(
                active_session, choice, request_id=data["request_id"],
            ) == 1

        approval.register_gateway_notify(active_session, notify)
        try:
            result = check_dangerous_command(command, "local")
        finally:
            approval.unregister_gateway_notify(active_session)
            approval_context.reset_current_session_key(session_token)
            reset_hermes_home_override(profile_token)
        return result, notified

    # The real gateway wait/resolver path returns "always" to _human_decision,
    # which persists its pattern key to profile A's config.yaml.
    result, first_prompt = run_gateway(profile_a, session, "always")
    assert result["approved"] is True
    assert len(first_prompt) == 1
    expected_key = approval.detect_dangerous_command(command)[1]
    assert expected_key in yaml.safe_load((profile_a / "config.yaml").read_text())["command_allowlist"]

    # Simulate another session and a cold process-side cache: the on-disk grant,
    # not the original session cache, must bypass the same approval prompt.
    a_key = hermes_home_key(profile_a)
    approval._permanent_approved_by_home.pop(a_key, None)
    approval._permanent_baseline_by_home.pop(a_key, None)
    result, no_prompt = run_gateway(profile_a, "telegram:chat-2", "once")
    assert result["approved"] is True
    assert no_prompt == []

    # The same gateway session id and command in another profile remains unapproved.
    result, profile_b_prompt = run_gateway(profile_b, session, "once")
    assert result["approved"] is True
    assert len(profile_b_prompt) == 1
    assert yaml.safe_load((profile_b / "config.yaml").read_text())["command_allowlist"] == []

    # A -> B -> A routing cannot lose or cross-apply A's persistent grant.
    result, no_prompt = run_gateway(profile_a, "telegram:chat-3", "once")
    assert result["approved"] is True
    assert no_prompt == []

    # Standing pattern approval never removes the unconditional hardline or
    # explicit sudo-stdin floors.
    token = set_hermes_home_override(str(profile_a))
    try:
        hardline = check_all_command_guards("rm -rf /", "local")
        sudo_stdin = check_all_command_guards("sudo -S whoami", "local")
    finally:
        reset_hermes_home_override(token)
    assert hardline["approved"] is False
    assert "hardline" in hardline["message"].lower()
    assert sudo_stdin["approved"] is False
    assert "sudo" in sudo_stdin["message"].lower()


def test_gateway_callback_cannot_exceed_request_scope():
    from tools.approval_gateway_wait import _ApprovalEntry

    session = "gateway:scope-test"
    entry = _ApprovalEntry({"request_id": "scope-1", "allow_session": False, "allow_permanent": False})
    with approval._lock:
        approval._gateway_queues[session] = [entry]
    try:
        assert approval.resolve_gateway_approval(session, "always", request_id="scope-1") == 0
        assert approval.resolve_gateway_approval(session, "session", request_id="scope-1") == 0
        assert not entry.event.is_set()
        assert approval.resolve_gateway_approval(session, "once", request_id="scope-1") == 1
        assert entry.result == "once"
        assert entry.event.is_set()
    finally:
        with approval._lock:
            approval._gateway_queues.pop(session, None)


def test_human_only_callback_choices_are_closed_set():
    assert approval.gateway_choice_allowed({}, "once")
    assert approval.gateway_choice_allowed({}, "deny")
    assert not approval.gateway_choice_allowed({}, "auto")
    assert not approval.gateway_choice_allowed({}, "profile")
    assert approval.gateway_choice_allowed({"allow_session": False}, "once")
    assert not approval.gateway_choice_allowed({"allow_permanent": False}, "always")


def test_stored_pattern_grant_short_circuits_smart_review(monkeypatch):
    session = "smart:approved-pattern"
    token = approval_context.set_current_session_key(session)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_smart_verdict", lambda *_: (_ for _ in ()).throw(AssertionError("AI called")))
    approval.approve_session(session, "plugin_rule:known-rule")
    try:
        result = approval.request_tool_approval("tool", "known rule", rule_key="known-rule")
    finally:
        approval_context.reset_current_session_key(token)
    assert result["approved"] is True
