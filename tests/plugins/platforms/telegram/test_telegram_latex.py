"""LaTeX macros in ordinary prose reach a Telegram chat as Unicode symbols, not raw backslash text.

Fork port: the fork has no PTB ``TelegramAdapter`` (Telethon only). The
``format_message`` cases drive ``telegram_markdown.to_markdown_v2`` directly
(upstream's ``format_message`` is exactly that wrapper), and the rich-payload
cases drive ``normalize_rich_latex``. The rich-send case
(``test_rich_send_maps_bare_macros_and_keeps_math_for_the_client``) is NOT
ported: it drives a PTB ``bot.do_api_request`` ``sendRichMessage`` double, and
the fork sends rich messages via MTProto, so there is no equivalent call site.
"""

import re

import pytest

from plugins.platforms.telegram.telegram_markdown import normalize_rich_latex, to_markdown_v2
from tests.fakes.platforms.telegram_standin import mdv2_error



def _shown(formatted: str) -> str:
    """What the Telegram client displays for a MarkdownV2 string (escapes dropped)."""
    return re.sub(r"\\(.)", r"\1", formatted)


@pytest.mark.parametrize(
    ("markdown", "visible"),
    [
        ("Flow: A \\rightarrow B \\rightarrow C", "Flow: A → B → C"),
        ("If $x \\leq y$ and $y \\neq 0$ then \\alpha \\Rightarrow \\beta", "If x ≤ y and y ≠ 0 then α ⇒ β"),
        ("Cost: $5 \\times 3$ items, total \\approx $15", "Cost: 5 × 3 items, total ≈ $15"),
        ("compact $x\\leq y$ math", "compact x≤ y math"),
        # Controls: code, paths, an escaped backslash, unknown macros and currency stay as written.
        ("Run `a \\rightarrow b` then\n```tex\n\\to\n```", "Run `a \\rightarrow b` then\n```tex\n\\to\n```"),
        ("C:\\in\\data, \\\\rightarrow, \\textbf{x}", "C:\\in\\data, \\\\rightarrow, \\textbf{x}"),
        ("Prices $395k vs $483k", "Prices $395k vs $483k"),
        # Relative Windows paths, env-var paths, hyphenated words, link targets and bare URLs stay as written.
        ("Run .\\to\\file.ps1 then ..\\top\\x", "Run .\\to\\file.ps1 then ..\\top\\x"),
        ("%APPDATA%\\top\\cfg and ~\\to\\x", "%APPDATA%\\top\\cfg and ~\\to\\x"),
        ("edit \\in-place", "edit \\in-place"),
        ("[docs](https://e.com/?q=\\to) A \\to B", "[docs](https://e.com/?q=\\to) A → B"),
        ("see https://e.com/a?x=\\in&y", "see https://e.com/a?x=\\in&y"),
        # A currency pair keeps both dollar signs even with a space before the amount.
        ("$5 \\to $ 10", "$5 → $ 10"),
        # A digit on the next line or after a space is prose, never an amount: the math still maps.
        ("1. Show $x \\to y$\n2. Then $a \\le b$\n3. Done", "1. Show x → y\n2. Then a ≤ b\n3. Done"),
        ("$\\alpha$ 3 times", "α 3 times"),
        # Adjacent macros all map, not only the first of the run.
        ("\\alpha\\beta and \\alpha\\rightarrow\\beta", "αβ and α→β"),
        # A link target with parentheses and a query keeps its macro; the prose after it still maps.
        ("[plot](https://example.com/f(x)?q=\\alpha) A \\to B", "[plot](https://example.com/f(x)?q=\\alpha) A → B"),
    ],
)
def test_format_message_renders_latex_macros_as_unicode(markdown, visible):
    formatted = to_markdown_v2(markdown)
    assert mdv2_error(formatted) is None
    assert _shown(formatted) == visible


@pytest.mark.parametrize(
    "markdown",
    ["Run .\\to\\file.ps1", "%APPDATA%\\top\\cfg", "[docs](https://e.com/?q=\\to)", "see https://e.com/a?x=\\in&y",
     "[plot](https://example.com/f(x)?q=\\alpha)", "\\alpha\\beta\\data", "\\alpha\\beta-file"],
)
def test_rich_latex_keeps_paths_and_link_targets(markdown):
    assert normalize_rich_latex(markdown) == markdown
