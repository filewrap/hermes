"""Behaviour contracts for ``gateway/web_designer/tokens.py``.

Three tiers only (primitive -> semantic -> component, never reversed),
semantic names by role not look, spacing on a 4px base, one source file,
generated CSS never hand-edited, and token drift: a literal appearing
twice is a defect, once is a change-level finding.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gateway.web_designer.tokens import (
    generate_css, load_tokens, save_tokens, token_drift, validate_tokens,
)

VALID_TOKENS = {
    "primitive": {
        "color": {
            "ink": {"value": "#111827", "$type": "color"},
            "paper": {"value": "#ffffff", "$type": "color"},
            "brand": {"value": "#2563eb", "$type": "color"},
        },
        "spacing": {
            "sm": {"value": "8px", "$type": "dimension"},
            "md": {"value": "16px", "$type": "dimension"},
            "lg": {"value": "24px", "$type": "dimension"},
        },
    },
    "semantic": {
        "color": {
            "text": {"primary": {"value": "{primitive.color.ink}"}},
            "surface": {"base": {"value": "{primitive.color.paper}"}},
            "action": {"primary": {"value": "{primitive.color.brand}"}},
        },
        "spacing": {
            "section": {"value": "{primitive.spacing.lg}"},
        },
    },
    "component": {
        "button": {
            "primary": {
                "background": {"value": "{semantic.color.action.primary}"},
                "text": {"value": "{semantic.color.surface.base}"},
            },
        },
    },
    "themes": {
        "dark": {"semantic.color.surface.base": "#121212"},
    },
}


def test_valid_three_tier_tree_passes():
    assert validate_tokens(VALID_TOKENS) == []


def test_references_must_run_primitive_to_semantic_to_component():
    raw_semantic = json.loads(json.dumps(VALID_TOKENS))
    raw_semantic["semantic"]["color"]["text"]["primary"] = {"value": "#111827"}
    assert validate_tokens(raw_semantic)

    primitive_ref = json.loads(json.dumps(VALID_TOKENS))
    primitive_ref["primitive"]["color"]["ink"] = {
        "value": "{semantic.color.text.primary}"}
    assert validate_tokens(primitive_ref)

    component_to_primitive = json.loads(json.dumps(VALID_TOKENS))
    component_to_primitive["component"]["button"]["primary"]["background"] = {
        "value": "{primitive.color.brand}"}
    assert validate_tokens(component_to_primitive)

    dangling = json.loads(json.dumps(VALID_TOKENS))
    dangling["semantic"]["color"]["text"]["primary"] = {
        "value": "{primitive.color.missing}"}
    assert validate_tokens(dangling)


def test_semantic_tokens_are_named_by_role_not_value():
    value_named = json.loads(json.dumps(VALID_TOKENS))
    value_named["semantic"]["color"]["blue-500"] = {
        "value": "{primitive.color.brand}"}
    errors = validate_tokens(value_named)
    assert errors and any("blue-500" in e for e in errors)
    assert validate_tokens(VALID_TOKENS) == []


def test_spacing_must_be_on_the_4px_base():
    off_base = json.loads(json.dumps(VALID_TOKENS))
    off_base["primitive"]["spacing"]["odd"] = {"value": "6px"}
    errors = validate_tokens(off_base)
    assert errors and any("6px" in e for e in errors)
    on_base = json.loads(json.dumps(VALID_TOKENS))
    on_base["primitive"]["spacing"]["xl"] = {"value": "32px"}
    assert validate_tokens(on_base) == []


def test_missing_tier_and_extra_tier_are_errors():
    assert validate_tokens({"primitive": {}, "semantic": {}})
    assert validate_tokens({**VALID_TOKENS, "extra": {}})


def test_generate_css_resolves_references_and_marks_itself_generated():
    css = generate_css(VALID_TOKENS)
    assert "DO NOT EDIT BY HAND" in css
    assert "--color-text-primary: #111827;" in css
    assert "--color-action-primary: #2563eb;" in css
    assert "--button-primary-background: #2563eb;" in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "--color-surface-base: #121212;" in css


def test_generate_css_without_dark_theme_has_no_dark_block():
    tokens = {k: v for k, v in VALID_TOKENS.items() if k != "themes"}
    css = generate_css(tokens)
    assert "prefers-color-scheme: dark" not in css
    assert "--color-text-primary: #111827;" in css


def test_generate_css_rejects_invalid_tokens():
    with pytest.raises(ValueError):
        generate_css({"semantic": {"color": {"text": {"primary": {
            "value": "#111827"}}}}})


def test_save_and_load_round_trip(tmp_path: Path):
    run_dir = tmp_path / "run"
    saved = save_tokens(run_dir, VALID_TOKENS)
    assert saved == run_dir / "tokens.json"
    assert saved.is_file()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert load_tokens(saved) == VALID_TOKENS


def test_save_invalid_tokens_writes_nothing(tmp_path: Path):
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError):
        save_tokens(run_dir, {"primitive": {"color": {"ink": {
            "value": "{semantic.color.text.primary}"}}},
            "semantic": {}, "component": {}})
    assert not (run_dir / "tokens.json").exists()


def test_load_tokens_rejects_bad_json(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tokens(path)
    with pytest.raises(ValueError):
        load_tokens(tmp_path / "missing.json")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_token_drift_twice_is_a_defect_once_is_a_change(tmp_path: Path):
    _write(tmp_path / "styles.css",
           ".a { color: #ff5733; }\n.b { color: #ff5733; }\n.c { color: #123456; }\n")
    findings = token_drift(tmp_path, VALID_TOKENS)
    by_literal = {f.actual: f for f in findings}
    twice = [f for f in findings if "#ff5733" in f.actual]
    assert twice and twice[0].severity == "defect"
    once = [f for f in findings if "#123456" in f.actual]
    assert once and once[0].severity == "change"
    assert all(f.rule_id == "R027" for f in twice + once)
    assert by_literal  # findings carry the literal and its count


def test_token_drift_ignores_tokenised_values_and_generated_css(tmp_path: Path):
    _write(tmp_path / "styles.css",
           ".a { color: #111827; margin: 16px; }\n")
    _write(tmp_path / "tokens.css",
           "/* Generated from tokens.json — DO NOT EDIT BY HAND. */\n"
           ".g { color: #ff5733; margin: 13px; }\n")
    assert token_drift(tmp_path, VALID_TOKENS) == []


def test_token_drift_flags_raw_spacing_and_font_sizes(tmp_path: Path):
    _write(tmp_path / "styles.css",
           "p { font-size: 13px; margin: 7px; }\n")
    findings = token_drift(tmp_path, VALID_TOKENS)
    rules = {f.rule_id for f in findings}
    assert "R012" in rules  # raw font size
    assert "R028" in rules  # raw spacing
