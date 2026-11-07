#!/usr/bin/env python3
"""Build an offline model/sampling preview without connecting to Telegram."""
from html import escape
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.model_ui import settings_payload
from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI


def render_preview() -> str:
    from telethon import types

    ui = TelegramModelSettingsUI(SimpleNamespace())
    dashboard = {
        "current_model": "openrouter/free", "current_provider": "openrouter",
        "provider_name": "OpenRouter", "provider_configured": True,
        "model_catalog_count": 63, "reasoning_supported": True,
        "session_settings": {"temperature": 0.1}, "reasoning_effort": "provider default",
    }
    dashboard_rich, dashboard_markup = ui._dashboard_view({"id": 1, "context": {"dashboard": dashboard}})
    sampling_html, sampling_markup = ui._settings_view({
        "id": 2, "identity": ("offline",) * 6, "page": "sampling",
        "context": {"settings": settings_payload(settings={"temperature": 0.1}, page="sampling")},
    })

    def rich_text_html(value):
        if isinstance(value, types.TextPlain):
            return escape(value.text)
        if isinstance(value, types.TextConcat):
            return "".join(rich_text_html(child) for child in value.texts)
        if isinstance(value, (types.TextBold, types.TextItalic)):
            tag = "b" if isinstance(value, types.TextBold) else "i"
            return f"<{tag}>{rich_text_html(value.text)}</{tag}>"
        if isinstance(value, types.TextButton):
            color = "success" if getattr(value.style, "bg_success", False) else (
                "danger" if getattr(value.style, "bg_danger", False) else "copy"
            )
            return f'<span class="rich-inline {color}">{rich_text_html(value.text)}</span>'
        if isinstance(value, types.TextEmpty):
            return ""
        return escape(str(getattr(value, "text", "")))

    def rich_blocks_html(message):
        rendered = []
        for block in message.blocks:
            if isinstance(block, types.PageBlockTitle):
                rendered.append(f"<h1>{rich_text_html(block.text)}</h1>")
            elif isinstance(block, types.PageBlockHeading2):
                rendered.append(f"<h2>{rich_text_html(block.text)}</h2>")
            elif isinstance(block, types.PageBlockParagraph):
                rendered.append(f"<p>{rich_text_html(block.text)}</p>")
            elif isinstance(block, types.PageBlockDivider):
                rendered.append("<hr/>")
            elif isinstance(block, types.PageBlockList):
                items = "".join(f"<li>{rich_text_html(item.text)}</li>" for item in block.items)
                rendered.append(f"<ul>{items}</ul>")
        return "".join(rendered)

    def card(body, markup):
        if isinstance(body, types.InputRichMessage):
            body = rich_blocks_html(body)
        rows = []
        for row in markup.rows:
            buttons = []
            for wrapped in row.buttons:
                button = getattr(wrapped, "button", wrapped)
                buttons.append(f'<button type="button" disabled>{escape(button.text)}</button>')
            rows.append('<div class="row">' + ''.join(buttons) + '</div>')
        return '<article>' + body + '</article><div class="keyboard">' + ''.join(rows) + '</div>'

    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model and sampling — offline preview</title><style>
*{box-sizing:border-box}body{margin:0;background:#141414;color:#f2f2f2;font:16px/1.45 system-ui,sans-serif}
main{max-width:540px;margin:auto;padding:18px}aside{color:#c4c4c4;font-size:13px;margin-bottom:22px}
article{background:#222;border-radius:14px;padding:20px;margin-top:24px;overflow-wrap:anywhere}
h1{font-size:24px;margin:0 0 14px}h2{font-size:20px;margin:22px 0 8px}p{margin:10px 0}
ul{padding-left:22px;margin:10px 0}hr{border:0;border-top:1px solid #626262;width:70%;margin:22px auto}
.rich-inline{display:inline-block;padding:1px 5px;border-radius:5px;font-weight:600}.rich-inline.success{background:#176b3a;color:#fff}.rich-inline.danger{background:#8a2730;color:#fff}.rich-inline.copy{background:#303c4a;color:#e9edf2}blockquote{margin:16px 0;padding:10px 12px;border-left:4px solid #d36a9b;background:#382732}
.keyboard{margin-top:6px}.row{display:flex;gap:5px;margin-bottom:5px}button{flex:1;min-width:0;padding:11px 3px;border:0;border-radius:8px;background:#8e3d66;color:white;font:600 12px system-ui;opacity:1}
</style></head><body><main><aside>Offline preview from the production Telegram RichMessage. Sample values are not a live settings read. Buttons are shown for layout only and are disabled. Telegram controls the final native appearance. No message was sent and no setting was changed.</aside>
''' + card(dashboard_rich, dashboard_markup) + card(sampling_html, sampling_markup) + '</main></body></html>\n'


if __name__ == "__main__":
    output = ROOT / "docs" / "model-ui-preview.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_preview(), encoding="utf-8")
    print(output)
