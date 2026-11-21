"""Gateway slash commands for saved memories: /memory (review + approval gate)
and /memories plus /memory all (list every saved memory).

Split out of ``gateway/slash_commands.py`` (already over the 2,000-line ratchet);
bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``. Origin
internals are imported lazily inside the bodies to avoid the import cycle.
"""

from __future__ import annotations

from agent.i18n import t
from gateway.platforms.event import MessageEvent


class GatewayMemoriesCommandsMixin:
    """Review pending memory writes; list every saved memory in full."""

    async def _handle_memory_command(self, event: MessageEvent) -> str | None:
        """Handle /memory — review pending memory writes + toggle the approval gate. Entries are small
        enough to review inline, so the full flow works on every platform."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Memory", text=text):
                return None
            return text
        from gateway.slash_commands import _pending_card_lines  # facade seam (see root AGENTS.md)
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store
        args = event.get_command_args().strip().split()
        if args and args[0].lower() == "all":
            # /memory all is the /memories listing (rich collapsibles on Telegram).
            return await self._handle_memories_command(event)
        # Apply approved writes against a fresh on-disk store (the gateway has no long-lived agent;
        # the store persists to the same MEMORY/USER.md and honors the configured char limits).
        out = handle_pending_subcommand(
            wa.MEMORY, args, memory_store=load_on_disk_store(),
            set_mode_fn=self._write_approval_setter("memory", event))
        reply = out if out is not None else t("gateway.memory.unknown_subcommand")
        return await _reply(_pending_card_lines(wa.MEMORY, reply))

    def _memories_page_state(self) -> dict:
        """Short-lived listing-page tokens (pruned like the sessions tokens)."""
        import time

        state = getattr(self, "_memories_page_tokens", None)
        if state is None:
            state = {}
            self._memories_page_tokens = state
        now = time.monotonic()
        for token in [t for t, v in state.items() if now - v.get("created_at", 0) > 300]:
            del state[token]
        return state

    def _rich_memories_listing(self, rows, *, title: str = "Memories"
                               ) -> tuple[str | None, tuple[tuple[str, str], ...]]:
        """Rich Telegram listing for *rows* (collapsibles, 10/page); ``None`` html when empty.

        Registers a short-lived page token so Prev/Next buttons can re-render the
        same listing without re-reading the store. The plain-text listing stays the
        fallback for non-Telegram surfaces.
        """
        from plugins.platforms.telegram.command_cards import memories_listing_html
        import secrets
        import time

        if not rows:
            return None, ()
        token = secrets.token_hex(4)
        self._memories_page_state()[token] = {
            "rows": list(rows), "title": title, "created_at": time.monotonic(),
        }
        html = memories_listing_html(list(rows), page=1, page_token=token, title=title)
        return html, ()

    def render_memories_listing_page(self, token: str, page: int) -> tuple[str, tuple] | None:
        """Re-render a registered memories listing at *page* for a Prev/Next tap."""
        from plugins.platforms.telegram.command_cards import memories_listing_html

        entry = self._memories_page_state().get(str(token or ""))
        if entry is None:
            return None
        html = memories_listing_html(
            entry["rows"], page=page, page_token=str(token or ""),
            title=entry.get("title") or "Memories")
        return html, ()

    async def _handle_memories_command(self, event: MessageEvent) -> str | None:
        """Handle /memories — list every saved memory in full.

        Same store/scope as /memory (``load_on_disk_store()``: personal scope in an
        authorized DM, profile scope elsewhere). On Telegram the reply is a rich
        card of one collapsible per entry, 10 per page; elsewhere the plain-text
        listing (also in full, no tables).
        """
        from tools.memory_tool import format_memories_text, list_memories, load_on_disk_store

        rows = list_memories(load_on_disk_store())
        text = format_memories_text(rows)
        html, copies = self._rich_memories_listing(rows, title="Memories")
        if await self._send_telegram_command_card(
                event, title="Memories", html=html, text=text, copies=copies):
            return None
        return text
