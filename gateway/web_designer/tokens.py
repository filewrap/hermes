"""3-tier W3C Design Tokens JSON (audit 6.14 / skill 3.28).

Single source of truth: one ``tokens.json`` per run, three tiers only —

    primitive -> semantic -> component, never reversed.

Primitive leaves hold raw values. Semantic leaves hold references to
primitive tokens only, named by role (``color.text.primary``), never by
look (``color.blue-500``). Component leaves reference semantic tokens
only, never primitives or raw values. Generated CSS (``generate_css``) is
never hand-edited; a value with no token stops the build until the token
exists, and a literal appearing twice in a component file becomes a
token (see :func:`token_drift`).

Only the standard library is used (``json``, ``re``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils import atomic_json_write

from .types import Finding

TIERS = ("primitive", "semantic", "component")
_TOP_LEVEL_KEYS = TIERS + ("themes", "$metadata")

#: Segment of a semantic token name that names a value instead of a role,
#: e.g. ``blue-500``, ``dark-grey``, ``red-600``. A semantic token named
#: this way would lie after a rebrand, so it is rejected.
_VALUE_NAME_RE = re.compile(
    r"^(?:blue|red|green|gray|grey|amber|purple|pink|orange|yellow|teal|"
    r"cyan|indigo|violet|lime|stone|zinc|slate|neutral|black|white)"
    r"[-_]?\d{2,3}$",
    re.IGNORECASE,
)
_HEX_NAME_RE = re.compile(r"^#?[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_REFERENCE_RE = re.compile(r"^\{([A-Za-z0-9_.-]+)\}$")
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_PX_RE = re.compile(r"([\d.]+)px")

_GENERATED_HEADER = (
    "/* Generated from tokens.json by gateway.web_designer.tokens.generate_css"
    " — DO NOT EDIT BY HAND. */"
)


def flatten_token_leaves(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map dotted token path (``semantic.color.text.primary``) to its leaf.

    A leaf is a dict carrying a ``"value"`` key. Group dicts without a
    value are walked, not returned. Non-dict sections are ignored.
    """
    out: dict[str, dict[str, Any]] = {}

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        if "value" in node:
            out[prefix] = node
            return
        for key, child in node.items():
            if key.startswith("$"):
                continue
            walk(child, f"{prefix}.{key}" if prefix else str(key))

    for tier in TIERS:
        walk(data.get(tier), tier)
    return out


def _is_reference(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _REFERENCE_RE.match(value.strip())
    return match.group(1) if match else None


def _check_spacing(path: str, value: Any, errors: list[str]) -> None:
    """Spacing tokens sit on a 4px base; a px spacing value must divide by 4."""
    if "spacing" not in path and "space" not in path:
        return
    if not isinstance(value, str):
        return
    match = _PX_RE.fullmatch(value.strip())
    if not match:
        return
    amount = float(match.group(1))
    if amount % 4 != 0:
        errors.append(
            f"{path}: spacing {value!r} is not on the 4px base "
            "(must be a multiple of 4px)"
        )


def _validate_leaf(
    tier: str, path: str, leaf: dict[str, Any],
    leaves: dict[str, dict[str, Any]], errors: list[str],
) -> None:
    value = leaf.get("value")
    if tier == "primitive":
        if _is_reference(value) is not None:
            errors.append(
                f"{path}: primitive token must hold a raw value, not a "
                f"reference {value!r} (tiers run primitive -> semantic -> "
                "component, never reversed)"
            )
        elif not isinstance(value, (str, int, float)):
            errors.append(f"{path}: primitive token value must be a raw value")
    elif tier == "semantic":
        ref = _is_reference(value)
        if ref is None:
            errors.append(
                f"{path}: semantic token value must be a reference "
                "'{primitive.<path>}' , never a raw value"
            )
        elif not ref.startswith("primitive."):
            errors.append(
                f"{path}: semantic token must reference a primitive token, "
                f"not {ref!r}"
            )
        elif ref not in leaves:
            errors.append(f"{path}: semantic token references missing {ref!r}")
    else:  # component
        ref = _is_reference(value)
        if ref is None:
            errors.append(
                f"{path}: component token value must be a reference "
                "'{semantic.<path>}' , never a raw value or primitive"
            )
        elif ref.startswith("primitive."):
            errors.append(
                f"{path}: component token must reference a semantic token, "
                f"not a primitive {ref!r}"
            )
        elif not ref.startswith("semantic."):
            errors.append(
                f"{path}: component token must reference a semantic token, "
                f"not {ref!r}"
            )
        elif ref not in leaves:
            errors.append(f"{path}: component token references missing {ref!r}")
    _check_spacing(path, value, errors)


def validate_tokens(data: Any) -> list[str]:
    """Return the list of errors in a token tree; empty means valid.

    Checks: three tiers only; reference direction primitive -> semantic
    -> component (never reversed, never raw where a reference belongs);
    references resolve; semantic names are roles, not values; spacing is
    on a 4px base.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["tokens must be a JSON object with primitive/semantic/component tiers"]
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            errors.append(
                f"unexpected top-level key {key!r}: only "
                "primitive/semantic/component (plus optional themes) are allowed"
            )
    for tier in TIERS:
        if tier not in data:
            errors.append(f"missing required tier {tier!r}")
        elif not isinstance(data[tier], dict):
            errors.append(f"tier {tier!r} must be an object of token groups")
    if errors:
        return errors
    leaves = flatten_token_leaves(data)
    for path, leaf in sorted(leaves.items()):
        tier = path.split(".", 1)[0]
        _validate_leaf(tier, path, leaf, leaves, errors)
        if tier == "semantic":
            for segment in path.split(".")[1:]:
                if _VALUE_NAME_RE.match(segment) or (
                    segment.startswith("#")
                    or (len(segment) == 6 and _HEX_NAME_RE.match(segment))
                ):
                    errors.append(
                        f"{path}: semantic token is named by value "
                        f"({segment!r}); name by role, e.g. color.text.primary"
                    )
                    break
    return errors


def _resolve_value(
    path: str, leaves: dict[str, dict[str, Any]], seen: frozenset[str],
) -> str:
    if path in seen:
        raise ValueError(f"circular token reference at {path!r}")
    leaf = leaves.get(path)
    if leaf is None:
        raise ValueError(f"token reference {path!r} does not resolve")
    value = leaf.get("value")
    ref = _is_reference(value)
    if ref is None:
        return str(value)
    return _resolve_value(ref, leaves, seen | {path})


def _css_var_name(path: str) -> str:
    """``semantic.color.text.primary`` -> ``--color-text-primary``."""
    parts = path.split(".")[1:]  # strip the tier
    return "--" + "-".join(parts)


def _flatten_dark_overrides(themes: Any) -> dict[str, Any]:
    """Normalise the dark theme to dotted-path -> value.

    Accepts either a flat ``{"semantic.color.surface.base": "#121212"}``
    mapping or a nested token tree.
    """
    dark = themes.get("dark") if isinstance(themes, dict) else None
    if not isinstance(dark, dict):
        return {}
    flat: dict[str, Any] = {}
    for key, value in dark.items():
        if isinstance(value, dict) and "value" not in value:
            for sub, leaf in flatten_token_leaves({"semantic": value}).items():
                flat[sub] = leaf.get("value")
        else:
            flat[key] = value.get("value") if isinstance(value, dict) else value
    return flat


def generate_css(tokens: dict[str, Any]) -> str:
    """Render CSS variables from a token tree.

    Light theme is the default ``:root`` block. A dark theme, when
    present under ``themes.dark``, is emitted as a remapped
    ``@media (prefers-color-scheme: dark)`` block. References resolve to
    their primitive values. The output is generated — never hand-edited.
    """
    errors = validate_tokens(tokens)
    if errors:
        raise ValueError("invalid tokens: " + "; ".join(errors))
    leaves = flatten_token_leaves(tokens)
    lines = [_GENERATED_HEADER, ":root {"]
    for path in sorted(leaves):
        lines.append(f"  {_css_var_name(path)}: {_resolve_value(path, leaves, frozenset())};")
    lines.append("}")
    dark = _flatten_dark_overrides(tokens.get("themes"))
    if dark:
        lines.append("@media (prefers-color-scheme: dark) {")
        lines.append("  :root {")
        for path in sorted(dark):
            lookup = path if path.split(".", 1)[0] in TIERS else f"semantic.{path}"
            raw = dark[path]
            ref = _is_reference(raw)
            if ref is not None:
                value = _resolve_value(ref, leaves, frozenset())
            else:
                value = str(raw)
            lines.append(f"    {_css_var_name(lookup)}: {value};")
        lines.append("  }")
        lines.append("}")
    return "\n".join(lines) + "\n"


def load_tokens(path: str | Path) -> dict[str, Any]:
    """Load and parse a tokens.json file; ValueError on missing/bad JSON."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load tokens from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"cannot load tokens from {path}: not a JSON object")
    return data


def save_tokens(run_dir: str | Path, data: dict[str, Any]) -> Path:
    """Validate and write the run's single tokens.json (0600, atomic).

    One source file per run: the tokens live only here, and the generated
    CSS is derived from it. Invalid tokens raise ValueError and write
    nothing.
    """
    errors = validate_tokens(data)
    if errors:
        raise ValueError("invalid tokens: " + "; ".join(errors))
    base = Path(run_dir)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / "tokens.json"
    atomic_json_write(path, data, mode=0o600)
    return path


def _token_value_sets(tokens: dict[str, Any] | None) -> tuple[set[str], set[str], set[str]]:
    """(hex colours, font-size px values, spacing px values) used by tokens."""
    colours: set[str] = set()
    font_sizes: set[str] = set()
    spacings: set[str] = set()
    if not tokens:
        return colours, font_sizes, spacings
    leaves = flatten_token_leaves(tokens)
    for path, leaf in leaves.items():
        try:
            value = _resolve_value(path, leaves, frozenset())
        except ValueError:
            continue
        lowered = value.strip().lower()
        match = _HEX_RE.search(lowered)
        if match:
            colours.add(_normalise_hex(match.group(0)))
        px = _PX_RE.fullmatch(lowered)
        if px:
            if "font" in path or "size" in path:
                font_sizes.add(str(float(px.group(1))))
            if "spacing" in path or "space" in path:
                spacings.add(str(float(px.group(1))))
    return colours, font_sizes, spacings


def _normalise_hex(raw: str) -> str:
    raw = raw.lower()
    if len(raw) == 4:  # #rgb -> #rrggbb
        return "#" + "".join(ch * 2 for ch in raw[1:])
    return raw


def _is_generated_css(text: str, name: str) -> bool:
    return "generated" in name.lower() or "DO NOT EDIT BY HAND" in text[:500]


def token_drift(project_dir: str | Path, tokens: dict[str, Any] | None) -> list[Finding]:
    """Scan component/source files for literals that bypass the tokens.

    Raw hex colours, raw px font sizes and raw spacing literals are drift
    when no token holds that value. A literal appearing twice is a defect;
    once is a change-level finding. Generated CSS is excluded — it is
    derived from the tokens, not a bypass.
    """
    root = Path(project_dir)
    colours, font_sizes, spacings = _token_value_sets(tokens)
    counts: dict[tuple[str, str], int] = {}
    first_seen: dict[tuple[str, str], str] = {}
    exts = {".html", ".css", ".js", ".ts", ".tsx", ".jsx"}
    if not root.is_dir():
        return []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix == ".css" and _is_generated_css(text, path.name):
            continue
        rel = str(path.relative_to(root))
        for lineno, line in enumerate(text.splitlines(), start=1):
            clean = re.sub(r"/\*.*?\*/", "", line)
            for raw in _HEX_RE.findall(clean):
                key = ("hex", _normalise_hex(raw))
                _note(counts, first_seen, key, f"{rel}:{lineno}")
            size = re.search(r"font-size\s*:\s*([\d.]+)px", clean)
            if size:
                _note(counts, first_seen, ("font", str(float(size.group(1)))),
                      f"{rel}:{lineno}")
            for match in re.finditer(
                r"(?:margin|padding|gap|row-gap|column-gap)\s*:[^;{]*?([\d.]+)px",
                clean,
            ):
                _note(counts, first_seen, ("spacing", str(float(match.group(1)))),
                      f"{rel}:{lineno}")
    findings: list[Finding] = []
    for (kind, literal), count in sorted(counts.items()):
        tokenised = {
            "hex": colours, "font": font_sizes, "spacing": spacings,
        }[kind]
        if literal in tokenised:
            continue
        rule, label = {
            "hex": ("R027", f"raw hex colour {literal}"),
            "font": ("R012", f"raw font size {literal}px"),
            "spacing": ("R028", f"raw spacing {literal}px"),
        }[kind]
        findings.append(Finding(
            rule_id=rule, judger="visual",
            severity="defect" if count >= 2 else "change",
            location=first_seen[(kind, literal)],
            expected="value comes from a design token (a literal appearing "
                     "twice becomes a token)",
            actual=f"{label} appears {count} time(s) with no matching token",
            evidence=f"{first_seen[(kind, literal)]} ({count} occurrence(s))",
        ))
    return findings


def _note(counts: dict[tuple[str, str], int],
          first_seen: dict[tuple[str, str], str],
          key: tuple[str, str], location: str) -> None:
    counts[key] = counts.get(key, 0) + 1
    if key not in first_seen:
        first_seen[key] = location
