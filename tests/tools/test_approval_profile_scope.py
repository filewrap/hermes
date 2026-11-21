from tools import approval
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _under(home, fn, *args):
    token = set_hermes_home_override(str(home))
    try:
        return fn(*args)
    finally:
        reset_hermes_home_override(token)


def test_session_approval_and_yolo_are_profile_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "_permanent_set", lambda: set())
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    session = "telegram:chat-1"

    _under(profile_a, approval.approve_session, session, "dangerous-operation")
    _under(profile_a, approval.enable_session_yolo, session)

    assert _under(profile_a, approval.is_approved, session, "dangerous-operation")
    assert _under(profile_a, approval.is_session_yolo_enabled, session)
    assert not _under(profile_b, approval.is_approved, session, "dangerous-operation")
    assert not _under(profile_b, approval.is_session_yolo_enabled, session)

    _under(profile_a, approval.clear_session, session)
    assert not _under(profile_a, approval.is_approved, session, "dangerous-operation")
    assert not _under(profile_a, approval.is_session_yolo_enabled, session)
    # Clearing one profile's session cannot revoke a same-named session in another.
    _under(profile_b, approval.approve_session, session, "dangerous-operation")
    assert _under(profile_b, approval.is_approved, session, "dangerous-operation")
