"""Behaviour contracts for the F10 widget kit (pre-made widgets + rich equivalents).

Covers the pure builders in ``plugins/platforms/telegram/widget_kit.py``:
the mermaid flowchart subset parses and renders deterministic SVG, every
template builds widget HTML inside the server limits and a Rich HTML
equivalent that the real rich parser accepts, and bad data is refused
with the field that failed.
"""
import pytest

from plugins.platforms.telegram.widget_kit import (
    build_rich_html,
    build_widget_html,
    parse_mermaid_flowchart,
    render_mermaid_svg,
    validate_template_data,
)
from plugins.platforms.telegram.widget_messages import validate_widget_html

FLOW_TD = """flowchart TD
  A[Start] --> B{Check}
  B -- yes --> C[Done]
  B -. no .-> D[Retry]
  D --> B
"""

FLOW_LR = """flowchart LR
  A[Build] --> B[Test]; B --> C[Deploy]
"""


def _rich_parses(html: str) -> str:
    """Parse Rich HTML through the real render path; return its stored html."""
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    parsed = input_rich_message(html, rich_format="html")
    assert type(parsed).__name__ == "InputRichMessageHTML"
    return parsed.html


def test_mermaid_td_parses_nodes_edges_shapes_and_styles():
    graph = parse_mermaid_flowchart(FLOW_TD)
    assert graph.direction == "TD"
    assert [n for n in graph.order] == ["A", "B", "C", "D"]
    assert graph.nodes["A"].label == "Start"
    assert graph.nodes["A"].shape == "rect"
    assert graph.nodes["B"].shape == "diamond"
    assert [(e.source, e.target, e.label, e.style) for e in graph.edges] == [
        ("A", "B", "", "solid"),
        ("B", "C", "yes", "solid"),
        ("B", "D", "no", "dotted"),
        ("D", "B", "", "solid"),
    ]


def test_mermaid_lr_chains_semicolons_and_comments():
    graph = parse_mermaid_flowchart("%% a comment\n" + FLOW_LR)
    assert graph.direction == "LR"
    assert [n for n in graph.order] == ["A", "B", "C"]
    assert len(graph.edges) == 2
    svg = render_mermaid_svg(FLOW_LR)
    assert svg.startswith("<svg")
    assert "Build" in svg and "Deploy" in svg
    assert 'marker-end="url(#ar)"' in svg


def test_mermaid_shapes_render():
    source = ("flowchart TD\n"
              "  A([Stadium]) --> B((Circle))\n"
              "  B --> C(Rounded)\n"
              "  C --> D{Diamond}\n")
    svg = render_mermaid_svg(source)
    assert "<ellipse" in svg  # circle
    assert "<polygon" in svg  # diamond
    assert 'rx="20"' in svg or 'rx="10"' in svg  # stadium / rounded
    for label in ("Stadium", "Circle", "Rounded", "Diamond"):
        assert label in svg


def test_mermaid_is_deterministic_and_escapes_labels():
    source = "flowchart TD\n  A[<b>&amp;</b>] --> B[Done]"
    assert render_mermaid_svg(source) == render_mermaid_svg(source)
    svg = render_mermaid_svg(source)
    assert "<b>" not in svg
    assert "&lt;b&gt;" in svg


def test_mermaid_rejects_bad_sources():
    for bad in ("", "not a flowchart\n  A --> B", "flowchart TD\n  A -->",
                "flowchart TD\n  --> B", "flowchart TD\n  A ~> B",
                "flowchart TD\n  A --> B\n  subgraph X\n  end"):
        with pytest.raises(ValueError):
            parse_mermaid_flowchart(bad)
    with pytest.raises(ValueError, match="no nodes"):
        parse_mermaid_flowchart("flowchart TD\n")


def test_mermaid_cycle_renders_instead_of_looping():
    svg = render_mermaid_svg("flowchart TD\n  A --> B\n  B --> A")
    assert svg.startswith("<svg") and "A" in svg and "B" in svg


@pytest.mark.parametrize("template,data,needles", [
    ("metric", {"label": "Success rate", "value": "99.2%", "delta": "+0.4%",
                "note": "Across 41 deploys."},
     ["99.2%", "Success rate", "+0.4%", "Across 41 deploys."]),
    ("metric", {"label": "Queue", "value": "7"}, ["Queue", "7"]),
    ("progress", {"label": "Migration", "done": 3, "total": 4,
                  "steps": [{"label": "Export", "done": True},
                            {"label": "Import", "done": False}]},
     ["Migration", "3/4", "75%", "Export", "Import"]),
    ("checklist", {"label": "Launch", "items": [{"label": "Tests", "done": True},
                                                {"label": "Docs", "done": False}]},
     ["Launch", "1/2 done", "Tests", "Docs"]),
    ("flow", {"mermaid": FLOW_LR}, ["Build", "Deploy"]),
    ("chart", {"label": "Signups", "kind": "bar",
               "labels": ["Mon", "Tue"], "values": [12, 19]},
     ["Signups", "Mon", "Tue", "12", "19"]),
    ("chart", {"label": "Latency", "kind": "line",
               "labels": ["a", "b", "c"], "values": [1.5, 2, 0.5], "unit": "ms"},
     ["Latency", "ms"]),
    ("status", {"label": "Services",
                "rows": [{"name": "Gateway", "state": "up", "detail": "all regions"},
                         {"name": "Worker", "state": "down"}]},
     ["Services", "Gateway", "up", "Worker", "down"]),
])
def test_every_template_builds_widget_and_rich_from_the_same_data(template, data, needles):
    cleaned = validate_template_data(template, data)
    html = build_widget_html(template, "Title", cleaned)
    assert validate_widget_html(html) == html
    assert "Title" in html
    for needle in needles:
        assert needle in html
    rich = build_rich_html(template, "Title", cleaned)
    parsed = _rich_parses(rich)
    assert "Title" in parsed
    for needle in needles:
        assert needle in parsed


def test_chart_widget_draws_on_canvas_with_a_value_fallback():
    data = validate_template_data(
        "chart", {"label": "Signups", "kind": "bar",
                  "labels": ["Mon", "Tue"], "values": [12, 19]})
    html = build_widget_html("chart", "Signups this week", data)
    assert "<canvas" in html
    assert '"l": ["Mon", "Tue"]' in html
    assert '"v": [12, 19]' in html
    assert "Mon: 12" in html  # fallback text under the canvas
    assert "<script>" in html


def test_flow_widget_embeds_svg_and_rich_keeps_the_source():
    data = validate_template_data("flow", {"mermaid": FLOW_LR})
    html = build_widget_html("flow", "Pipeline", data)
    assert "<svg" in html and "Build" in html
    rich = build_rich_html("flow", "Pipeline", data)
    parsed = _rich_parses(rich)
    assert "Build" in parsed and "Deploy" in parsed
    assert "flowchart LR" in parsed  # foldout keeps the copyable source


def test_template_data_is_refused_with_the_field_that_failed():
    with pytest.raises(ValueError, match="template must be one of"):
        validate_template_data("sparkles", {})
    with pytest.raises(ValueError, match="data must be an object"):
        validate_template_data("metric", ["not", "a", "dict"])
    with pytest.raises(ValueError, match="value is required"):
        validate_template_data("metric", {"label": "x"})
    with pytest.raises(ValueError, match="0 <= done <= total"):
        validate_template_data("progress", {"label": "x", "done": 5, "total": 4})
    with pytest.raises(ValueError, match="same number of values"):
        validate_template_data("chart", {"label": "x", "kind": "bar",
                                         "labels": ["a"], "values": [1, 2]})
    with pytest.raises(ValueError, match="must be numbers"):
        validate_template_data("chart", {"label": "x", "kind": "bar",
                                         "labels": ["a", "b"], "values": [1, "two"]})
    with pytest.raises(ValueError, match="must be finite"):
        validate_template_data("chart", {"label": "x", "kind": "line",
                                         "labels": ["a", "b"], "values": [1, float("inf")]})
    with pytest.raises(ValueError, match="mermaid must start with"):
        validate_template_data("flow", {"mermaid": "sequenceDiagram\n  A->>B: hi"})
    with pytest.raises(ValueError, match="items must be a list"):
        validate_template_data("checklist", {"label": "x", "items": []})


def test_user_data_is_escaped_on_both_surfaces():
    data = validate_template_data(
        "metric", {"label": "A <b>tag</b>", "value": "1 & 2"})
    html = build_widget_html("metric", "T <script>", data)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&lt;b&gt;tag&lt;/b&gt;" in html and "1 &amp; 2" in html
    rich = _rich_parses(build_rich_html("metric", "T <script>", data))
    assert "<script>" not in rich


def test_largest_supported_flow_still_fits_the_widget_limits():
    lines = ["flowchart TD"]
    for i in range(24):
        lines.append(f"  N{i}[Step {i} with a fairly long label] --> N{i + 1}[Next {i + 1}]")
    data = validate_template_data("flow", {"mermaid": "\n".join(lines)})
    assert validate_widget_html(build_widget_html("flow", "Big flow", data))
