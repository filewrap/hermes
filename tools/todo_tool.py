"""Todo tool: in-memory, revisioned task list for multi-step work. State lives on the
AIAgent (one per session), is re-injected after context compression, and every write bumps
a monotonic revision so UI clients can reject stale updates. One ``todo_list`` tool: pass
``todos`` to write, omit to read; every call returns the full list. No system-prompt mutation."""

import json
import time
from datetime import datetime, timezone, UTC
from typing import Any, Dict, List, Optional

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
# The list is re-read after every compression (format_for_injection), so unbounded
# content/count would defeat the compression it rides through. Caps apply equally to
# model-authored items and caller-replayed API history.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# Max single todo tool-result payload accepted during history hydration, so a forged
# oversized result is dropped before parsing (AIAgent._hydrate_todo_store).
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"
# Persisted as ordinary message content; ContextCompressor keys on this stable header to
# tell the synthetic post-compaction row from a real user message.
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}
_ACTIVE_STATUSES = {"pending", "in_progress"}


class TodoLifecycleError(ValueError):
    """Rejected strict-lifecycle transition; current snapshot remains unchanged."""


class TodoStore:
    """In-memory todo list, one per AIAgent. List position is priority; items are
    ``{id, content, status, parent?}`` — ``parent`` nests a subtask. Runtime fields
    are server-generated. Elapsed runtime is measured only while this process observes
    an item in_progress; restore starts a fresh observed segment, excluding downtime.
    ``started_at`` is the first observed wall-clock start."""

    def __init__(self, *, strict_lifecycle: bool = False):
        self._items: list[dict[str, str]] = []
        self._revision = 0
        self.strict_lifecycle = bool(strict_lifecycle)
        self._timing: dict[str, dict[str, Any]] = {}
        self._running_since: dict[str, float] = {}
        self._worked_since_start: set[str] = set()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")

    @staticmethod
    def _valid_timestamp(value: Any) -> Optional[str]:
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value

    def note_work_result(self, success: bool) -> None:
        """Credit only unambiguous active work; a failed verification revokes it.

        A successful write followed by a skipped read-back is not verification.
        Multiple active tasks cannot all borrow one unrelated tool's result.
        """
        if not self.strict_lifecycle:
            return
        active = [item["id"] for item in self._items if item["status"] == "in_progress"]
        if success and len(active) == 1:
            self._worked_since_start.add(active[0])
        else:
            self._worked_since_start.difference_update(active)

    def _validate_lifecycle(self, candidate: list[dict[str, str]]) -> None:
        if not self.strict_lifecycle:
            return
        previous = {item["id"]: item for item in self._items}
        if sum(item["status"] == "completed" and previous.get(item["id"], {}).get("status") != "completed"
               for item in candidate) > 1:
            raise TodoLifecycleError(
                "Strict lifecycle allows at most one newly completed task per write. "
                "Complete one verified task at a time; do not bulk backfill completed statuses."
            )
        newly_completed = []
        for item in candidate:
            if item["status"] != "completed":
                continue
            old = previous.get(item["id"])
            if old is None:
                raise TodoLifecycleError(
                    f"Task {item['id']!r} is new and cannot be completed directly. "
                    "Add it as pending or in_progress, then complete it after doing the work."
                )
            if old["status"] != "completed":
                newly_completed.append(item["id"])
                if old["status"] != "in_progress":
                    raise TodoLifecycleError(
                        f"Task {item['id']!r} must be in_progress before it can be completed. "
                        "Start it in one write, do and verify the work, then complete it in a later write."
                    )
                if item["id"] not in self._worked_since_start:
                    raise TodoLifecycleError(
                        f"Task {item['id']!r} has no successful tool work recorded since it started. "
                        "Perform the task with a non-todo tool, then complete it after verifying the result."
                    )
        if len(newly_completed) > 1:
            raise TodoLifecycleError(
                "Strict lifecycle allows at most one newly completed task per write. "
                "Complete one verified task at a time; do not bulk backfill completed statuses."
            )

    def _apply_timing(self, candidate: list[dict[str, str]]) -> None:
        old_by_id = {item["id"]: item for item in self._items}
        candidate_ids = {item["id"] for item in candidate}
        timing = {key: value.copy() for key, value in self._timing.items() if key in candidate_ids}
        running = {key: value for key, value in self._running_since.items() if key in candidate_ids}
        now_mono, now_wall = time.monotonic(), self._now()
        for item in candidate:
            item_id, status = item["id"], item["status"]
            old = old_by_id.get(item_id)
            old_status = old["status"] if old else None
            details = timing.setdefault(item_id, {})
            if old_status == "in_progress" and status != "in_progress":
                started = running.pop(item_id, None)
                if started is not None:
                    details["elapsed_seconds"] = float(details.get("elapsed_seconds", 0)) + max(0.0, now_mono - started)
            if status == "in_progress":
                if not details.get("started_at"):
                    details["started_at"] = now_wall
                details.pop("completed_at", None)
                if old_status != "in_progress" or item_id not in running:
                    running[item_id] = now_mono
                if old_status != "in_progress":
                    self._worked_since_start.discard(item_id)
            elif status == "completed":
                if old_status != "completed" or not details.get("completed_at"):
                    details["completed_at"] = now_wall
                running.pop(item_id, None)
                self._worked_since_start.discard(item_id)
            else:
                details.pop("completed_at", None)
                self._worked_since_start.discard(item_id)
            if not details:
                timing.pop(item_id, None)
        self._timing, self._running_since = timing, running

    def _fresh_items(self, todos: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Validate, dedupe and order a whole new list (replace / restore)."""
        existing = {item["id"]: item for item in self._items}
        prepared = []
        for t in self._dedupe_by_id(todos):
            # A status-only update of an item already on the list keeps its
            # content: the caller is changing status, not blanking the
            # description. An absent content key on a NEW item still fails
            # validation below (upstream #44496), as does an explicitly empty
            # or whitespace-only content value.
            if isinstance(t, dict) and "content" not in t:
                cur = existing.get(str(t.get("id", "")).strip())
                if cur is not None:
                    t = {**t, "content": cur["content"]}
            prepared.append(t)
        return self._normalize_order([self._validate(t) for t in prepared])

    def write(self, todos: list[dict[str, Any]], merge: bool = False) -> list[dict[str, str]]:
        """Replace the list (default) or merge by id; returns the full list after writing."""
        before = ([item.copy() for item in self._items],
                  {key: value.copy() for key, value in self._timing.items()})
        if merge:
            candidate = self._merged_items(todos)
        else:
            candidate = self._fresh_items(todos)
        del candidate[MAX_TODO_ITEMS:]  # keep the priority head; replays can't grow unbounded
        self._sanitize_parents(candidate)
        self._validate_lifecycle(candidate)
        self._apply_timing(candidate)
        self._items = candidate
        after = ([item.copy() for item in self._items],
                 {key: value.copy() for key, value in self._timing.items()})
        if after != before:
            self._revision += 1
        return self.read()

    def _merged_items(self, todos: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Build a merge candidate without mutating current state."""
        items = [item.copy() for item in self._items]
        existing = {item["id"]: item for item in items}
        for t in self._dedupe_by_id(todos):
            item_id = str(t.get("id", "")).strip()
            if not item_id:
                continue  # can't merge without an id
            cur = existing.get(item_id)
            if cur is None:
                validated = self._validate(t)
                existing[validated["id"]] = validated
                items.append(validated)
                continue
            if t.get("content"):
                cur["content"] = self._cap_content(str(t["content"]).strip())
            if t.get("status") and str(t["status"]).strip().lower() in VALID_STATUSES:
                cur["status"] = str(t["status"]).strip().lower()
            if "parent" in t:
                parent = str(t["parent"] or "").strip()
                if parent:
                    cur["parent"] = parent
                else:
                    cur.pop("parent", None)
        return self._normalize_order(items)

    def read(self) -> list[dict[str, str]]:
        result = []
        now = time.monotonic()
        for item in self._items:
            copy = item.copy()
            details = self._timing.get(item["id"], {})
            for field in ("started_at", "completed_at"):
                if details.get(field):
                    copy[field] = details[field]
            if details.get("started_at") or "elapsed_seconds" in details:
                elapsed = float(details.get("elapsed_seconds", 0))
                if item["status"] == "in_progress" and item["id"] in self._running_since:
                    elapsed += max(0.0, now - self._running_since[item["id"]])
                copy["elapsed_seconds"] = round(elapsed, 3)
            if self.strict_lifecycle and item["id"] in self._worked_since_start:
                copy["work_observed"] = True
            result.append(copy)
        return result

    def has_items(self) -> bool:
        return bool(self._items)

    def snapshot(self) -> dict[str, Any]:
        """Full state clients can reconcile atomically."""
        return {"todos": self.read(), "revision": self._revision}

    def restore(self, todos: list[dict[str, Any]], *, revision: Any = 0) -> list[dict[str, str]]:
        """Restore a trusted snapshot without manufacturing a new revision."""
        self._items = self._fresh_items(todos)[:MAX_TODO_ITEMS]
        raw_by_id = {str(item.get("id", "")).strip(): item for item in todos if isinstance(item, dict)}
        self._timing = {}
        self._running_since = {}
        now = time.monotonic()
        self._worked_since_start = set()
        for item in self._items:
            raw = raw_by_id.get(item["id"], {})
            details: dict[str, Any] = {}
            for field in ("started_at", "completed_at"):
                value = self._valid_timestamp(raw.get(field))
                if value:
                    details[field] = value
            try:
                elapsed = float(raw.get("elapsed_seconds"))
                if 0 <= elapsed < 1e12:
                    details["elapsed_seconds"] = elapsed
            except (TypeError, ValueError):
                pass
            if item["status"] == "in_progress" and not details.get("started_at"):
                details["started_at"] = self._now()
            if details:
                self._timing[item["id"]] = details
            if item["status"] == "in_progress":
                # Start a fresh monotonic segment: process downtime is not active runtime.
                self._running_since[item["id"]] = now
                if raw.get("work_observed") is True:
                    self._worked_since_start.add(item["id"])
        try:
            self._revision = max(0, int(revision or 0))
        except (TypeError, ValueError):
            self._revision = 0
        return self.read()

    def format_for_injection(self) -> Optional[str]:
        """Render the list for post-compression injection, or None if nothing active. Only
        pending/in_progress items are injected — finished ones make the model re-do work after
        compression. A parent is kept (with its real status marker) when any descendant is
        active so subtasks keep context."""
        if not self._items:
            return None
        children: dict[str, list[dict[str, str]]] = {}
        for item in self._items:
            if item.get("parent"):
                children.setdefault(item["parent"], []).append(item)

        def render(item: dict[str, str], depth: int, out: list[str]) -> bool:
            kid_lines: list[str] = []
            has_active_kid = False
            for kid in children.get(item["id"], []):
                has_active_kid |= render(kid, depth + 1, kid_lines)
            keep = item["status"] in _ACTIVE_STATUSES or has_active_kid
            if keep:
                marker = _STATUS_MARKERS.get(item["status"], "[?]")
                out.append(f"{'  ' * depth}- {marker} {item['id']}. "
                           f"{item['content']} ({item['status']})")
                out.extend(kid_lines)
            return keep

        lines = [TODO_INJECTION_HEADER]
        for item in self._items:
            if not item.get("parent"):
                render(item, 0, lines)
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _cap_content(content: str) -> str:
        """Truncate to MAX_TODO_CONTENT_CHARS keeping the head (the actionable part) + marker."""
        if len(content) > MAX_TODO_CONTENT_CHARS:
            return content[:MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: dict[str, Any]) -> dict[str, str]:
        """Normalize one item to ``{id, content, status, parent?}`` (placeholders when missing)."""
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}
        item_id = str(item.get("id", "")).strip() or "?"
        content = str(item.get("content", "")).strip()
        if not content:
            raise ValueError(
                f"Todo item '{item_id}' has empty or missing content. "
                "Each item must have a non-empty description."
            )
        status = str(item.get("status", "pending")).strip().lower()
        result = {"id": item_id,
                  "content": TodoStore._cap_content(content),
                  "status": status if status in VALID_STATUSES else "pending"}
        parent = str(item.get("parent") or "").strip()
        if parent and parent != item_id:
            result["parent"] = parent
        return result

    @staticmethod
    def _sanitize_parents(items: list[dict[str, str]]) -> None:
        """Drop dangling parent refs and break cycles in place (such items become roots)."""
        by_id = {item["id"]: item for item in items}
        for item in items:
            if item.get("parent") and item["parent"] not in by_id:
                item.pop("parent", None)
        for item in items:
            seen, node = {item["id"]}, item
            while node.get("parent"):
                if node["parent"] in seen:
                    item.pop("parent", None)
                    break
                seen.add(node["parent"])
                node = by_id[node["parent"]]

    @staticmethod
    def _dedupe_by_id(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: dict[str, int] = {}
        for i, item in enumerate(todos):  # non-dicts get a synthetic key; _validate handles them
            key = str(item.get("id", "")).strip() if isinstance(item, dict) else f"__invalid_{i}"
            last_index[key or "?"] = i
        return [todos[i] for i in sorted(last_index.values())]

    @staticmethod
    def _normalize_order(items: list[dict[str, str]]) -> list[dict[str, str]]:
        """Lift the in_progress step ahead of any earlier pending placeholder. Nested lists
        keep authored order — reordering would tear a subtask from its siblings."""
        statuses = [item["status"] for item in items]
        if any(item.get("parent") for item in items) or "in_progress" not in statuses:
            return items
        active_index = statuses.index("in_progress")
        if "pending" not in statuses[:active_index]:
            return items
        normalized = items.copy()
        normalized.insert(statuses.index("pending"), normalized.pop(active_index))
        return normalized


def todo_tool(todos: Optional[list[dict[str, Any]]] = None, merge: bool = False,
              store: Optional[TodoStore] = None) -> str:
    """Write ``todos`` (replace, or ``merge`` by id) or read when None -> list + summary JSON."""
    if store is None:
        return tool_error("TodoStore not initialized")
    if todos is None:
        items = store.read()
    else:
        if not isinstance(todos, list):
            return tool_error(f"todos must be a list, got {type(todos).__name__}")
        try:
            items = store.write(todos, merge)
        except TodoLifecycleError as exc:
            return json.dumps({"error": str(exc), "snapshot": store.snapshot()}, ensure_ascii=False)
        except ValueError as e:
            return tool_error(str(e))
    summary = {"total": len(items)}
    for status in ("pending", "in_progress", "completed", "cancelled"):
        summary[status] = sum(1 for i in items if i["status"] == status)
    return json.dumps({"todos": items, "revision": store.snapshot()["revision"],
                       "summary": summary}, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# Behavioral guidance is baked into the (static, cached) description; item shape and merge
# semantics live ONLY in the parameter schema.
TODO_SCHEMA = {
    "name": "todo_list",
    "description": (
        # See #95681.
        "Track a task list for multi-step work (3+ steps). Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "For 'all N items' tasks, enumerate every instance as its own checklist "
        "item so none are silently dropped. "
        "Call with no parameters to read the current list.\n"
        "List order is priority. Only ONE item in_progress at a time. "
        "Break large phases into subtasks via parent. "
        "Mark an item completed only after the work is verified done, never "
        "based on intent. Start work with in_progress before marking completed; "
        "complete at most one newly finished item per write and do not bulk backfill "
        "completed statuses. If something fails, cancel it and add a revised "
        "item. Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"]
                        },
                        "parent": {
                            "type": "string",
                            "description": "Optional id of another item, making this a nested subtask. Omit for top-level."
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list with a fresh plan."
                ),
                "default": False
            }
        },
        "required": []
    }
}

# Pre-rename names that replay as the Todo tool. model_tools._LEGACY_TOOL_ALIASES derives its todo
# entries from this, so the alias map and the transcript/TUI predicates below cannot drift.
TODO_LEGACY_ALIASES = ("todo",)
TODO_TOOL_NAMES = frozenset((TODO_SCHEMA["name"], *TODO_LEGACY_ALIASES))


def is_todo_tool_name(name: Any) -> bool:
    """True for the Todo tool's current name or a legacy alias (an already-unwrapped dispatch name)."""
    return isinstance(name, str) and name in TODO_TOOL_NAMES


def is_todo_tool_call(tool_call: Any) -> bool:
    """True when a transcript tool_call entry (dict or object) invoked the Todo tool.

    Covers the current name, legacy aliases, and the ``tool_call`` bridge (``todo_list`` is deferred by
    default, and the transcript keeps the bridge name). The bridge is peeled from the recorded arguments
    only, never live tool-search config, and must wrap exactly one call. Keep this module free of model_tools / agent.tool_executor
    imports: TUI resume and run_agent call this without loading either.
    """
    from agent.message_sanitization import _tc_field

    fn = _tc_field(tool_call, "function")
    name, raw_args = _tc_field(fn, "name") or "", _tc_field(fn, "arguments")
    if is_todo_tool_name(name):
        return True
    # Cheap heuristic before the bridge modules load: skip args without a literal "todo". Only a
    # unicode-escaped name slips past, which json.dumps never writes for ASCII.
    if isinstance(raw_args, str) and "todo" not in raw_args:
        return False
    from tools.tool_search_catalog import TOOL_CALL_NAME

    if name != TOOL_CALL_NAME:
        return False
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        return False
    if not isinstance(args, dict):
        return False
    from tools.tool_search_validation import normalize_tool_call_entries

    entries, error = normalize_tool_call_entries(args)
    return not error and len(entries) == 1 and is_todo_tool_name(entries[0]["name"])


from tools.registry import registry, tool_error

registry.register(
    name="todo_list", toolset="todo", schema=TODO_SCHEMA, check_fn=check_todo_requirements,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store")),
    emoji="📋")
