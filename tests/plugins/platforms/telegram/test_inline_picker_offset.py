"""Negative or malformed ``offset`` values must not corrupt inline-picker pagination.

``inline_query.offset`` is client-controlled text echoed back from our previous ``next_offset``;
``int()`` accepts negatives, which would slice the catalog with a negative index (tail items or an
empty page) and emit a ``next_offset`` that resumes from the wrong position.

Only the pure ``build_inline_results`` contract is pinned here. The handler e2e was removed:
``TelethonAdapter._on_inline_query`` deliberately answers empty — inline result pickers are not
wired to Hermes's control resolvers yet — so no handler path serves the catalog to assert on.
"""

import pytest

from plugins.platforms.telegram import inline_picker


@pytest.fixture
def catalog(monkeypatch):
    items = [{"name": f"cmd{i:03d}", "description": f"command {i}"} for i in range(120)]
    monkeypatch.setattr(inline_picker, "collect_inline_catalog", lambda: items)
    return items


def test_negative_offset_returns_first_page(catalog):
    results, next_offset = inline_picker.build_inline_results("cmd", "-5")
    first, first_next = inline_picker.build_inline_results("cmd", "0")
    assert results == first
    assert next_offset == first_next == "50"


@pytest.mark.parametrize("offset", ["-1", "-50", "-1000"])
def test_any_negative_offset_is_first_page(catalog, offset):
    results, next_offset = inline_picker.build_inline_results("cmd", offset)
    assert len(results) == 50
    assert results[0]["title"] == "/cmd000"
    assert next_offset == "50"


def test_negative_offset_does_not_serve_catalog_tail(monkeypatch):
    """With a catalog smaller than one page the negative slice used to return the tail."""
    items = [{"name": f"cmd{i:02d}", "description": ""} for i in range(40)]
    monkeypatch.setattr(inline_picker, "collect_inline_catalog", lambda: items)
    results, next_offset = inline_picker.build_inline_results("cmd", "-3")
    assert len(results) == 40
    assert next_offset == ""


@pytest.mark.parametrize("offset", ["", "abc", "3.5", "--5", None])
def test_malformed_offset_falls_back_to_first_page(catalog, offset):
    results, next_offset = inline_picker.build_inline_results("cmd", offset)
    assert len(results) == 50
    assert next_offset == "50"


def test_valid_offset_paginates(catalog):
    results, next_offset = inline_picker.build_inline_results("cmd", "50")
    assert results[0]["title"] == "/cmd050"
    assert next_offset == "100"
    last, last_next = inline_picker.build_inline_results("cmd", "100")
    assert len(last) == 20
    assert last_next == ""


def test_past_end_offset_returns_empty(catalog):
    results, next_offset = inline_picker.build_inline_results("cmd", "99999")
    assert results == []
    assert next_offset == ""

