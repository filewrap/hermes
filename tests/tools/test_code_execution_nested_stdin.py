#!/usr/bin/env python3
"""A python spawned by a cell's terminal() call can import hermes_tools.

Live failure (2026-11-12, Telegram DM session 20261112_024756_4d55a452): the
model asked execute_code to check telethon via a ``python3 -`` heredoc inside
terminal(); the nested interpreter failed on the heredoc's line 1 with
``ModuleNotFoundError: No module named 'hermes_tools'``, and the outer call
returned error. The stub lives only in the session kernel's staging dir, which
is on the kernel child's PYTHONPATH but never on a terminal-env python's
sys.path — and even with the import fixed by hand, the nested terminal() call
hung to its timeout, because the RPC server served a single client connection
while the kernel held it open.

Both tests drive the REAL kernel + REAL terminal dispatch against a temp
HERMES_HOME (child_env fixture), project mode like the host.
"""

import json

import pytest

from tests.tools import _child_env_fixtures
from tests.tools._child_env_fixtures import run_code
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS

child_env = _child_env_fixtures.child_env


def _cell_with_nested_heredoc(inner: str) -> str:
    """Cell that runs *inner* in a ``python3 -`` heredoc via terminal() and
    prints one JSON doc with the nested run's output and exit code."""
    heredoc = "python3 - <<'PY'\n" + inner + "\nPY"
    return (
        "import json\n"
        "from hermes_tools import terminal\n"
        f"r = terminal({json.dumps(heredoc)}, timeout=45)\n"
        "print(json.dumps({'inner_output': r.get('output', ''), "
        "'inner_exit': r.get('exit_code')}))\n"
    )


@pytest.mark.platforms("posix")
def test_nested_python_via_terminal_imports_and_calls_hermes_tools(child_env):
    """The live shape: nested ``python3 -`` imports hermes_tools and calls
    terminal() back; the reply arrives instead of a ModuleNotFoundError."""
    inner = (
        "from hermes_tools import terminal\n"
        "print(terminal('echo nested-hi', timeout=20))\n"
    )
    out = run_code(_cell_with_nested_heredoc(inner), mode="project",
                   enabled_tools=list(SANDBOX_ALLOWED_TOOLS), reset=True)
    assert out["inner_exit"] == 0, out
    assert "nested-hi" in out["inner_output"], out
    assert "ModuleNotFoundError" not in out["inner_output"], out


@pytest.mark.platforms("posix")
def test_nested_python_imports_all_sandbox_tools(child_env):
    """Every allowed tool (not just terminal) imports from the nested python."""
    names = sorted(SANDBOX_ALLOWED_TOOLS)
    inner = "from hermes_tools import " + ", ".join(names) + "\nprint('imports-ok')\n"
    out = run_code(_cell_with_nested_heredoc(inner), mode="project",
                   enabled_tools=list(SANDBOX_ALLOWED_TOOLS), reset=True)
    assert out["inner_exit"] == 0, out
    assert out["inner_output"].strip() == "imports-ok", out
