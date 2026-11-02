"""Private Telegram skill management; packages remain inert until confirmed."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import time
from html import escape
from pathlib import Path
from typing import Any

from gateway.telegram_personal_settings import (
    effective_auto_load_skill_names, load_personal_settings, save_personal_settings,
)
from .mtproto_rich import inline_markup, send_request
from .personal_settings_ui import TelegramPersonalSettingsUI

logger = logging.getLogger(__name__)
_TEMPLATE = """---
name: my-skill
description: Explain when Hermes should use this skill.
---
# My skill

## When to use
Describe the task and trigger conditions.

## Instructions
1. Describe the steps to follow.
2. Explain checks, safety constraints, and the expected output.
"""
_PAGE_SIZE = 8


def _page_footer(page: int, total: int) -> str:
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    return f"__Page {page + 1} of {pages} • total {total}__"


_HELP = (
    "A real skill is a UTF-8 `SKILL.md` with YAML frontmatter between `---` lines: "
    "a lowercase hyphenated `name`, a useful `description` (when to use it), then non-empty Markdown instructions. "
    "Use the template below. Pasting a name or an idea alone does not create a skill. "
    "For scripts/references/assets, send a ZIP with `SKILL.md` and its companion files. "
    "Never include API keys, passwords, or session files. Skills can influence tool use; review sources you trust."
)


def _plain(value: Any, limit: int = 300) -> str:
    return re.sub(r"[`*_\[\]<>]", "", str(value)).strip()[:limit]


class TelegramSkillsUI(TelegramPersonalSettingsUI):
    """Reuse the private, profile/user/message-bound settings card lifecycle."""

    def _cancel_pending(self, state, source, *, keep_payload=False):
        super()._cancel_pending(state, source, keep_payload=keep_payload)
        if not keep_payload:
            state.pop("skill_candidates", None)
            state.pop("skill_candidate", None)
            state.pop("bulk_candidate_digests", None)
            state.pop("bulk_candidate_danger", None)
            state.pop("bulk_candidates_page", None)
            state.pop("environment_choice_expected_digest", None)
            state.pop("environment_review_digest", None)
            state.pop("environment_choice", None)
            state.pop("reviewed_environment_digest", None)
            state.pop("environment_after_edit", None)
            state.pop("edit_merge_done", None)
            state.pop("reviewed_dangerous", None)
            state.pop("remove_name", None)
            state.pop("edit_name", None)

    @staticmethod
    def _button(state, label, action, style=None):
        return (label, f"sk:{state['id']}:{action}", style)

    def _push_control_frame(self, source, state):
        view = state.get("control_view", "home")
        action = state.get("control_action", view)
        page = state.get("control_page", 0)
        if view not in {"home", "list", "autoload"} or action != view:
            view, action, page = "home", "home", 0
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            page = 0
        push = getattr(self.adapter, "_push_control_frame", None)
        return bool(callable(push) and push(source, state.get("message_id"), {
            "kind": "skills", "view": view, "action": action, "page": page,
        }))

    async def restore_control_frame(self, source, message_id, frame):
        """Rebuild a safe skills destination from the current private library."""
        if (not isinstance(frame, dict) or frame.get("kind") != "skills"
                or frame.get("view") not in {"home", "list", "autoload"}
                or frame.get("action") != frame.get("view")
                or not self._is_private(source) or not source.user_id):
            return False
        page = frame.get("page", 0)
        if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= 10000:
            return False
        state = self._new_state(source, self._home(source), message_id=message_id)
        state["control_view"] = frame["view"]
        state["control_action"] = frame["action"]
        state["control_page"] = page
        if frame["view"] == "home":
            await self._render_home(state)
        elif frame["view"] == "list":
            await self._list(state, source, str(page))
        else:
            await self._autoload(state, source, str(page))
        return True

    def _back(self, state):
        edit_name = state.get("edit_name")
        names = state.get("skill_names") or []
        if edit_name and edit_name in names:
            return inline_markup([[self._button(
                state, "Back to skill", f"detail:{names.index(edit_name)}", "primary")]])
        return inline_markup([[self._button(state, "Cancel · Skills", "home", "danger")]])

    async def open(self, source, *, reply_to=None, message_id=None, invocation_message_id=None):
        self._prune()
        if not self._is_private(source) or not source.user_id:
            await self.adapter.send(source.chat_id, "Your skills are private. Open a direct chat with this bot and send `/skills`.", reply_to=reply_to)
            return
        home = self._home(source)
        key = self._owner_key(home, source)
        old = self._owner_state.get(key)
        if old is not None:
            self._drop_state(old)
        state = self._new_state(source, home, message_id=message_id)
        if invocation_message_id is not None:
            state["invocation_message_id"] = str(invocation_message_id)
        if message_id is None:
            text, markup = await self._home_view(state)
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(source.chat_id), text,
                reply_to=int(reply_to) if reply_to is not None else None, reply_markup=markup,
                rich_format="blocks",
            ))
            state["message_id"] = self.adapter._sent_message_id(response)
        else:
            await self._render_home(state)

    async def _home_view(self, state, note="", *, error=""):
        state["control_view"] = "home"
        state["control_action"] = "home"
        state["control_page"] = 0
        state.pop("autoload_back", None)
        state.pop("help_back", None)
        from gateway.telegram_skills import list_personal_skills
        own = await asyncio.to_thread(list_personal_skills, state["home"], state["user_id"])
        settings = load_personal_settings(state["home"], state["user_id"])
        selected = effective_auto_load_skill_names(state["home"], state["user_id"], settings=settings)
        description = ("Manage your private skill library and auto-load selection for this Telegram account. "
                       "Profile skills remain read-only; other users cannot see your private library.")
        change_note = "Library and auto-load changes apply to a new session (/new); the current conversation's prompt stays unchanged."
        total = 2 + (1 if note else 0)
        text = (
            "# Skills\n\n" + description + "\n\n"
            + change_note + "\n\n"
            f"**Your skills / library:** **`{len(own)} skills`**\n\n"
            f"**Auto-load selection:** **`{len(selected)} selected`**"
            + (f"\n\n**Status:** {note}" if note else "")
            + f"\n\n---\n\n__Page 1 of 1 • total {total}__"
        )
        if error:
            text = (
                "<h1>Skills</h1><p>Manage your private skill library and auto-load selection for this Telegram account. "
                "Profile skills remain read-only; other users cannot see your private library.</p>"
                f"<p>{escape(change_note)}</p>"
                f"<p><b>Your skills / library:</b> <code>{len(own)} skills</code></p>"
                f"<p><b>Auto-load selection:</b> <code>{len(selected)} selected</code></p>"
                + (f"<p><b>Status:</b> {escape(note)}</p>" if note else "")
                + "<p><b>Action failed</b></p>"
                f"<pre>{escape(str(error).strip()[:500])}</pre><hr /><p><i>Page 1 of 1 • total {total}</i></p>"
            )
        b = lambda label, action, style=None: self._button(state, label, action, style)
        rows = [[b("Add a skill", "add")],
                [b("Overview", "home", "primary"), b("Skill format / template", "help")],
                [b("Your skills", "list:0"), b("Auto-load skills", "autoload:0")],
                [b("Personal settings", "personal"), b("Close", "close", "danger")]]
        return text, inline_markup(rows)

    async def _render_home(self, state, *, note="", error=""):
        text, markup = await self._home_view(state, note, error=error)
        await self._edit(state, text, markup, rich_format="auto" if error else "blocks")

    async def handle_callback(self, event, source, data):
        if not data.startswith("sk:"):
            return False
        parts = data.split(":", 2)
        try:
            state_id = int(parts[1])
        except (ValueError, IndexError):
            await self._answer(event, "This skills card is unavailable.")
            return True
        if not self._is_private(source) or not source.user_id:
            await self._answer(event, "Skills are available only in your private chat.")
            return True
        state, source = self._state_for_callback(state_id, source, event)
        if state is None or len(parts) != 3:
            await self._answer(event, "This skills card expired or belongs to another account/profile.")
            return True
        self._rotate_callback_state(state)
        await self._answer(event)
        try:
            await self._dispatch_action(state, source, event, parts[2])
        except Exception as exc:
            logger.warning("Telegram skill action failed", exc_info=True)
            self._cancel_pending(state, source)
            await self._render_home(state, note="No change was applied.", error=str(exc))
        return True

    async def _dispatch_action(self, state, source, event, action):
        verb, _, argument = action.partition(":")
        handlers = {"home": self._home_action, "add": self._add, "help": self._help,
                    "input": self._input, "list": self._list, "detail": self._detail,
                    "read": self._read, "export": self._export, "edit": self._edit_skill,
                    "remove": self._remove, "delete": self._delete, "personal": self._personal,
                    "autoload": self._autoload, "toggle": self._toggle, "dropall": self._drop_all,
                    "choose": self._choose, "candidates": self._candidates, "preview": self._preview,
                    "review": self._review,
                    "env": self._environment_files,
                    "confirm": self._confirm, "reviewall": self._review_all,
                    "confirmall": self._confirm_all}
        if verb == "close":
            await self._close(state, source, argument, event)
            return
        handler = handlers.get(verb)
        if handler:
            await handler(state, source, argument)
        else:
            await self._render_home(state, note="This action is unavailable.")

    async def _home_action(self, state, source, argument):
        self._cancel_pending(state, source)
        await self._render_home(state)

    async def _personal(self, state, source, argument):
        self._push_control_frame(source, state)
        await self.adapter._settings_ui().open(
            source, message_id=state["message_id"],
            invocation_message_id=state.get("invocation_message_id"))
        self._drop_state(state["id"])

    async def _close(self, state, source, argument, event):
        await self._delete_attached_menu_and_command(state, source, event)
        self._drop_state(state["id"])

    async def _add(self, state, source, argument):
        self._cancel_pending(state, source)
        state["help_back"] = "add"
        b = lambda label, action, style=None: self._button(state, label, action, style)
        await self._edit(state, "# Add a skill\n\nChoose how to add it. Every detected skill is validated and shown for confirmation before saving. Nothing runs during import.", inline_markup([
            [b("Send / forward ZIP or .md", "input:file")], [b("GitHub repository / skill link", "input:github")],
            [b("Write / paste SKILL.md here", "input:text")], [b("Skill format / template", "help")],
            [b("Back to Skills", "home", "primary")]]))

    async def _help(self, state, source, argument):
        back_action = state.get("help_back")
        await self._edit(state, "# Skill format\n\n" + _HELP + "\n\n```markdown\n" + _TEMPLATE + "```", inline_markup([
            [self._button(state, "Write it here", "input:text", "primary")],
            [self._button(state, "Back to add" if back_action == "add" else "Back to Skills",
                          back_action or "home", "primary")]]))

    async def _input(self, state, source, argument):
        if argument not in {"file", "github", "text"}:
            raise ValueError("Unknown skill input method")
        edit_name = state.get("edit_name") if argument == "text" else None
        self._cancel_pending(state, source)
        if edit_name:
            state["edit_name"] = edit_name
        key = self._pending_key(Path(state["home"]), source)
        self._pending[key] = {"state_id": state["id"], "mode": argument}
        state["pending_mode"] = argument
        state["created_at"] = time.monotonic()
        prompts = {
            "file": "Send or forward one `.md` or `.zip` file. ZIP packages may contain multiple skills; choose one to review.\n\n" + _HELP,
            "github": "Send a public `https://github.com/owner/repo` link, or a tree/blob link to a specific skill. Private repositories and credentials in URLs are not accepted.",
            "text": "Send the complete SKILL.md in your next message, not just a title or idea. " + _HELP + "\n\n```markdown\n" + _TEMPLATE + "```",
        }
        await self._edit(state, "# Add a skill\n\n" + prompts[argument] + "\n\nSend `/cancel` to stop. Expires in 10 minutes.", self._back(state))

    async def handle_message(self, event, source, text):
        if not self._is_private(source) or not source.user_id:
            return False
        self._prune()
        key = self._pending_key(self._home(source), source)
        pending = self._pending.get(key)
        detected_candidates = None
        if not pending:
            message = getattr(event, "message", None)
            filename = str(getattr(getattr(message, "file", None), "name", "") or "")
            stripped = str(text or "").strip()
            mode = ("file" if filename.lower().endswith((".md", ".zip")) else
                    "text" if stripped.startswith(("---\n", "---\r\n", "```markdown\n---", "```md\n---")) else
                    "github" if re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+(?:/(?:tree|blob)/[^\s]+)?/?", stripped) else None)
            if mode is None:
                return False
            if mode == "file":
                # Ordinary project archives/notes must still reach the agent. Only
                # complete, validated skill packages are auto-detected outside Add.
                intake = getattr(event, "_hermes_attachment_intake", None)
                if intake is not None and any(item.deferred for item in intake.attachments):
                    return False
                from gateway.telegram_skills import MAX_UPLOAD_BYTES, prepare_upload, SkillValidationError
                info = getattr(message, "file", None)
                if int(getattr(info, "size", 0) or 0) > MAX_UPLOAD_BYTES:
                    return False
                media = getattr(event, "_hermes_skill_probe_media", None)
                if media is None:
                    media = await self.adapter._cache_inbound_media(message)
                event._hermes_skill_probe_media = media
                paths, _types, note = media
                if note or not paths:
                    return False
                path = Path(paths[0])
                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    return False
                try:
                    detected_candidates = await asyncio.to_thread(prepare_upload, path.read_bytes(), filename)
                except (SkillValidationError, ValueError):
                    return False
                path.unlink(missing_ok=True)
            await self.open(source, reply_to=getattr(message, "id", None))
            state_id = self._owner_state[self._owner_key(self._home(source), source)]
            state = self._states[state_id]
            self._pending[key] = {"state_id": state_id, "mode": mode}
            pending = self._pending[key]
        state = self._states.get(pending["state_id"])
        if not state:
            self._pending.pop(key, None)
            return False
        if time.monotonic() - state["created_at"] > 600:
            self._cancel_pending(state, source)
            await self._render_home(state, note="Input expired. Choose Add a skill to try again.")
            return True
        text = str(text or "").strip()
        if text.lower() == "/cancel":
            await self._home_action(state, source, "")
            return True
        if text.startswith("/"):
            self._cancel_pending(state, source)
            return False
        from gateway.telegram_skills import MAX_UPLOAD_BYTES, prepare_skill, prepare_upload, prepare_github
        try:
            mode = pending["mode"]
            if detected_candidates is not None:
                candidates = detected_candidates
            elif mode == "file":
                message = getattr(event, "message", None)
                info = getattr(message, "file", None)
                filename = str(getattr(info, "name", "") or "")
                if not filename.lower().endswith((".md", ".zip")):
                    raise ValueError("Send a .md or .zip attachment, including when forwarding a file.")
                if int(getattr(info, "size", 0) or 0) > MAX_UPLOAD_BYTES:
                    raise ValueError("Skill attachments must be at most 25 MiB.")
                media = getattr(event, "_hermes_skill_probe_media", None)
                if media is None:
                    media = await self.adapter._cache_inbound_media(message)
                paths, _types, note = media
                if note or not paths:
                    raise ValueError("The skill attachment could not be received.")
                path = Path(paths[0])
                try:
                    if path.stat().st_size > MAX_UPLOAD_BYTES:
                        raise ValueError("Skill attachments must be at most 25 MiB.")
                    candidates = await asyncio.to_thread(prepare_upload, path.read_bytes(), filename)
                finally:
                    path.unlink(missing_ok=True)
            elif mode == "github":
                candidates = await asyncio.to_thread(prepare_github, text)
            elif mode == "text":
                if text.startswith("```") and text.endswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                candidates = [await asyncio.to_thread(prepare_skill, text)]
            else:
                await self._edit(state, "# Skills\n\nUse the review card to confirm or cancel this skill.", self._back(state))
                return True
            if not candidates:
                raise ValueError("No complete SKILL.md was found.")
            if state.get("edit_name") and any(c.name != state["edit_name"] for c in candidates):
                raise ValueError("Keep the existing YAML name when editing; add a new skill to use a different name.")
            state["skill_candidates"] = candidates
            self._pending.pop(key, None)
            await self._review_candidates_or_environment_files(state, source)
        except Exception as exc:
            error_text = " ".join(str(exc).strip().split())[:500]
            await self._edit(
                state, "<h1>Skill not accepted</h1><p>" + escape(error_text)
                + "</p><p>Nothing was installed. Correct the input and send it again, "
                "or <code>/cancel</code>.</p>", self._back(state),
            )
        return True

    @staticmethod
    def _candidate_set_digest(candidates):
        material = "\0".join(candidate.digest for candidate in candidates)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _review_candidates_or_environment_files(self, state, source):
        candidates = state.get("skill_candidates") or []
        if any(candidate.environment_files for candidate in candidates):
            state["environment_choice_expected_digest"] = self._candidate_set_digest(candidates)
            rows = []
            for candidate in candidates:
                for path in sorted(candidate.environment_files, key=str.casefold):
                    rows.append(f"- {_plain(candidate.name, 64)}: `{_plain(path, 120)}`")
            listing = "\n".join(rows[:20])
            if len(rows) > 20:
                listing += f"\n- …and {len(rows) - 20} more"
            shared_notes = sorted({
                warning for candidate in candidates for warning in candidate.warnings
                if warning.startswith("Package-level environment files have no single skill owner;")
            })
            shared_note = (
                "**Package-level file handling**\n"
                + "\n".join(_plain(note, 400) for note in shared_notes)
                + "\n\n"
                if shared_notes else ""
            )
            text = (
                "# Environment files detected\n\n"
                "This package contains dotenv files that may hold passwords, API keys, or other secrets. "
                "They are not included unless you explicitly choose Include. Only include files from a source you trust; "
                "they will be saved in your private skill library and will not be executed.\n\n"
                f"{shared_note}**Detected files**\n{listing}\n\nNo skill has been saved yet."
            )
            await self._edit(state, text, inline_markup([
                [self._button(state, "Include environment files", "env:include", "danger")],
                [self._button(state, "Exclude environment files", "env:exclude", "primary")],
                [self._button(state, "Cancel", "home", "danger")],
            ]))
            return
        await self._continue_candidate_review(state, source)

    async def _continue_candidate_review(self, state, source):
        candidates = state.get("skill_candidates") or []
        if len(candidates) == 1:
            await self._choose(state, source, "0")
        else:
            await self._candidates(state, source, "0")

    async def _environment_files(self, state, source, argument):
        candidates = state.get("skill_candidates") or []
        expected = state.pop("environment_choice_expected_digest", None)
        if (not candidates or not expected
                or expected != self._candidate_set_digest(candidates)):
            raise ValueError("Environment-file review expired or changed. Import the skills again.")
        from gateway.telegram_skills import exclude_environment_files, include_environment_files
        if argument == "include":
            candidates = [await asyncio.to_thread(include_environment_files, candidate)
                          if candidate.environment_files else candidate for candidate in candidates]
            state["environment_choice"] = "include"
        elif argument == "exclude":
            candidates = [await asyncio.to_thread(exclude_environment_files, candidate)
                          if candidate.environment_files else candidate for candidate in candidates]
            state["environment_choice"] = "exclude"
        else:
            raise ValueError("Unknown environment-file choice")
        state["skill_candidates"] = candidates
        state["environment_review_digest"] = self._candidate_set_digest(candidates)
        if state.pop("environment_after_edit", False):
            candidate = candidates[0]
            if candidate.environment_files:
                raise ValueError("Environment files still need an explicit choice.")
            await self._show_skill_review(state, candidate)
            return
        await self._continue_candidate_review(state, source)

    async def _candidates(self, state, source, argument):
        candidates = state["skill_candidates"]
        page = max(0, min(int(argument or 0), max(0, (len(candidates) - 1) // _PAGE_SIZE)))
        state["candidates_page"] = page
        rows = [[self._button(state, _plain(candidate.name, 40), f"choose:{i}")]
                for i, candidate in enumerate(candidates) if page * _PAGE_SIZE <= i < (page + 1) * _PAGE_SIZE]
        nav = []
        if page:
            nav.append(self._button(state, "Previous", f"candidates:{page - 1}", "primary"))
        if (page + 1) * _PAGE_SIZE < len(candidates):
            nav.append(self._button(state, "Next", f"candidates:{page + 1}", "primary"))
        if nav:
            rows.append(nav)
        if len(candidates) > 1:
            rows.append([self._button(state, f"Review all {len(candidates)}", f"reviewall:{page}", "success")])
        rows.append([self._button(state, "Cancel", "home", "danger")])
        await self._edit(state, f"# Detected skills\n\n**{len(candidates)}** skills found. Choose one to inspect and confirm, or review the full set before adding all. No skills have been installed.\n\n{_page_footer(page, len(candidates))}", inline_markup(rows), rich_format="blocks")

    async def _choose(self, state, source, argument):
        try:
            index = int(argument)
        except (TypeError, ValueError):
            raise ValueError("That skill choice is no longer available.") from None
        candidates = state.get("skill_candidates") or []
        if index < 0 or index >= len(candidates):
            raise ValueError("That skill choice is no longer available.")
        candidate = candidates[index]
        if candidate.environment_files:
            raise ValueError("Environment files still need an explicit Include or Exclude choice.")
        if candidate.environment_files_included:
            if (state.get("environment_choice") != "include"
                    or state.get("environment_review_digest") != self._candidate_set_digest(candidates)):
                raise ValueError("Environment-file consent expired or changed. Import the skills again.")
        if state.get("edit_name") and not state.get("edit_merge_done"):
            from gateway.telegram_skills import read_personal_skill, prepare_upload
            import zipfile
            existing = read_personal_skill(state["home"], state["user_id"], state["edit_name"])
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in {**existing.files, **candidate.files}.items():
                    archive.writestr(name, data)
            candidate = prepare_upload(package.getvalue(), "edited.zip")[0]
            state["skill_candidates"] = [candidate]
            state["edit_merge_done"] = True
            state.pop("environment_choice", None)
            state.pop("environment_review_digest", None)
            if candidate.environment_files:
                state["environment_after_edit"] = True
                await self._review_candidates_or_environment_files(state, source)
                return
        await self._show_skill_review(state, candidate)

    async def _show_skill_review(self, state, candidate):
        state["skill_candidate"] = candidate
        state["reviewed_digest"] = candidate.digest
        state["reviewed_dangerous"] = candidate.dangerous
        state["reviewed_environment_digest"] = (
            candidate.digest if candidate.environment_files_included else None)
        state["pending_mode"] = "confirm"
        from gateway.telegram_skills import list_personal_skills
        own = list_personal_skills(state["home"], state["user_id"])
        replacing = candidate.name in {item["name"] for item in own}
        state["replace_skill"] = replacing
        content = candidate.files.get("SKILL.md", b"")
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        warnings = "\n".join("- " + _plain(w, 160) for w in candidate.warnings[:5]) or "No scanner findings. This is not a guarantee of safety."
        if candidate.dangerous:
            warnings = "**DANGEROUS SCANNER VERDICT — review carefully before authorizing installation.**\n" + warnings
        environment_note = (
            "**Environment files:** explicitly approved for inclusion.\n\n"
            if candidate.environment_files_included else "")
        text = (f"# Confirm {'replacement' if replacing else 'new skill'}\n\n**Name:** {_plain(candidate.name)}\n"
                f"**When to use:** {_plain(candidate.description, 500)}\n**Files:** {len(candidate.files)}\n\n"
                f"**Package fingerprint:** `{candidate.digest[:16]}`\n\n"
                f"**Included:** {_plain(', '.join(candidate.files), 350)}\n\n{environment_note}"
                f"**Security review**\n{warnings}\n\n**Instruction preview**\n{_plain(content, 1600)}\n\n"
                "Saved only to your account after confirmation. No scripts run during import. "
                "Auto-load is off until you choose it. Changes apply to new sessions.")
        await self._edit(state, text, inline_markup([
            [self._button(state, "I reviewed risks · Replace" if candidate.dangerous and replacing else
                           "I reviewed risks · Add" if candidate.dangerous else
                           "Confirm replacement" if replacing else "Add this skill", "confirm", "primary")],
            [self._button(state, "Read full SKILL.md", "preview")],
            *([[self._button(state, "Back to detected skills",
                             f"candidates:{state.get('candidates_page', 0)}", "primary")]]
              if len(state.get("skill_candidates") or []) > 1 else []),
            [self._button(state, "Cancel", "home", "danger")]]))

    async def _review_all(self, state, source, argument):
        candidates = state.get("skill_candidates") or []
        if len(candidates) < 2:
            raise ValueError("There is no multi-skill batch to add.")
        page = max(0, min(int(argument or 0), max(0, (len(candidates) - 1) // _PAGE_SIZE)))
        names = [candidate.name for candidate in candidates]
        if len(names) != len(set(names)):
            raise ValueError("This package has duplicate skill names. Choose skills individually to review them.")
        from gateway.telegram_skills import list_personal_skills
        existing = {item["name"] for item in await asyncio.to_thread(
            list_personal_skills, state["home"], state["user_id"])}
        conflicts = sorted(existing.intersection(names), key=str.casefold)
        if conflicts:
            raise ValueError("Batch not added: these names already exist in your private library: "
                             + ", ".join(conflicts) + ". Review each skill individually to replace it.")

        state["bulk_candidate_digests"] = [candidate.digest for candidate in candidates]
        state["bulk_candidate_danger"] = [candidate.dangerous for candidate in candidates]
        state["bulk_candidates_page"] = page
        state.pop("skill_candidate", None)
        state.pop("reviewed_digest", None)
        display_names = names[:30]
        listing = "\n".join("- " + _plain(name, 64) for name in display_names)
        if len(names) > len(display_names):
            listing += f"\n- …and {len(names) - len(display_names)} more"
        flagged = [(candidate.name, warning) for candidate in candidates for warning in candidate.warnings]
        dangerous_count = sum(candidate.dangerous for candidate in candidates)
        if flagged:
            warnings = [f"- {_plain(name, 64)}: {_plain(warning, 180)}" for name, warning in flagged[:8]]
            if len(flagged) > len(warnings):
                warnings.append(f"- {len(flagged) - len(warnings)} additional deterministic scanner warnings; review skills individually for details.")
            security = f"**Scanner warnings ({len(flagged)}):**\n" + "\n".join(warnings)
        else:
            security = "**Security scan:** No scanner findings reported. This is not a guarantee of safety."
        if dangerous_count:
            security = (f"**DANGEROUS SCANNER VERDICT for {dangerous_count} skill(s). Review every warning; "
                        "confirming below explicitly authorizes this private install.**\n\n" + security)
        included_environment_files = [
            f"- {_plain(candidate.name, 64)}: "
            + ", ".join(f"`{_plain(path, 100)}`" for path in sorted(candidate.files, key=str.casefold)
                         if Path(path).name.casefold() == ".env"
                         or Path(path).name.casefold().startswith((".env.", ".env_", ".env-")))
            for candidate in candidates if candidate.environment_files_included
        ]
        included_environment_files = [line for line in included_environment_files if not line.endswith(": ")]
        if included_environment_files:
            security += "\n\n**Environment files explicitly approved for inclusion**\n" + "\n".join(included_environment_files)
        text = (f"# Review all **{len(candidates)}** skills\n\nNo skills have been installed. This will add all listed skills to your private library; "
                "existing private skills are never overwritten by batch install.\n\n"
                f"**Package fingerprint:** `{hashlib.sha256(''.join(state['bulk_candidate_digests']).encode()).hexdigest()[:16]}`\n\n"
                f"**Skills**\n{listing}\n\n{security}\n\n"
                "Only validated files are stored. No scripts run during import. Auto-load remains off until selected.")
        await self._edit(state, text, inline_markup([
            [self._button(state, "I reviewed risks · Add all" if dangerous_count else
                           f"Confirm add all {len(candidates)}", "confirmall", "success")],
            [self._button(state, "Back to detected skills", f"candidates:{page}", "primary")],
            [self._button(state, "Cancel", "home", "danger")]]))

    async def _confirm_all(self, state, source, argument):
        candidates = state.get("skill_candidates") or []
        reviewed = state.pop("bulk_candidate_digests", None)
        reviewed_danger = state.pop("bulk_candidate_danger", None)
        if (len(candidates) < 2 or not reviewed
                or [candidate.digest for candidate in candidates] != reviewed
                or [candidate.dangerous for candidate in candidates] != reviewed_danger):
            raise ValueError("Batch review expired or changed. Import the skills again.")
        if any(candidate.environment_files for candidate in candidates):
            raise ValueError("Environment files still need an explicit Include or Exclude choice.")
        includes_environment_files = any(candidate.environment_files_included for candidate in candidates)
        if includes_environment_files and (
            state.get("environment_choice") != "include"
            or state.get("environment_review_digest") != self._candidate_set_digest(candidates)
        ):
            raise ValueError("Environment-file consent expired or changed. Import the skills again.")
        from gateway.telegram_skills import install_skills, list_personal_skills
        await asyncio.to_thread(
            install_skills, state["home"], state["user_id"], candidates,
            include_environment_files=includes_environment_files,
        )
        names = [item["name"] for item in await asyncio.to_thread(
            list_personal_skills, state["home"], state["user_id"])]
        save_personal_settings(state["home"], state["user_id"], {"personal_skills": names})
        self._cancel_pending(state, source)
        await self._render_home(state, note=f"Added {len(candidates)} skills to your private library. Auto-load is off; choose it if wanted, then `/new`.")
        refresh = getattr(self.adapter, "_refresh_command_menu", None)
        if callable(refresh):
            await refresh(source, force=True)

    async def _review(self, state, source, argument):
        candidate = state.get("skill_candidate")
        if candidate is None:
            raise ValueError("The skill review expired. Choose the skill again to review it.")
        await self._show_skill_review(state, candidate)

    async def _preview(self, state, source, argument):
        candidate = state.get("skill_candidate")
        if candidate is None:
            raise ValueError("The skill preview expired.")
        await self._send_file(source, candidate.files["SKILL.md"], "SKILL.md")
        await self._edit(state, f"# Review {_plain(candidate.name)}\n\nThe complete SKILL.md was sent as a file. No skill is saved until you confirm.", inline_markup([
            [self._button(state, "I reviewed risks · Replace" if candidate.dangerous and state.get("replace_skill") else
                           "I reviewed risks · Add" if candidate.dangerous else
                           "Confirm replacement" if state.get("replace_skill") else "Add this skill", "confirm", "primary")],
            [self._button(state, "Back to review", "review", "primary")],
            [self._button(state, "Cancel", "home", "danger")]]))

    async def _confirm(self, state, source, argument):
        from gateway.telegram_skills import install_skill, list_personal_skills
        candidate = state.pop("skill_candidate", None)
        if candidate is None or state.get("pending_mode") != "confirm":
            raise ValueError("Review expired; import the skill again.")
        if candidate.digest != state.pop("reviewed_digest", None):
            raise ValueError("The skill changed after review. Import and review it again.")
        if candidate.dangerous != state.pop("reviewed_dangerous", None):
            raise ValueError("The scanner verdict changed after review. Import and review it again.")
        if candidate.environment_files:
            raise ValueError("Environment files still need an explicit Include or Exclude choice.")
        includes_environment_files = candidate.environment_files_included
        if includes_environment_files and (
            state.pop("reviewed_environment_digest", None) != candidate.digest
            or state.get("environment_choice") != "include"
            or state.get("environment_review_digest") != self._candidate_set_digest(
                state.get("skill_candidates") or [])
        ):
            raise ValueError("Environment-file consent expired or changed. Import the skill again.")
        await asyncio.to_thread(
            install_skill, state["home"], state["user_id"], candidate,
            replace=state.get("replace_skill", False),
            include_environment_files=includes_environment_files,
        )
        names = [item["name"] for item in list_personal_skills(state["home"], state["user_id"])]
        save_personal_settings(state["home"], state["user_id"], {"personal_skills": names})
        self._cancel_pending(state, source)
        await self._render_home(state, note=f"Saved `{_plain(candidate.name)}` to your private library. Choose Auto-load skills if wanted, then `/new`.")
        refresh = getattr(self.adapter, "_refresh_command_menu", None)
        if callable(refresh):
            await refresh(source, force=True)

    async def _list(self, state, source, argument):
        from gateway.telegram_skills import list_personal_skills
        catalog = list_personal_skills(state["home"], state["user_id"])
        selected = set(effective_auto_load_skill_names(state["home"], state["user_id"]))
        ordered = sorted(
            catalog,
            key=lambda item: (item["name"] not in selected, item["name"].casefold(), item["name"]),
        )
        state["skill_names"] = [item["name"] for item in ordered]
        state.pop("autoload_back", None)
        page = max(0, min(int(argument or 0), max(0, (len(ordered) - 1) // _PAGE_SIZE)))
        state["control_view"] = "list"
        state["control_action"] = "list"
        state["control_page"] = page
        rows = [[self._button(state, _plain(item["name"], 40), f"detail:{i}",
                              "success" if item["name"] in selected else None)]
                for i, item in enumerate(ordered) if page * _PAGE_SIZE <= i < (page + 1) * _PAGE_SIZE]
        nav = []
        if page:
            nav.append(self._button(state, "Previous", f"list:{page - 1}", "primary"))
        if (page + 1) * _PAGE_SIZE < len(ordered):
            nav.append(self._button(state, "Next", f"list:{page + 1}", "primary"))
        if nav:
            rows.append(nav)
        rows.extend([[self._button(state, "Add a skill", "add", "primary")], [self._button(state, "Back to Skills", "home", "primary")]])
        description = (f"**{len(ordered)}** private skills. Select one to read, export, edit or remove."
                       if ordered else "Your private library is empty. Add your first skill below.")
        await self._edit(state, f"# Your skills\n\n{description}\n\n{_page_footer(page, len(ordered))}", inline_markup(rows), rich_format="blocks")

    def _name(self, state, argument):
        try:
            index = int(argument)
        except (TypeError, ValueError):
            raise ValueError("That skill is no longer in your library.") from None
        if index < 0:
            raise ValueError("That skill is no longer in your library.")
        try:
            return state["skill_names"][index]
        except (IndexError, KeyError, TypeError):
            raise ValueError("That skill is no longer in your library.") from None

    async def _detail(self, state, source, argument):
        name = self._name(state, argument)
        state["autoload_back"] = f"detail:{argument}"
        from agent.skill_commands import personal_skill_command_alias
        command = personal_skill_command_alias(name).replace("-", "_")
        rows = [[self._button(state, "Read SKILL.md", f"read:{argument}"), self._button(state, "Export ZIP", f"export:{argument}")],
                [self._button(state, "Edit instructions", f"edit:{argument}"), self._button(state, "Remove", f"remove:{argument}", "danger")],
                [self._button(state, "Auto-load selection", "autoload:0")], [self._button(state, "Your skills", f"list:{int(argument) // _PAGE_SIZE}")]]
        await self._edit(
            state, f"# {_plain(name)}\n\nThis skill belongs only to your account.\n\n"
            "**Use in Telegram**\nThis private command always targets your copy, even when a profile skill has the same name. "
            "If you just added or edited it, refresh with `/reload-skills`, then send:\n\n"
            f"`/{command} <task>`\n\n"
            "The full skill instructions are loaded into your next agent turn, together with your task. "
            "No `/new` is needed for this explicit invocation.\n\n"
            "**Auto-load instead**\nChoose it under Auto-load skills, then send `/new` to start a session "
            "with that selection. Auto-load is separate from invoking a skill by command.\n\n"
            "Editing requires a new validated SKILL.md and explicit replacement confirmation.",
            inline_markup(rows),
        )

    async def _read(self, state, source, argument):
        from gateway.telegram_skills import read_skill
        text = read_skill(state["home"], state["user_id"], self._name(state, argument))
        if len(text) > 2800:
            await self._send_file(source, text.encode("utf-8"), "SKILL.md")
            text = text[:1800] + "\n… Full SKILL.md sent as a file."
        await self._edit(state, "# Skill content\n\n" + _plain(text, 2900), inline_markup([[self._button(state, "Back", f"detail:{argument}", "primary")]]))

    async def _send_file(self, source, data, name):
        document = io.BytesIO(data)
        document.name = name
        await self.adapter._client.send_file(self.adapter._peer_for(source.chat_id), document, force_document=True)

    async def _export(self, state, source, argument):
        from gateway.telegram_skills import export_skill
        name = self._name(state, argument)
        data = export_skill(state["home"], state["user_id"], name)
        await self._send_file(source, data, name + ".zip")
        await self._detail(state, source, argument)

    async def _edit_skill(self, state, source, argument):
        name = self._name(state, argument)
        await self._read(state, source, argument)
        state["edit_name"] = name
        await self._input(state, source, "text")

    async def _remove(self, state, source, argument):
        name = self._name(state, argument)
        state["remove_name"] = name
        await self._edit(state, f"# Remove skill?\n\nRemove `{_plain(name)}` and its companion files from your private library and auto-load selection? Other users are unaffected.", inline_markup([
            [self._button(state, "Confirm removal", "delete", "danger")], [self._button(state, "Cancel", f"list:{int(argument) // _PAGE_SIZE}")]]))

    async def _delete(self, state, source, argument):
        from gateway.telegram_skills import remove_skill, list_personal_skills
        name = state.pop("remove_name", None)
        if not name:
            raise ValueError("The skill removal expired. Open the skill again to remove it.")
        remove_skill(state["home"], state["user_id"], name)
        settings = load_personal_settings(state["home"], state["user_id"])
        save_personal_settings(state["home"], state["user_id"], {
            "selected_skills": [n for n in settings.get("selected_skills", []) if n != name],
            "personal_skills": [item["name"] for item in list_personal_skills(state["home"], state["user_id"])],
        })
        await self._render_home(state, note="Removed from your private library and future auto-load selection.")
        refresh = getattr(self.adapter, "_refresh_command_menu", None)
        if callable(refresh):
            await refresh(source, force=True)

    async def _autoload(self, state, source, argument):
        from gateway.run import _profile_runtime_scope
        from gateway.telegram_personal_settings import set_personal_memory_context, reset_personal_memory_context
        from agent.skill_commands import scan_skill_commands
        from tools.skills_tool import skills_list
        with _profile_runtime_scope(Path(state["home"])):
            token = set_personal_memory_context(state["home"], state["user_id"])
            try:
                scan_skill_commands()
                names = sorted({item["name"] for item in json.loads(skills_list())["skills"]
                                if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item["name"])},
                               key=lambda name: (name.casefold(), name))
                from agent.skill_commands import resolve_profile_auto_load_skill_names
                profile_selected = set(resolve_profile_auto_load_skill_names())
            finally:
                reset_personal_memory_context(token)
        state["autoload_names"] = names
        state["autoload_profile_selected"] = profile_selected
        page = max(0, min(int(argument or 0), max(0, (len(names) - 1) // _PAGE_SIZE)))
        state["control_view"] = "autoload"
        state["control_action"] = "autoload"
        state["control_page"] = page
        selected = profile_selected | set(
            load_personal_settings(state["home"], state["user_id"]).get("selected_skills") or []
        )
        ordered = sorted(names, key=lambda name: (name not in selected, name.casefold(), name))
        state["autoload_names"] = ordered
        rows = [[self._button(state, _plain(name, 36), f"toggle:{i}", "success" if name in selected else None)]
                for i, name in enumerate(ordered) if page * _PAGE_SIZE <= i < (page + 1) * _PAGE_SIZE]
        nav = []
        if page:
            nav.append(self._button(state, "Previous", f"autoload:{page - 1}", "primary"))
        if (page + 1) * _PAGE_SIZE < len(names):
            nav.append(self._button(state, "Next", f"autoload:{page + 1}", "primary"))
        if nav:
            rows.append(nav)
        rows.append([self._button(state, "Drop all", f"dropall:{page}", "danger")])
        back_target = state.get("autoload_back")
        rows.append([self._button(state, "Back to skill" if back_target else "Back to Skills",
                                  back_target or "home", "primary")])
        note = state.pop("autoload_note", "")
        note_text = f"\n\n{_plain(note)}" if note else ""
        await self._edit(
            state,
            "# Auto-load skills\n\nProfile defaults are read-only. Toggle skills to auto-load them in new sessions (`/new`).\n\n"
            + f"**{len(selected & set(names))}** selected.{note_text}\n\n---\n\n{_page_footer(page, len(names))}",
            inline_markup(rows), rich_format="blocks",
        )

    async def _toggle(self, state, source, argument):
        try:
            index = int(argument)
        except (TypeError, ValueError):
            raise ValueError("That auto-load choice is no longer available. Open Auto-load skills again.") from None
        if index < 0:
            raise ValueError("That auto-load choice is no longer available. Open Auto-load skills again.")
        try:
            name = state["autoload_names"][index]
        except (IndexError, KeyError, TypeError):
            raise ValueError("That auto-load choice is no longer available. Open Auto-load skills again.") from None
        if name in state.get("autoload_profile_selected", set()):
            state["autoload_note"] = "This skill is selected by profile defaults and is read-only here."
            await self._autoload(state, source, str(index // _PAGE_SIZE))
            return
        selected = set(load_personal_settings(state["home"], state["user_id"]).get("selected_skills") or [])
        selected.symmetric_difference_update({name})
        save_personal_settings(state["home"], state["user_id"], {"selected_skills": sorted(selected)})
        await self._autoload(state, source, str(index // _PAGE_SIZE))

    async def _drop_all(self, state, source, argument):
        # Clears only the user's own auto-load selections. Profile defaults
        # are operator-set and read-only here, so they stay as they are.
        selected = set(load_personal_settings(state["home"], state["user_id"]).get("selected_skills") or [])
        if selected:
            save_personal_settings(state["home"], state["user_id"], {"selected_skills": []})
        state["autoload_note"] = (
            f"Dropped {len(selected)} personal auto-load selection(s). Profile defaults stay as they are."
            if selected else "No personal auto-load selections to drop; only profile defaults are active."
        )
        await self._autoload(state, source, argument or "0")
