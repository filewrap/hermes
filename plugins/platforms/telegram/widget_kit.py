"""Pre-made precise widgets and their rich-message equivalents (F10).

The external agent's ``show_widget`` takes free-form HTML; Hermes generalises the
Telegram widget pipeline the other way: a small set of precise, named
templates whose data shapes are validated here, so the model sends a
``metric`` or a ``flow`` instead of hand-writing HTML. Each template builds
two surfaces from the same data:

* the widget HTML for a DM — inline SVG for mermaid flows, ``<canvas>``
  plus compact inline JavaScript for charts, Telegram theme variables
  throughout;
* the Rich HTML for any other chat — headings, tables, ``<details>``
  foldouts and ``<pre>`` blocks through the existing rich engine, so the
  same content arrives in a group as a precise rich message.

Widgets never leave a DM: the transport refuses widget sends to any other
chat (``widget_transport._widget_peer_or_refusal``), and the
``widget_show`` action routes on the resolved peer, so a group always gets
the rich surface. The dashboard half (``widget_put`` on a board tab) is
out of scope here — it needs F9 boards.

This module is pure construction + validation: no transport, no rendering
(the recipient's beta client renders the widget; Telegram renders the rich
message). Every builder escapes user data; every widget HTML goes through
``validate_widget_html`` before it leaves this module.
"""
from __future__ import annotations

import html as _html
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

TEMPLATES = ("metric", "progress", "checklist", "flow", "chart", "status")

MAX_TEMPLATE_TITLE_CHARS = 128
MAX_TEMPLATE_LABEL_CHARS = 128
MAX_TEMPLATE_NOTE_CHARS = 512
MAX_TEMPLATE_ROWS = 24
MAX_MERMAID_CHARS = 4_000

# Shared widget stylesheet: compact on purpose — the whole document must
# stay under the 4,096-byte zlib limit. Theme variables come from the
# recipient client (timer.html uses the same names); the fallbacks are
# what a client without them shows.
_WIDGET_CSS = (
    "body{font-family:sans-serif;background:var(--tg-theme-bg-color,#fff);"
    "color:var(--tg-theme-text-color,#000);margin:0;padding:12px}"
    "h1{font-size:16px;margin:0 0 8px}"
    "p{font-size:14px;margin:0 0 8px}.mut{color:var(--tg-theme-hint-color,#888)}"
    ".big{font-size:32px;font-weight:700;margin:4px 0}"
    ".chip{display:inline-block;border-radius:10px;padding:2px 8px;font-size:12px;"
    "background:var(--tg-theme-secondary-bg-color,#eef);"
    "color:var(--tg-theme-accent-text-color,#2481cc)}"
    ".bar{height:10px;border-radius:5px;background:var(--tg-theme-secondary-bg-color,#e8e8e8)}"
    ".bar>i{display:block;height:10px;border-radius:5px;"
    "background:var(--tg-theme-button-color,#2481cc)}"
    "table{border-collapse:collapse;font-size:14px;width:100%}"
    "td,th{padding:4px 6px;text-align:left;vertical-align:top}"
    "th{color:var(--tg-theme-hint-color,#888);font-weight:600}"
    ".ok{color:var(--tg-theme-accent-text-color,#2481cc)}"
    ".nd{fill:var(--tg-theme-secondary-bg-color,#eef);stroke:var(--tg-theme-button-color,#2481cc)}"
    ".nt{fill:var(--tg-theme-text-color,#000);font:13px sans-serif}"
    ".eg{stroke:var(--tg-theme-hint-color,#999);fill:none}"
    ".el{fill:var(--tg-theme-hint-color,#888);font:11px sans-serif}"
)


def _esc(value: Any) -> str:
    return _html.escape(str(value), quote=True)


def _shell(title: str, body: str) -> str:
    """Wrap a body fragment in the widget document shell."""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_WIDGET_CSS}</style></head><body>"
        f"<h1>{_esc(title)}</h1>{body}</body></html>"
    )


def _require_str(data: dict[str, Any], key: str, *, maximum: int,
                 required: bool = True) -> str | None:
    value = data.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _require_items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = data.get(key)
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TEMPLATE_ROWS:
        raise ValueError(f"{key} must be a list of 1–{MAX_TEMPLATE_ROWS} items")
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"each {key} item must be an object")
        cleaned.append(item)
    return cleaned


def validate_template_data(template: Any, data: Any) -> dict[str, Any]:
    """Validate and clean one template's data, or raise ValueError.

    The shapes are the contract the telegram-visualize skill documents;
    the builders below trust the cleaned dict.
    """
    name = str(template or "").strip().lower()
    if name not in TEMPLATES:
        raise ValueError(f"template must be one of {', '.join(TEMPLATES)}")
    if not isinstance(data, dict):
        raise ValueError("data must be an object for the template")
    if name == "metric":
        return {
            "label": _require_str(data, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "value": _require_str(data, "value", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "delta": _require_str(data, "delta", maximum=MAX_TEMPLATE_LABEL_CHARS, required=False),
            "note": _require_str(data, "note", maximum=MAX_TEMPLATE_NOTE_CHARS, required=False),
        }
    if name == "progress":
        done = data.get("done")
        total = data.get("total")
        if type(done) is not int or type(total) is not int:
            raise ValueError("progress needs integer done and total")
        if total < 1 or total > 1000 or done < 0 or done > total:
            raise ValueError("progress needs 0 <= done <= total, total 1–1000")
        steps = []
        for item in _require_items(data, "steps") if "steps" in data else []:
            steps.append({
                "label": _require_str(item, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
                "done": item.get("done") is True,
            })
        return {
            "label": _require_str(data, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "done": done, "total": total, "steps": steps,
        }
    if name == "checklist":
        items = [{
            "label": _require_str(item, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "done": item.get("done") is True,
        } for item in _require_items(data, "items")]
        return {
            "label": _require_str(data, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "items": items,
        }
    if name == "flow":
        mermaid = _require_str(data, "mermaid", maximum=MAX_MERMAID_CHARS)
        parse_mermaid_flowchart(mermaid)  # raises on unparseable source
        return {"mermaid": mermaid}
    if name == "chart":
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in {"bar", "line"}:
            raise ValueError("chart kind must be bar or line")
        labels = data.get("labels")
        values = data.get("values")
        if (not isinstance(labels, list) or not isinstance(values, list)
                or not 2 <= len(labels) <= MAX_TEMPLATE_ROWS
                or len(labels) != len(values)):
            raise ValueError("chart needs 2–24 labels and the same number of values")
        cleaned_labels = [_require_str({"label": x}, "label", maximum=MAX_TEMPLATE_LABEL_CHARS)
                          for x in labels]
        cleaned_values = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("chart values must be numbers")
            if not math.isfinite(value):
                raise ValueError("chart values must be finite numbers")
            cleaned_values.append(value)
        return {
            "label": _require_str(data, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
            "kind": kind, "labels": cleaned_labels, "values": cleaned_values,
            "unit": _require_str(data, "unit", maximum=32, required=False),
        }
    # status
    rows = [{
        "name": _require_str(item, "name", maximum=MAX_TEMPLATE_LABEL_CHARS),
        "state": _require_str(item, "state", maximum=32),
        "detail": _require_str(item, "detail", maximum=MAX_TEMPLATE_NOTE_CHARS, required=False),
    } for item in _require_items(data, "rows")]
    return {
        "label": _require_str(data, "label", maximum=MAX_TEMPLATE_LABEL_CHARS),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Mermaid flowchart (subset) -> SVG
# ---------------------------------------------------------------------------

@dataclass
class FlowNode:
    """One flowchart node: id, label, shape."""

    node_id: str
    label: str
    shape: str = "rect"  # rect | stadium | rounded | circle | diamond


@dataclass
class FlowEdge:
    """One directed edge: source, target, optional label, line style."""

    source: str
    target: str
    label: str = ""
    style: str = "solid"  # solid | dotted | thick


@dataclass
class FlowGraph:
    """A parsed flowchart: direction, nodes in first-seen order, edges."""

    direction: str  # TD | LR
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    edges: list[FlowEdge] = field(default_factory=list)
    order: list[str] = field(default_factory=list)


_DIRECTION_RE = re.compile(r"^(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)\s*$")

# Mermaid statements the subset does not implement. A statement starting
# with one is refused instead of being misread as node definitions (a
# ``subgraph`` line would otherwise parse as two bare nodes).
_UNSUPPORTED_MERMAID_KEYWORDS = frozenset({
    "subgraph", "end", "class", "classDef", "style", "click",
    "direction", "linkStyle",
})


def _read_node(text: str, pos: int) -> tuple[str, str, str, int] | None:
    """Read ``ID[shape]`` at pos; return (id, shape, label, end) or None."""
    match = re.match(r"[A-Za-z0-9_]+", text[pos:])
    if not match:
        return None
    node_id = match.group(0)
    i = pos + len(node_id)
    while i < len(text) and text[i] == " ":
        i += 1
    rest = text[i:]
    for prefix, closer, shape in (
            ("[[", "]]", "rect"), ("[", "]", "rect"),
            ("((", "))", "circle"), ("([", "])", "stadium"),
            ("(", ")", "rounded"), ("{", "}", "diamond"),
            ("[/", "/]", "rect"), ("[\\", "\\]", "rect")):
        if rest.startswith(prefix):
            end = rest.find(closer, len(prefix))
            if end < 0:
                raise ValueError(f"unclosed node shape after {node_id!r}")
            label = rest[len(prefix):end].strip().strip("\"'")
            return node_id, shape, label or node_id, i + end + len(closer)
    return node_id, "rect", node_id, i


def _read_edge(text: str, pos: int) -> tuple[str, str, int] | None:
    """Read an edge operator at pos; return (style, label, end) or None."""
    rest = text[pos:]
    for prefix, style in (("-->", "solid"), ("-.->", "dotted"),
                          ("==>", "thick"), ("---", "solid")):
        if rest.startswith(prefix):
            return style, "", pos + len(prefix)
    for pattern, style in (
            (r"^--\s+([^<>]+?)\s*-->", "solid"),
            (r"^-\.\s+([^<>]+?)\s*\.\->", "dotted"),
            (r"^==\s+([^<>]+?)\s*==>", "thick")):
        match = re.match(pattern, rest)
        if match:
            return style, match.group(1).strip(), pos + len(match.group(0))
    return None


def _split_statements(line: str) -> list[str]:
    """Split a line on ``;`` separators that sit outside node shapes.

    A naive ``split(";")`` breaks labels that contain ``;`` — an HTML
    entity such as ``&amp;`` inside ``A[<b>&amp;</b>]`` would be cut in
    half and the shape then reads as unclosed. Bracket depth and double
    quotes are tracked so only top-level separators split.
    """
    parts: list[str] = []
    depth = 0
    in_quote = False
    start = 0
    for pos, char in enumerate(line):
        if char == '"' and (pos == 0 or line[pos - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote and char in "[({":
            depth += 1
        elif not in_quote and char in "])}":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0 and not in_quote:
            parts.append(line[start:pos])
            start = pos + 1
    parts.append(line[start:])
    return parts


def parse_mermaid_flowchart(source: Any) -> FlowGraph:
    """Parse the flowchart subset into a FlowGraph, or raise ValueError.

    Supported: ``flowchart TD|LR`` (``graph`` and TB/RL/BT spellings map
    onto the same two directions), node shapes ``[]`` ``[[]]`` ``()``
    ``([])`` ``(())`` ``{}`` ``[/ /]``, edges ``-->`` ``---`` ``-.->``
    ``==>`` with optional ``-- label -->`` text, chains (``A --> B --> C``)
    and ``;`` separators, ``%%`` comments. Anything else — subgraphs,
    class lines, ``&`` joins, other diagram kinds — is refused with the
    line that failed, instead of rendering a wrong diagram.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("mermaid must be a non-empty flowchart source")
    lines = [line.split("%%", 1)[0].rstrip() for line in source.splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        raise ValueError("mermaid must be a non-empty flowchart source")
    direction_match = _DIRECTION_RE.match(lines[0].strip())
    if not direction_match:
        raise ValueError("mermaid must start with 'flowchart TD' or 'flowchart LR'")
    direction = direction_match.group(1)
    if direction in {"TB", "BT"}:
        direction = "TD"
    elif direction in {"RL"}:
        direction = "LR"
    graph = FlowGraph(direction=direction)
    reversed_flow = direction_match.group(1) in {"BT", "RL"}
    for line in lines[1:]:
        for statement in _split_statements(line):
            statement = statement.strip()
            if not statement:
                continue
            first = statement.split(None, 1)[0]
            if first in _UNSUPPORTED_MERMAID_KEYWORDS:
                raise ValueError(
                    f"mermaid {first!r} is not supported; restate the flow "
                    "in the supported subset")
            _parse_statement(graph, statement)
    if not graph.nodes:
        raise ValueError("mermaid flowchart has no nodes")
    if reversed_flow:
        # BT/RL lay out like TD/LR, then the renderer mirrors the canvas.
        graph.direction = f"{direction}R"
    return graph


def _parse_statement(graph: FlowGraph, statement: str) -> None:
    """Parse one statement: nodes joined by edges, in scan order."""
    items: list[tuple[str, Any]] = []
    i = 0
    while i < len(statement):
        while i < len(statement) and statement[i] == " ":
            i += 1
        if i >= len(statement):
            break
        node = _read_node(statement, i)
        if node is not None:
            node_id, shape, label, i = node
            items.append(("node", (node_id, shape, label)))
            continue
        edge = _read_edge(statement, i)
        if edge is not None:
            style, label, i = edge
            items.append(("edge", (style, label)))
            continue
        raise ValueError(f"cannot parse mermaid near {statement[i:i + 12]!r} in {statement!r}")
    last_node: str | None = None
    pending: tuple[str, str] | None = None
    for kind, value in items:
        if kind == "node":
            node_id, shape, label = value
            if node_id not in graph.nodes:
                graph.nodes[node_id] = FlowNode(node_id, label, shape)
                graph.order.append(node_id)
            if pending is not None:
                if last_node is None:
                    raise ValueError(f"edge without a source node in {statement!r}")
                style, edge_label = pending
                graph.edges.append(FlowEdge(last_node, node_id, edge_label, style))
                pending = None
            last_node = node_id
        else:
            if pending is not None or last_node is None:
                raise ValueError(f"edge without a source node in {statement!r}")
            pending = value
    if pending is not None:
        raise ValueError(f"edge without a target node in {statement!r}")


def _node_size(node: FlowNode) -> tuple[int, int]:
    width = max(56, int(len(node.label) * 7.5) + 28)
    return width, 60 if node.shape == "diamond" else 40


def _layout(graph: FlowGraph) -> tuple[dict[str, tuple[int, int]], int, int]:
    """Layered layout: ranks by longest path, first-seen order inside a rank.

    Returns ({node_id: (cx, cy)}, width, height). Cycles fall back to
    first-seen order as ranks, so a cyclic flow still renders instead of
    looping.
    """
    rank: dict[str, int] = {node_id: 0 for node_id in graph.order}
    for _ in range(len(graph.order) + 1):
        changed = False
        for edge in graph.edges:
            if rank[edge.target] < rank[edge.source] + 1:
                rank[edge.target] = rank[edge.source] + 1
                changed = True
        if not changed:
            break
    else:
        rank = {node_id: i for i, node_id in enumerate(graph.order)}
    by_rank: dict[int, list[str]] = {}
    for node_id in graph.order:
        by_rank.setdefault(rank[node_id], []).append(node_id)
    horizontal = graph.direction.startswith("LR")
    positions: dict[str, tuple[int, int]] = {}
    if horizontal:
        x = 16
        max_height = 0
        for level in sorted(by_rank):
            ids = by_rank[level]
            width = max(_node_size(graph.nodes[n])[0] for n in ids)
            y = 16
            for node_id in ids:
                _, height = _node_size(graph.nodes[node_id])
                positions[node_id] = (x + width // 2, y + height // 2)
                y += height + 28
                max_height = max(max_height, y)
            x += width + 48
        return positions, x + 16, max(max_height, 72)
    y = 16
    max_width = 0
    for level in sorted(by_rank):
        ids = by_rank[level]
        height = max(_node_size(graph.nodes[n])[1] for n in ids)
        x = 16
        for node_id in ids:
            width, _ = _node_size(graph.nodes[node_id])
            positions[node_id] = (x + width // 2, y + height // 2)
            x += width + 28
            max_width = max(max_width, x)
        y += height + 48
    return positions, max(max_width, 72), y + 16


def render_mermaid_svg(source: str) -> str:
    """Render a mermaid flowchart source as inline SVG (deterministic)."""
    graph = parse_mermaid_flowchart(source)
    positions, width, height = _layout(graph)
    mirror_x = graph.direction == "LRR"
    mirror_y = graph.direction == "TDR"
    if mirror_x or mirror_y:
        mirrored = {}
        for node_id, (cx, cy) in positions.items():
            mirrored[node_id] = (
                width - cx if mirror_x else cx,
                height - cy if mirror_y else cy,
            )
        positions = mirrored
    parts = [
        f"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 {width} {height}\" "
        "width=\"100%\" role=\"img\"><defs><marker id=\"ar\" viewBox=\"0 0 10 10\" "
        "refX=\"9\" refY=\"5\" markerWidth=\"7\" markerHeight=\"7\" orient=\"auto-start-reverse\">"
        "<path d=\"M0 0L10 5L0 10z\" fill=\"var(--tg-theme-hint-color,#999)\"/></marker></defs>",
    ]
    horizontal = graph.direction.startswith("LR")
    for edge in graph.edges:
        sx, sy = positions[edge.source]
        tx, ty = positions[edge.target]
        sw, sh = _node_size(graph.nodes[edge.source])
        tw, th = _node_size(graph.nodes[edge.target])
        if horizontal:
            x1 = sx + sw // 2 if tx >= sx else sx - sw // 2
            x2 = tx - tw // 2 if tx >= sx else tx + tw // 2
            y1, y2 = sy, ty
        else:
            x1, x2 = sx, tx
            y1 = sy + sh // 2 if ty >= sy else sy - sh // 2
            y2 = ty - th // 2 if ty >= sy else ty + th // 2
        dash = " stroke-dasharray=\"4 3\"" if edge.style == "dotted" else ""
        thick = " stroke-width=\"2\"" if edge.style == "thick" else ""
        if (horizontal and sy == ty) or (not horizontal and sx == tx):
            parts.append(f"<line x1=\"{x1}\" y1=\"{y1}\" x2=\"{x2}\" y2=\"{y2}\" "
                         f"class=\"eg\"{dash}{thick} marker-end=\"url(#ar)\"/>")
        else:
            mid_x, mid_y = (x2, y1) if horizontal else (x1, y2)
            parts.append(f"<polyline points=\"{x1},{y1} {mid_x},{mid_y} {x2},{y2}\" "
                         f"class=\"eg\"{dash}{thick} marker-end=\"url(#ar)\"/>")
        if edge.label:
            parts.append(f"<text x=\"{(x1 + x2) // 2}\" y=\"{(y1 + y2) // 2}\" "
                         f"text-anchor=\"middle\" class=\"el\">{_esc(edge.label)}</text>")
    for node_id in graph.order:
        node = graph.nodes[node_id]
        cx, cy = positions[node_id]
        width_n, height_n = _node_size(node)
        label = _esc(node.label)
        if node.shape == "diamond":
            points = (f"{cx},{cy - height_n // 2} {cx + width_n // 2},{cy} "
                      f"{cx},{cy + height_n // 2} {cx - width_n // 2},{cy}")
            parts.append(f"<polygon points=\"{points}\" class=\"nd\"/>")
        elif node.shape == "circle":
            parts.append(f"<ellipse cx=\"{cx}\" cy=\"{cy}\" rx=\"{width_n // 2}\" "
                         f"ry=\"{height_n // 2}\" class=\"nd\"/>")
        else:
            rx = height_n // 2 if node.shape == "stadium" else 10 if node.shape == "rounded" else 6
            parts.append(f"<rect x=\"{cx - width_n // 2}\" y=\"{cy - height_n // 2}\" "
                         f"width=\"{width_n}\" height=\"{height_n}\" rx=\"{rx}\" class=\"nd\"/>")
        parts.append(f"<text x=\"{cx}\" y=\"{cy + 4}\" text-anchor=\"middle\" "
                     f"class=\"nt\">{label}</text>")
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Widget builders (DM surface)
# ---------------------------------------------------------------------------

def _text_bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "░" * (width - filled)


_CHART_JS = (
    "(function(){var D=%s;var c=document.getElementById('wc');"
    "if(!c||!c.getContext)return;var x=c.getContext('2d');"
    "var W=c.width,H=c.height,pl=34,pr=8,pt=10,pb=24;"
    "var vals=D.v,max=Math.max.apply(null,vals.concat([1]));"
    "var cs=getComputedStyle(document.body);"
    "var ac=(cs.getPropertyValue('--tg-theme-accent-text-color')||'#2481cc').trim();"
    "var tc=(cs.getPropertyValue('--tg-theme-text-color')||'#000').trim();"
    "x.font='11px sans-serif';x.fillStyle=tc;x.strokeStyle='#bbb';"
    "x.beginPath();x.moveTo(pl,pt);x.lineTo(pl,H-pb);x.lineTo(W-pr,H-pb);x.stroke();"
    "var bw=(W-pl-pr)/vals.length;"
    "if(D.k==='bar'){for(var i=0;i<vals.length;i++){var h=(H-pt-pb)*vals[i]/max;"
    "x.fillStyle=ac;x.fillRect(pl+i*bw+4,H-pb-h,bw-8,h);"
    "x.fillStyle=tc;x.fillText(D.l[i],pl+i*bw+4,H-8);}}"
    "else{x.strokeStyle=ac;x.lineWidth=2;x.beginPath();"
    "for(var i2=0;i2<vals.length;i2++){var px=pl+i2*bw+bw/2;"
    "var py=H-pb-(H-pt-pb)*vals[i2]/max;"
    "if(i2===0)x.moveTo(px,py);else x.lineTo(px,py);}"
    "x.stroke();for(var j=0;j<vals.length;j++){var qx=pl+j*bw+bw/2;"
    "var qy=H-pb-(H-pt-pb)*vals[j]/max;x.fillStyle=ac;"
    "x.beginPath();x.arc(qx,qy,3,0,7);x.fill();"
    "x.fillStyle=tc;x.fillText(D.l[j],pl+j*bw+4,H-8);}}})();"
)


def _widget_body_metric(data: dict[str, Any]) -> str:
    delta = f" <span class=\"chip\">{_esc(data['delta'])}</span>" if data.get("delta") else ""
    note = f"<p class=\"mut\">{_esc(data['note'])}</p>" if data.get("note") else ""
    return (f"<p>{_esc(data['label'])}{delta}</p>"
            f"<p class=\"big\">{_esc(data['value'])}</p>{note}")


def _widget_body_progress(data: dict[str, Any]) -> str:
    pct = round(100 * data["done"] / data["total"])
    steps = "".join(
        f"<tr><td>{'✓' if s['done'] else '○'}</td><td>{_esc(s['label'])}</td></tr>"
        for s in data.get("steps", []))
    table = f"<table>{steps}</table>" if steps else ""
    return (f"<p>{_esc(data['label'])} — {data['done']}/{data['total']} ({pct}%)</p>"
            f"<div class=\"bar\"><i style=\"width:{pct}%\"></i></div>{table}")


def _widget_body_checklist(data: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{'✓' if item['done'] else '○'}</td><td>{_esc(item['label'])}</td></tr>"
        for item in data["items"])
    done = sum(1 for item in data["items"] if item["done"])
    return (f"<p>{_esc(data['label'])} — {done}/{len(data['items'])} done</p>"
            f"<table>{rows}</table>")


def _widget_body_flow(data: dict[str, Any]) -> str:
    return render_mermaid_svg(data["mermaid"])


def _widget_body_chart(data: dict[str, Any]) -> str:
    payload = json.dumps({"l": data["labels"], "v": data["values"],
                          "k": data["kind"]}, ensure_ascii=True)
    fallback = ", ".join(
        f"{label}: {value}" for label, value in zip(data["labels"], data["values"]))
    unit = f" ({_esc(data['unit'])})" if data.get("unit") else ""
    return (f"<p>{_esc(data['label'])}{unit}</p>"
            "<canvas id=\"wc\" width=\"560\" height=\"200\"></canvas>"
            f"<p class=\"mut\">{_esc(fallback)}</p>"
            f"<script>{_CHART_JS % payload}</script>")


def _widget_body_status(data: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td><b>{_esc(row['name'])}</b></td><td>{_esc(row['state'])}</td>"
        f"<td>{_esc(row['detail']) if row.get('detail') else ''}</td></tr>"
        for row in data["rows"])
    return (f"<p>{_esc(data['label'])}</p>"
            f"<table><tr><th>Name</th><th>State</th><th>Detail</th></tr>{rows}</table>")


# Template name -> widget body builder. An unknown name falls through to
# the status builder, as the old if/elif ladder's else did; callers
# validate the name first (validate_template_data).
_WIDGET_BODY_BUILDERS = {
    "metric": _widget_body_metric,
    "progress": _widget_body_progress,
    "checklist": _widget_body_checklist,
    "flow": _widget_body_flow,
    "chart": _widget_body_chart,
    "status": _widget_body_status,
}


def build_widget_html(template: str, title: str, data: dict[str, Any]) -> str:
    """Build the widget HTML for one template; refuses oversize output.

    Raises ValueError from ``validate_widget_html`` when the document
    breaks the raw/zlib limits — the caller reports that instead of
    sending a widget the client would drop.
    """
    from plugins.platforms.telegram.widget_messages import validate_widget_html

    name = str(template or "").strip().lower()
    builder = _WIDGET_BODY_BUILDERS.get(name, _widget_body_status)
    return validate_widget_html(_shell(title, builder(data)))


# ---------------------------------------------------------------------------
# Rich builders (group surface — same content, rich engine)
# ---------------------------------------------------------------------------

def build_rich_html(template: str, title: str, data: dict[str, Any]) -> str:
    """Build the Rich HTML equivalent of one template for non-DM chats."""
    name = str(template or "").strip().lower()
    head = f"<h1>{_esc(title)}</h1>"
    if name == "metric":
        delta = f" {_esc(data['delta'])}" if data.get("delta") else ""
        note = f"<p>{_esc(data['note'])}</p>" if data.get("note") else ""
        return (f"{head}<p><b>{_esc(data['label'])}</b></p>"
                f"<p><b>{_esc(data['value'])}</b>{delta}</p>{note}")
    if name == "progress":
        pct = round(100 * data["done"] / data["total"])
        steps = "".join(
            f"<tr><td>{'✓' if s['done'] else '○'}</td><td>{_esc(s['label'])}</td></tr>"
            for s in data.get("steps", []))
        table = (f"<table><tr><th>Step</th><th>Done</th></tr>{steps}</table>"
                 if steps else "")
        return (f"{head}<p>{_esc(data['label'])} — {data['done']}/{data['total']} "
                f"({pct}%)</p><pre>{_text_bar(data['done'] / data['total'])}</pre>{table}")
    if name == "checklist":
        rows = "".join(
            f"<tr><td>{'✓' if item['done'] else '○'}</td><td>{_esc(item['label'])}</td></tr>"
            for item in data["items"])
        done = sum(1 for item in data["items"] if item["done"])
        return (f"{head}<p>{_esc(data['label'])} — {done}/{len(data['items'])} done</p>"
                f"<table><tr><th>Done</th><th>Item</th></tr>{rows}</table>")
    if name == "flow":
        graph = parse_mermaid_flowchart(data["mermaid"])
        rows = "".join(
            f"<tr><td>{_esc(graph.nodes[e.source].label)}</td><td>→</td>"
            f"<td>{_esc(graph.nodes[e.target].label)}</td>"
            f"<td>{_esc(e.label) if e.label else ''}</td></tr>"
            for e in graph.edges)
        table = (f"<table><tr><th>From</th><th></th><th>To</th><th>Via</th></tr>{rows}</table>"
                 if rows else "<p>Single step.</p>")
        return (f"{head}{table}<details><summary>Mermaid source</summary>"
                f"<pre>{_esc(data['mermaid'])}</pre></details>")
    if name == "chart":
        rows = "".join(
            f"<tr><td>{_esc(label)}</td><td>{value}</td></tr>"
            for label, value in zip(data["labels"], data["values"]))
        peak = max(data["values"])
        bars = "\n".join(
            f"{label[:12]:12} {_text_bar(value / peak, 16)} {value}"
            for label, value in zip(data["labels"], data["values"]))
        unit = f" ({_esc(data['unit'])})" if data.get("unit") else ""
        return (f"{head}<p>{_esc(data['label'])}{unit}</p>"
                f"<table><tr><th>Label</th><th>Value</th></tr>{rows}</table>"
                f"<pre>{_esc(bars)}</pre>")
    rows = "".join(
        f"<tr><td><b>{_esc(row['name'])}</b></td><td>{_esc(row['state'])}</td>"
        f"<td>{_esc(row['detail']) if row.get('detail') else ''}</td></tr>"
        for row in data["rows"])
    return (f"{head}<p>{_esc(data['label'])}</p>"
            f"<table><tr><th>Name</th><th>State</th><th>Detail</th></tr>{rows}</table>")
