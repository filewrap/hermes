"""Tests for the widget size measure (ported from tg-widgets, measure only).

The source tool's ``send()`` half POSTed to the Bot API and is deliberately
not ported: sending a widget is the ``telegram_client`` ``widget_send``
action, called exactly once and never retried.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "skills"
    / "messaging"
    / "telegram-widgets"
    / "scripts"
    / "widget_measure.py"
)


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("widget_measure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_small_widget_is_ok(tool):
    assert tool.measure(b"<p>hello</p>")["verdict"] == "ok"


def test_incompressible_html_is_too_large(tool):
    assert tool.measure(os.urandom(5000))["verdict"] == "too_large"


def test_over_html_limit_is_too_large_even_if_it_compresses(tool):
    assert tool.measure(b"a" * 40000)["verdict"] == "too_large"


def test_near_limit_widget_is_flagged(tool):
    # Seeded incompressible bytes under the raw limit but over the 3,800-byte
    # compressed target: the measure must warn before it tips over.
    import random

    assert tool.measure(random.Random(7).randbytes(3900))["verdict"] == "near_limit"


def test_examples_fit(tool):
    examples = SCRIPT.parents[1] / "examples"
    assert list(examples.glob("*.html")), "no example widgets shipped"
    for example in examples.glob("*.html"):
        assert tool.measure(example.read_bytes())["verdict"] != "too_large", example
