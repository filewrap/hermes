#!/usr/bin/env python3
"""Build the node-connect presentation from a ``nodes`` diagnose result.

Reads the diagnose JSON (a file path argument, or stdin) and prints one
JSON object: the mermaid flow for the four connection steps (route → auth
→ pairing → reconnect, each labelled with its state) and the
``widget_show`` params that send it — the flow widget in a DM, the same
content as a rich message anywhere else.

Usage:
    python node_diagnosis_flow.py diagnosis.json
    echo '<diagnose json>' | python node_diagnosis_flow.py

Stdlib only; no network, no Telegram call. The widget itself is validated
again at send time by the widget kit, which refuses a flow it cannot
render — this script only shapes the data, it never sends anything.
"""

from __future__ import annotations

import html as _html
import json
import sys

_STEP_ORDER = ("route", "auth", "pairing", "reconnect")
_STEP_LABELS = {
    "route": "Route",
    "auth": "Auth",
    "pairing": "Pairing",
    "reconnect": "Reconnect",
}
_STEP_IDS = {"route": "R", "auth": "A", "pairing": "P", "reconnect": "C"}

_VERDICTS = {
    "healthy": "Healthy",
    "unreachable": "Unreachable",
    "auth_failed": "Auth failed",
    "node_error": "Node error",
    "pairing_pending": "Pairing pending",
    "not_paired": "Not paired",
}

# Copy buttons cap the value at 256 characters (rich engine limit).
_MAX_COPY_CHARS = 250
_MAX_TITLE_CHARS = 128


def _fail(message: str) -> SystemExit:
    return SystemExit(f"node_diagnosis_flow: {message}")


def _node_name(diagnosis: dict) -> str:
    node = diagnosis.get("node")
    if isinstance(node, dict):
        return str(node.get("name") or node.get("nodeId") or "node")
    return str(node or "node")


def build_mermaid(steps: list[dict]) -> str:
    """The four steps as one ``flowchart LR`` chain, states in the labels."""
    by_name = {step.get("name"): step for step in steps}
    nodes = []
    for name in _STEP_ORDER:
        step = by_name.get(name)
        if step is None:
            raise _fail(f"diagnose result is missing the {name} step")
        state = str(step.get("state") or "unknown")
        nodes.append(f"{_STEP_IDS[name]}[{_STEP_LABELS[name]}: {state}]")
    return "flowchart LR\n  " + " --> ".join(nodes)


def build_companion(diagnosis: dict, verdict_label: str, name: str) -> str:
    """Rich HTML for the companion message: verdict, steps, next step."""
    rows = "".join(
        f"<tr><td><b>{_html.escape(_STEP_LABELS[s['name']])}</b></td>"
        f"<td>{_html.escape(str(s.get('state') or 'unknown'))}</td>"
        f"<td>{_html.escape(str(s.get('detail') or ''))}</td></tr>"
        for s in diagnosis["steps"]
    )
    next_step = str(diagnosis.get("next_step") or "")
    return (
        f"<h1>{_html.escape(f'Node connection: {name}')}</h1>"
        f"<p><b>{_html.escape(verdict_label)}</b></p>"
        "<table><tr><th>Step</th><th>State</th><th>Detail</th></tr>"
        f"{rows}</table>"
        f"<p>Next: {_html.escape(next_step)}</p>"
    )


def build_presentation(diagnosis: dict) -> dict:
    """The mermaid source plus the ``widget_show`` params for one diagnosis."""
    if not isinstance(diagnosis, dict) or diagnosis.get("ok") is not True:
        raise _fail("expected a successful diagnose result (ok: true)")
    verdict = str(diagnosis.get("verdict") or "")
    verdict_label = _VERDICTS.get(verdict)
    if verdict_label is None:
        raise _fail(f"unknown verdict {verdict!r}")
    steps = diagnosis.get("steps")
    if not isinstance(steps, list) or [s.get("name") for s in steps] != list(_STEP_ORDER):
        raise _fail("steps must list route, auth, pairing, reconnect in order")
    name = _node_name(diagnosis)
    title = f"Node connection: {name}"[:_MAX_TITLE_CHARS]
    mermaid = build_mermaid(steps)
    copy_value = f"{name}: {verdict_label} — {diagnosis.get('next_step') or ''}"
    copy_value = copy_value[:_MAX_COPY_CHARS]
    buttons = [{"type": "copy", "label": "Copy diagnosis", "value": copy_value}]
    request_id = diagnosis.get("requestId")
    if request_id:
        buttons.append(
            {"type": "copy", "label": "Copy request ID", "value": str(request_id)}
        )
    return {
        "mermaid": mermaid,
        "widget_show_params": {
            "template": "flow",
            "title": title,
            "data": {"mermaid": mermaid},
            "companion": build_companion(diagnosis, verdict_label, name),
            "buttons": buttons,
        },
    }


def main(argv: list[str]) -> int:
    # argv is sys.argv[1:]: zero args reads stdin, one arg is a file path.
    if len(argv) > 1:
        print("usage: node_diagnosis_flow.py [diagnosis.json]", file=sys.stderr)
        return 2
    try:
        if len(argv) == 1:
            with open(argv[0], "r", encoding="utf-8") as handle:
                diagnosis = json.load(handle)
        else:
            diagnosis = json.load(sys.stdin)
        print(json.dumps(build_presentation(diagnosis), ensure_ascii=False))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"node_diagnosis_flow: cannot read diagnosis: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
