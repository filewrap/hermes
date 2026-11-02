"""HTML presentation invariants for controls delegated from a setup card."""
from __future__ import annotations

import re

_FOOTER = re.compile(
    r"<p>\s*<(?:i|em)>Page \d+ of \d+ • total \d+</(?:i|em)>\s*</p>",
    re.IGNORECASE,
)


def setup_card_html(rich, text: str, total: int):
    """Preserve structured content and move the existing page count to the bottom."""
    from telethon import types

    html = getattr(rich, "html", None)
    if html is None:
        from markdown_it import MarkdownIt
        source = getattr(rich, "markdown", None)
        if source is None:
            # Structured math/block payloads are not setup text; don't flatten them.
            return rich, text
        html = MarkdownIt("commonmark", {"html": True}).enable("table").render(source)
    footers = list(_FOOTER.finditer(html))
    if footers:
        footer = footers[-1].group()
        html = _FOOTER.sub("", html).rstrip()
    else:
        footer = f"<p><i>Page 1 of 1 • total {total}</i></p>"
        html = html.rstrip() + "<hr/>"
    html += footer
    return types.InputRichMessageHTML(html), html
