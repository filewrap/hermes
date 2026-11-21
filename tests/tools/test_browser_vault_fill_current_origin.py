"""A vault fill runs on the CURRENT page, whatever origin the item was saved for.

One behaviour per fill path (password, one-time code, payment card, address, save-then-fill): on a page
whose origin is not one the item was saved for, the fill goes ahead and the in-page write is pinned to
the current page origin (the fill script re-asserts it synchronously before writing, so a page that
navigated between inspection and fill gets nothing). A payment fill still asks the user to confirm
before anything is written.

The page is a stand-in for the CDP supervisor (current URL + inspected controls; it records every
secret-bearing evaluation). Secrets are dummy values in a temporary local vault.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.vault_backends.local import LocalLoginBackend
from agent.vault_store import VaultStore, normalize_origin

_PASSWORD = "dummy-password-7"
_OTP_SEED = "JBSWY3DPEHPK3PXP"  # base32 test seed, not a real account
_CARD = {"card_number": "4111111111111111", "cardholder_name": "Test User", "exp_month": "07",
         "exp_year": "2029", "cvc": "123", "billing_postal_code": "94107"}
_ADDRESS = {"address_line1": "1 Test Way", "city": "Testville", "state": "CA", "postal_code": "94107",
            "country": "US"}

_LOGIN_CONTROLS = [
    {"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "user", "type": "text"},
    {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
]
_OTP_CONTROLS = [{"autocomplete": "one-time-code", "formIndex": 0, "index": 0, "label": "Authentication code",
                  "name": "otp", "type": "text"}]
_CARD_CONTROLS = [
    {"autocomplete": "cc-number", "index": 0, "type": "text"},
    {"autocomplete": "cc-exp", "index": 1, "type": "text"},
    {"autocomplete": "cc-csc", "index": 2, "type": "text"},
]
_ADDRESS_CONTROLS = [
    {"autocomplete": "address-line1", "index": 0, "type": "text"},
    {"autocomplete": "postal-code", "index": 1, "type": "text"},
]


class _FakePage:
    """The supervisor's view of one open tab: answers origin reads and input inspection, and records every
    secret-bearing fill script as a write."""

    def __init__(self, href: str, controls: list[dict]):
        self.href, self.controls, self.writes = href, controls, []

    def focus_page(self, origin: str, accept=None) -> dict:
        return {"ok": bool(origin) and normalize_origin(self.href) == origin, "url": self.href}

    def evaluate_runtime(self, expression: str) -> dict:
        if expression.strip() == "window.location.href":
            return {"ok": True, "result": self.href}
        if "expectedOrigin" in expression:  # the fill script (build_fill_js)
            self.writes.append(expression)
            return {"ok": True, "result": json.dumps({"filled": 1})}
        return {"ok": True, "result": json.dumps(self.controls)}  # input inspection


class _RecordingBackend(LocalLoginBackend):
    """The real local backend, recording which secret kinds were resolved."""

    def __init__(self):
        self.resolved: list[str] = []

    def resolve_password(self, handle):
        self.resolved.append("password")
        return super().resolve_password(handle)

    def resolve_otp(self, handle):
        self.resolved.append("otp")
        return super().resolve_otp(handle)

    def resolve_secret(self, handle):
        self.resolved.append("secret")
        return super().resolve_secret(handle)


@pytest.fixture()
def vault(tmp_path):
    from agent import redact
    from agent.vault_backends import unlock as unlock_mod

    store = VaultStore(base_dir=tmp_path / "vault")
    backend = _RecordingBackend()
    asked: list[str] = []
    unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
    try:
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            yield store, backend, asked
    finally:
        unlock_mod.set_code_prompt_callback(None)
        unlock_mod.set_save_login_prompt_callback(None)
        redact.clear_vault_redaction_values()


def _on_page(page: _FakePage):
    from tools import browser_vault_tool

    return patch.object(browser_vault_tool, "_ensure_supervisor", return_value=page)


def _login(store, origin="https://bank.example", otp=True):
    secret = {"identifier_type": "username", "identifier": "tester", "password": _PASSWORD}
    if otp:
        secret["otp_secret"] = _OTP_SEED
    return store.add_item("login", "Bank", secret, origin=origin)


def _filled_on_current_origin(out: dict, backend: _RecordingBackend, page: _FakePage,
                              origin: str, resolved: list[str]):
    """The fill went ahead on the open page: the secret was resolved, one in-page write happened, and the
    write names the open page's origin."""
    assert out["success"] is True
    assert out["origin"] == origin
    assert backend.resolved == resolved
    [write] = page.writes
    assert json.dumps(origin) in write  # the in-page origin assert names the current page


class TestOneTimeCode:
    def test_saved_code_is_minted_on_a_page_on_another_origin(self, vault):
        from tools import browser_vault_tool

        store, backend, asked = vault
        meta = _login(store)
        page = _FakePage("https://elsewhere.example/verify", _OTP_CONTROLS)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"))
        _filled_on_current_origin(out, backend, page, "https://elsewhere.example", ["otp"])
        assert out["source"] == "local" and asked == []

    def test_saved_code_is_minted_on_the_saved_origin_and_pinned_to_it(self, vault):
        from tools import browser_vault_tool

        store, backend, asked = vault
        meta = _login(store)
        page = _FakePage("https://bank.example/2fa", _OTP_CONTROLS)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t"))
        assert out["success"] is True and out["source"] == "local" and out["origin"] == "https://bank.example"
        assert backend.resolved == ["otp"] and asked == []
        [write] = page.writes
        assert '"https://bank.example"' in write  # the in-page origin assert names the saved origin

    def test_a_code_without_a_saved_item_is_still_asked_of_the_user(self, vault):
        from tools import browser_vault_tool

        _, backend, asked = vault
        page = _FakePage("https://elsewhere.example/verify", _OTP_CONTROLS)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
        assert out["success"] is True and out["source"] == "user"
        assert out["origin"] == "https://elsewhere.example"
        assert asked == ["elsewhere.example"] and backend.resolved == []


class TestFill:
    def test_password_is_resolved_and_filled_on_a_page_on_another_origin(self, vault):
        from tools import browser_vault_tool

        store, backend, _ = vault
        meta = _login(store, otp=False)
        page = _FakePage("https://elsewhere.example/login", _LOGIN_CONTROLS)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="t"))
        _filled_on_current_origin(out, backend, page, "https://elsewhere.example", ["password"])
        assert out["filled_fields"] == 1

    def test_card_fill_asks_the_user_to_confirm_then_fills_on_a_page_on_another_origin(self, vault):
        from tools import browser_vault_tool

        store, backend, _ = vault
        meta = store.add_item(kind="payment", label="Card", origin="https://shop.example", secret=_CARD)
        page = _FakePage("https://elsewhere.example/checkout", _CARD_CONTROLS)
        with _on_page(page), \
             patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
            out = json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="t"))
        consent.assert_called_once()
        _filled_on_current_origin(out, backend, page, "https://elsewhere.example", ["secret"])
        assert out["filled_fields"] == 1

    def test_address_is_resolved_and_filled_on_a_page_on_another_origin(self, vault):
        from tools import browser_vault_tool

        store, backend, _ = vault
        meta = store.add_item(kind="address", label="Home", origin="https://shop.example", secret=_ADDRESS)
        page = _FakePage("https://elsewhere.example/shipping", _ADDRESS_CONTROLS)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="t"))
        _filled_on_current_origin(out, backend, page, "https://elsewhere.example", ["secret"])
        assert out["filled_fields"] == 1

    def test_save_login_fill_follows_the_page_after_navigation(self, vault):
        """save_login stores the login for the page it read, then fills the page the user ended on when the
        page navigated away while the user was typing."""
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        store, backend, _ = vault
        page = _FakePage("https://bank.example/login", _LOGIN_CONTROLS)

        def prompt(origin, host):
            page.href = "https://elsewhere.example/login"
            return {"identifier": "tester", "password": _PASSWORD}

        unlock_mod.set_save_login_prompt_callback(prompt)
        with _on_page(page):
            out = json.loads(browser_vault_tool.browser_vault_save_login(task_id="t"))
        assert out["success"] is True
        assert out["origin"] == "https://bank.example"  # the login is stored for the page it was read on
        assert [m.origin for m in store.list_items()] == ["https://bank.example"]
        _filled_on_current_origin(out["fill"], backend, page, "https://elsewhere.example", ["password"])
