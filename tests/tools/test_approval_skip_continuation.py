"""Skipping an approval blocks its action, not independent work in the task."""

import pytest

from tools import approval, approval_context


@pytest.mark.parametrize("gate,surface", [
    ("command", "cli"), ("command", "gateway"), ("code", "gateway"),
    ("action", "cli"), ("action", "gateway"),
])
@pytest.mark.parametrize("choice", ["deny", "timeout"])
def test_skipped_approval_preserves_consent_and_independent_work(
    monkeypatch, gate, surface, choice,
):
    session = f"skip-{gate}-{surface}-{choice}"
    token = approval_context.set_current_session_key(session)
    for key in ("HERMES_CRON_SESSION", "HERMES_YOLO_MODE", "HERMES_EXEC_ASK",
                "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION" if surface == "gateway" else "HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_config",
                        lambda: {"mode": "manual", "timeout": 0 if choice == "timeout" else 60})
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **kw: choice)
    notified = []

    def notify(data):
        notified.append(data)
        if choice == "deny":
            approval.resolve_gateway_approval(session, "deny", reason="skip this step")

    if surface == "gateway":
        approval.register_gateway_notify(session, notify)
    try:
        if gate == "command":
            # A dangerous command reaches the human-decision path on its own
            # pattern (upstream removed the tirith security scanner the old
            # benign command relied on); it is only ever assessed, never run.
            result = approval.check_all_command_guards("rm -rf /tmp/security-scan-regression", "local")
        elif gate == "code":
            result = approval.check_execute_code_guard("print('approval regression')", "local")
        else:
            result = approval.request_tool_approval("write_file", "protected write")

        assert result["approved"] is False
        assert result["user_consent"] is False
        assert result["outcome"] == ("denied" if choice == "deny" else "timeout")
        message = result["message"]
        assert "Do NOT retry" in message
        assert "Continue with other independent, already-authorized work" in message
        assert "Stop the current workflow" not in message
        assert not approval.is_approved(session, result["pattern_key"])
        assert not approval.has_blocking_approval(session)
        if surface == "gateway":
            assert len(notified) == 1
            if choice == "deny":
                assert "skip this step" in message
    finally:
        approval.unregister_gateway_notify(session)
        approval.clear_session(session)
        approval_context._approval_session_key.reset(token)
