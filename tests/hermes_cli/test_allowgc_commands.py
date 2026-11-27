"""Registry, tenant-policy and menu wiring for /allowgc and /imout.

/allowgc (owner-only, group-only) opens the shared-group-access confirmation;
its owner/group gating lives in the Telegram adapter
(``handle_access_command``), not in the registry: the registry carries the
same shape as /allow and /trigger (gateway-only, dispatch mid-run).
/imout (admitted allowgc member, group-only) opens the one-way opt-out
confirmation in DM; the adapter's multiuser branch answers it inline before
any tenant dispatch, but the registry still classifies it as a tenant
command so the tenant drift guard sees an explicit scope for it.
"""
from __future__ import annotations

from hermes_cli.commands import (
    GATEWAY_KNOWN_COMMANDS,
    is_gateway_known_command,
    resolve_command,
    should_bypass_active_session,
)
from hermes_cli.commands_platforms import (
    _SLACK_VIA_HERMES_ONLY,
    slack_native_slashes,
    slack_subcommand_map,
    telegram_bot_commands,
    telegram_menu_commands,
    telegram_menu_overflow,
)
from gateway.tenant_commands import (
    _registered_gateway_commands,
    _TENANT_COMMANDS,
    tenant_command_policy,
)


def test_allowgc_resolves_like_allow():
    """Same registry mechanism as /allow: gateway-only, dispatch mid-run."""
    allow = resolve_command("allow")
    allowgc = resolve_command("allowgc")
    assert allowgc is not None
    assert allowgc.category == allow.category
    assert allowgc.gateway_only is True
    assert allowgc.gateway_only == allow.gateway_only
    assert allowgc.busy_policy == "dispatch"
    assert allowgc.busy_policy == allow.busy_policy


def test_imout_resolves_like_trigger():
    """Member command, same registry mechanism as /trigger (gateway-only,
    dispatch mid-run); owner/group gating is enforced adapter-side."""
    trigger = resolve_command("trigger")
    imout = resolve_command("imout")
    assert imout is not None
    assert imout.category == trigger.category
    assert imout.gateway_only is True
    assert imout.busy_policy == "dispatch"
    assert imout.busy_policy == trigger.busy_policy


def test_tenant_policy_allowgc_is_tenant_and_classified():
    """No-limits: there is no owner-only tenant class — allowgc is a
    classified tenant command like any other. Owner/group gating lives
    adapter-side (handle_access_command), not in the tenant policy."""
    assert "allowgc" in _TENANT_COMMANDS
    assert tenant_command_policy("allowgc") == "tenant"


def test_tenant_policy_imout_is_tenant_and_classified():
    """imout is an explicit tenant classification: the adapter's multiuser
    branch answers it before any tenant dispatch, but the drift guard must
    still see an explicit scope for every registered gateway command."""
    assert "imout" in _TENANT_COMMANDS
    assert tenant_command_policy("imout") == "tenant"
    assert "allowgc" not in _registered_gateway_commands() - _TENANT_COMMANDS
    assert "imout" not in _registered_gateway_commands() - _TENANT_COMMANDS


def test_telegram_menu_still_caps_at_100_and_keeps_both_visible():
    """Core grew by two (103 -> 105) but the Telegram menu stays at 100: the
    deterministic lowest-priority tail overflows instead. Both new commands
    stay menu-visible by registry order (they are not priority-ranked, like
    /allow and /trigger); the tail that drops is the unranked tail."""
    menu, _overflow = telegram_menu_commands()
    names = {name for name, _desc in menu}
    assert len(menu) == 100
    assert {"allowgc", "imout"} <= names
    # Operation commands must stay menu-visible (same contract as the
    # default-scope menu test).
    assert {"poll", "tgmessages", "tgfiles", "tginfo", "tgadmin", "tgprofile",
            "managedbots", "botpeers"} <= names
    overflow = telegram_menu_overflow()["names"]
    assert {"version", "button", "webauth"} <= set(overflow)


def test_typed_commands_work_without_the_menu():
    """The typed path is registry-based, not menu-based: /version is out of
    the menu yet still resolves and dispatches, and so do the new commands."""
    menu, _overflow = telegram_menu_commands()
    menu_names = {name for name, _desc in menu}
    assert "version" not in menu_names
    assert resolve_command("version") is not None
    for name in ("allowgc", "imout"):
        assert resolve_command(name) is not None
        assert is_gateway_known_command(name) is True
        assert name in GATEWAY_KNOWN_COMMANDS
        assert name in {cmd for cmd, _desc in telegram_bot_commands()}


def test_mid_run_commands_bypass_the_active_session_guard():
    """Guard 1 (platforms/base.py) dispatches every resolvable command inline
    instead of queueing it in _pending_messages — same as /allow. The
    multiuser branch answers both even earlier, before base.handle_message."""
    for name in ("allowgc", "imout", "allow"):
        assert should_bypass_active_session(name) is True


def test_slack_routes_both_through_hermes_command():
    """Telegram-group-only access commands have no Slack equivalent: they take
    no native Slack slot (so /reasoning keeps its slot under the 50 cap) and
    stay reachable as `/hermes <command>`."""
    assert {"allowgc", "imout"} <= _SLACK_VIA_HERMES_ONLY
    native = {name for name, _desc, _hint in slack_native_slashes()}
    assert "allowgc" not in native
    assert "imout" not in native
    mapping = slack_subcommand_map()
    assert mapping["allowgc"] == "/allowgc"
    assert mapping["imout"] == "/imout"
