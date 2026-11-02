#!/usr/bin/env python3
"""Safe, atomic snapshots of Hermes chat continuation state for HF Spaces.

Only a mounted, writable ``/data`` is used. The live SQLite database remains in
HERMES_HOME; published artifacts contain a curated SQLite export plus a checksum
manifest, never the broader Hermes home directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from typing import Any, Callable

DATA_ROOT = Path("/data")
SNAPSHOT_DIR_NAME = "hermes-state"
SNAPSHOT_PREFIX = "chat-state-"
FORMAT_VERSION = 1
RETAIN_SNAPSHOTS = 3
BACKUP_TIMEOUT_SECONDS = 30
_SPACE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_BUCKET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def is_hf_space() -> bool:
    """Require the platform-provided canonical Space identity."""
    return _SPACE_ID_RE.fullmatch(os.environ.get("SPACE_ID", "")) is not None


def configured_bucket_name(value: str | None = None) -> str | None:
    """Return a validated Hugging Face bucket owner/name, or None."""
    name = os.environ.get("BUCKET_NAME", "") if value is None else value
    name = name.strip() if isinstance(name, str) else ""
    return name if _BUCKET_NAME_RE.fullmatch(name) else None


def bucket_directory(data_root: Path, directory_name: str, bucket_name: str | None) -> Path:
    """Namespace state by the selected bucket without permitting path traversal."""
    base = Path(data_root) / directory_name
    if bucket_name is None:
        return base
    owner, name = bucket_name.split("/", 1)
    return base / owner / name

# Tables and columns are deliberately enumerated. Anything not named here is
# omitted from published artifacts, including settings/secret stores, caches,
# Telegram's .session files, temp state, and unrelated Hermes tables.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "id", "source", "user_id", "session_key", "chat_id", "chat_type",
        "thread_id", "display_name", "expiry_finalized", "model",
        "parent_session_id", "started_at", "ended_at", "end_reason",
        "message_count", "tool_call_count", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
        "title", "title_source", "last_activity_at", "api_call_count",
        "rewind_count", "archived", "pinned", "hidden", "last_read_at",
        "tool_names",
    ),
    "messages": (
        "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
        "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
        "reasoning_content", "reasoning_details", "codex_reasoning_items",
        "codex_message_items", "observed", "_compressed_summary", "active",
        "compacted", "api_content", "effect_disposition", "platform_message_id",
        "display_kind", "display_metadata", "display_identity", "display_order",
    ),
    "gateway_routing": ("scope", "session_key", "entry_json", "updated_at"),
    # This durable peer counter prevents conversation-generation reuse.
    "conversation_generations": ("source", "session_key", "generation"),
    # Only non-owner access flags are restored; user config/data and owner ID are not.
    "tenant_access": (
        "tenant_id", "user_id", "allowed", "banned", "authorization_generation",
        "credits", "weight", "start_count", "allowed_scopes", "sudo_scopes",
    ),
    # TenantRuntime stores its continuing Telegram transcript in profile data,
    # not in SessionDB. Only the validated room transcript is exported.
    "tenant_history": ("tenant_id", "user_id", "chat_id", "room_key", "revision", "payload"),
}
REQUIRED_TABLES = frozenset(("sessions", "messages"))
_LEGACY_EXCLUDED_COLUMNS: dict[str, tuple[str, ...]] = {
    # Old v1 archives copied source table DDL, but nulled these excluded values.
    "sessions": (
        "model_config", "system_prompt", "system_prompt_hash", "origin_json",
        "billing_base_url", "billing_provider", "billing_mode", "handoff_state",
        "handoff_platform", "handoff_error", "cwd", "git_branch", "git_repo_root",
        "last_activity_description", "last_activity_provenance",
        "compression_failure_error", "pricing_version", "profile_name",
    ),
}
_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|authorization|credential|api[_-]?(?:key|hash)|token|"
    r"auth[_-]?key|session[_-]?string|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_ROUTING_FIELDS = frozenset({
    "session_key", "session_id", "created_at", "updated_at", "origin",
    "display_name", "platform", "chat_type", "metadata", "input_tokens",
    "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens",
    "estimated_cost_usd", "cost_status", "last_prompt_tokens", "was_auto_reset",
    "auto_reset_reason", "reset_had_activity", "prev_session_id", "is_fresh_reset",
    "expiry_finalized", "suspended", "resume_pending", "resume_reason",
    "last_resume_marked_at", "active_turn_token", "active_turn_started_at",
})


def _decode_mount_path(value: str) -> Path:
    for escaped, char in (("\\040", " "), ("\\011", "\t"),
                          ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(escaped, char)
    return Path(value)


def is_mountpoint(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Return true only when *path itself* is a Linux mount point; fail closed."""
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        expected = Path(os.path.abspath(path))
        for row in mountinfo.read_text(encoding="utf-8").splitlines():
            left, _right = row.split(" - ", 1)
            fields = left.split()
            if len(fields) > 4 and _decode_mount_path(fields[4]) == expected:
                return True
    except (OSError, ValueError):
        return False
    return False


def _safe_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise OSError("snapshot directory is not a real directory")
    path.chmod(0o700)


def _prepare_bucket_directory(data_root: Path, directory_name: str,
                              bucket_name: str) -> Path:
    """Create and verify every directory component under the mounted bucket."""
    owner, name = bucket_name.split("/", 1)
    paths = (Path(data_root) / directory_name,
             Path(data_root) / directory_name / owner,
             Path(data_root) / directory_name / owner / name)
    for path in paths:
        if path.is_symlink():
            raise OSError("snapshot directory is a symlink")
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        _safe_directory(path)
    return paths[-1]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _connect_readonly(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return sqlite3.connect(path.resolve().as_uri() + query, uri=True, timeout=30)


def _quick_check(connection: sqlite3.Connection) -> None:
    rows = [tuple(row) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if rows != [("ok",)]:
        raise ValueError("SQLite integrity check failed")


def _scrub_json(value: Any) -> Any:
    """Recursively remove known credential-shaped keys from routing metadata."""
    if isinstance(value, dict):
        return {
            key: _scrub_json(item)
            for key, item in value.items()
            if isinstance(key, str) and not _SENSITIVE_KEY.search(key)
        }
    if isinstance(value, list):
        return [_scrub_json(item) for item in value]
    return value


def _safe_routing_entry(entry_json: str) -> str | None:
    try:
        entry = json.loads(entry_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    result = {
        key: _scrub_json(value)
        for key, value in entry.items()
        if key in _ROUTING_FIELDS
    }
    # The opaque turn token is a locally generated resume marker, not an auth
    # credential; it is retained to preserve interrupted-turn recovery.
    if not isinstance(result.get("session_key"), str) or not result["session_key"]:
        return None
    if not isinstance(result.get("session_id"), str) or not result["session_id"]:
        return None
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _create_allowlisted_table(
    source: sqlite3.Connection, destination: sqlite3.Connection,
    table: str, columns: tuple[str, ...],
) -> tuple[str, ...]:
    """Create a fresh schema with only reviewed columns and harmless defaults."""
    info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
    by_name = {row[1]: row for row in info}
    selected = tuple(column for column in columns if column in by_name)
    if not selected:
        return ()
    quoted = {name: '"' + name.replace('"', '""') + '"' for name in selected}
    primary_key = sorted((row[5], row[1]) for row in info if row[1] in selected and row[5])
    pieces: list[str] = []
    for name in selected:
        row = by_name[name]
        declared_type = row[2] or ""
        if not re.fullmatch(r"[A-Za-z0-9_(), ]*", declared_type):
            raise ValueError(f"unsupported declared type in {table}.{name}")
        clause = f"{quoted[name]} {declared_type}".rstrip()
        if row[3] and not row[5]:
            clause += " NOT NULL"
        default = row[4]
        if isinstance(default, str) and re.fullmatch(r"(?:NULL|-?[0-9]+(?:\.[0-9]+)?|'')", default):
            clause += f" DEFAULT {default}"
        pieces.append(clause)
    if len(primary_key) == 1:
        pieces[selected.index(primary_key[0][1])] += " PRIMARY KEY"
    elif primary_key:
        pieces.append("PRIMARY KEY (" + ", ".join(quoted[name] for _, name in primary_key) + ")")
    destination.execute(f'CREATE TABLE "{table}" ({", ".join(pieces)})')
    return selected


def _copy_allowlisted_database(source: Path, destination: Path) -> tuple[int, int]:
    """Create a fresh DB from only the explicitly approved session/chat rows."""
    with closing(_connect_readonly(source)) as src, closing(sqlite3.connect(destination, timeout=30)) as dst:
        src.row_factory = sqlite3.Row
        _quick_check(src)
        tables = {
            row[0] for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not REQUIRED_TABLES.issubset(tables):
            raise ValueError("SQLite database lacks chat session tables")
        # Snapshot-only tenant tables are never authoritative in state.db.
        # Always re-export them from the current tenant database instead.
        tables -= {"tenant_access", "tenant_history"}
        dst.execute("PRAGMA foreign_keys=OFF")
        selected_columns: dict[str, tuple[str, ...]] = {}
        for table, allowed in TABLE_COLUMNS.items():
            if table in tables:
                selected_columns[table] = _create_allowlisted_table(src, dst, table, allowed)
        copied: dict[str, int] = {}
        for table, allowed in TABLE_COLUMNS.items():
            if table not in tables:
                continue
            selected = selected_columns[table]
            if not selected:
                continue
            quoted = ",".join('"' + column.replace('"', '""') + '"' for column in selected)
            rows = src.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            for row in rows:
                values = list(row)
                if table == "sessions":
                    # These columns can contain runtime provider credentials or
                    # prompt/config material. They are intentionally not exported.
                    for name in ("model_config", "system_prompt", "system_prompt_hash",
                                 "origin_json", "billing_base_url", "billing_provider",
                                 "billing_mode", "handoff_state", "handoff_platform",
                                 "handoff_error", "cwd", "git_branch", "git_repo_root",
                                 "last_activity_description", "last_activity_provenance",
                                 "compression_failure_error", "pricing_version", "profile_name"):
                        if name in selected:
                            values[selected.index(name)] = None
                elif table == "gateway_routing":
                    entry_position = selected.index("entry_json")
                    safe_entry = _safe_routing_entry(values[entry_position])
                    if safe_entry is None:
                        continue
                    values[entry_position] = safe_entry
                marks = ",".join("?" for _ in selected)
                dst.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({marks})', values)
            copied[table] = len(rows) if table != "gateway_routing" else dst.execute(
                "SELECT COUNT(*) FROM gateway_routing"
            ).fetchone()[0]
        dst.commit()
        _quick_check(dst)
        # A compact rewrite physically removes source pages that held the scrubbed
        # model_config, billing, prompt, or non-allowlisted table contents.
        dst.execute("VACUUM")
        dst.commit()
        return copied.get("sessions", 0), copied.get("messages", 0)


def _tenant_id_for_user(user_id: str) -> str:
    if re.fullmatch(r"[0-9]+", user_id):
        return "tg-" + user_id
    return "u-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _tenant_scopes(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"invalid tenant {field}")
    result: list[str] = []
    for scope in value:
        if scope == "dm" or isinstance(scope, str) and re.fullmatch(r"group:-?[0-9]+", scope):
            clean = scope
        else:
            raise ValueError(f"invalid tenant {field}")
        if clean not in result:
            result.append(clean)
    return result


def _validated_tenant_history(
    tenant_id: str, user_id: str, key: str, payload: Any,
) -> tuple[str, list[dict[str, str]]]:
    match = re.fullmatch(r"history:(room-[0-9a-f]{32})", key)
    if not match or not isinstance(payload, list):
        raise ValueError("invalid tenant conversation record")
    room_key = match.group(1)
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("tenant conversation exceeds storage limit")
    history: list[dict[str, str]] = []
    chat_id: str | None = None
    for item in payload:
        if (not isinstance(item, dict)
                or item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("content"), str)
                or item.get("user_id") != user_id
                or not isinstance(item.get("chat_id"), str)):
            raise ValueError("invalid tenant conversation message")
        item_chat_id = item["chat_id"]
        if chat_id is None:
            chat_id = item_chat_id
        elif chat_id != item_chat_id:
            raise ValueError("mixed tenant conversation rooms")
        expected_room = "room-" + hashlib.sha256(
            f"{user_id}\0{item_chat_id}".encode()
        ).hexdigest()[:32]
        if expected_room != room_key:
            raise ValueError("tenant conversation routing identity mismatch")
        # Project the documented gateway transcript shape; discard every
        # unreviewed field even if one was manually added to a local record.
        history.append({
            "role": item["role"], "content": item["content"],
            "user_id": user_id, "chat_id": item_chat_id,
        })
    if not history:
        raise ValueError("empty tenant conversation record")
    return room_key, history


def _copy_tenant_access(state_home: Path, destination: Path) -> tuple[int, int]:
    """Append only non-owner access flags and room histories from local tenants."""
    source = state_home / "tenant-state.db"
    if not source.exists():
        return 0, 0
    if source.is_symlink() or not source.is_file():
        raise ValueError("tenant database is not a regular local file")
    with closing(_connect_readonly(source)) as src:
        # Pin permissions and their room histories to the same committed view.
        src.execute("BEGIN")
        _quick_check(src)
        tables = {row[0] for row in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if "tenant_documents" not in tables:
            raise ValueError("tenant database has no recognized document table")
        columns = {row[1] for row in src.execute('PRAGMA table_info("tenant_documents")')}
        if not {"collection", "id", "body"}.issubset(columns):
            raise ValueError("tenant document table has an unexpected schema")
        profiles = src.execute(
            "SELECT id,body FROM tenant_documents WHERE collection=? ORDER BY id",
            ("hermes_tenant_profiles",),
        ).fetchall()
        # Ask SQLite for only the exact document class used by TenantRuntime.
        # In particular, never materialize settings, memories, skills, usage,
        # receipts, or arbitrary tenant payloads while building this archive.
        data_rows = src.execute(
            """SELECT id,body FROM tenant_documents
               WHERE collection=? AND json_extract(body,'$.kind')='profile'
                 AND json_extract(body,'$.key') GLOB 'history:room-*'
               ORDER BY id""",
            ("hermes_tenant_data",),
        ).fetchall()
    profile_by_tenant: dict[str, dict[str, Any]] = {}
    for row_id, raw in profiles:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("_id") != row_id:
            raise ValueError("invalid tenant profile record")
        if row_id == "owner":
            continue
        raw_user_id = doc.get("user_id")
        if isinstance(raw_user_id, bool) or not isinstance(raw_user_id, (str, int)):
            raise ValueError("invalid tenant profile identity")
        user_id = str(raw_user_id).strip()
        if (not user_id or len(user_id) > 512 or "\x00" in user_id
                or _tenant_id_for_user(user_id) != row_id):
            raise ValueError("invalid tenant profile identity")
        if row_id in profile_by_tenant:
            raise ValueError("duplicate tenant profile identity")
        allowed, banned = doc.get("allowed", False), doc.get("banned", False)
        generation, starts = doc.get("authorization_generation", 0), doc.get("start_count", 0)
        credits, weight = doc.get("credits", 0), doc.get("weight", 1)
        if type(allowed) is not bool or type(banned) is not bool:
            raise ValueError("invalid tenant access flags")
        if any(type(value) is not int or value < 0 for value in (generation, starts)):
            raise ValueError("invalid tenant authorization generation")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in (credits, weight)):
            raise ValueError("invalid tenant profile accounting value")
        allowed_scopes = _tenant_scopes(doc.get("allowed_scopes", []), field="allowed scopes")
        sudo_scopes = _tenant_scopes(doc.get("sudo_scopes", []), field="sudo scopes")
        if not set(sudo_scopes).issubset(allowed_scopes):
            raise ValueError("invalid tenant scoped administrator grants")
        profile_by_tenant[row_id] = {
            "user_id": user_id, "allowed": int(allowed), "banned": int(banned),
            "generation": generation, "credits": credits, "weight": weight,
            "starts": starts, "allowed_scopes": allowed_scopes,
            "sudo_scopes": sudo_scopes,
        }
    with closing(sqlite3.connect(destination, timeout=30)) as dst:
        dst.execute("""CREATE TABLE tenant_access (
            tenant_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, allowed INTEGER NOT NULL,
            banned INTEGER NOT NULL, authorization_generation INTEGER NOT NULL,
            credits REAL NOT NULL, weight REAL NOT NULL, start_count INTEGER NOT NULL,
            allowed_scopes TEXT NOT NULL, sudo_scopes TEXT NOT NULL
        )""")
        for tenant_id, profile in profile_by_tenant.items():
            dst.execute(
                "INSERT INTO tenant_access VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, profile["user_id"], profile["allowed"], profile["banned"],
                 profile["generation"], profile["credits"], profile["weight"],
                 profile["starts"], json.dumps(profile["allowed_scopes"], separators=(",", ":")),
                 json.dumps(profile["sudo_scopes"], separators=(",", ":"))),
            )
        dst.execute("""CREATE TABLE tenant_history (
            tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
            room_key TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(tenant_id, room_key)
        )""")
        history_count = 0
        for row_id, raw in data_rows:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get("_id") != row_id:
                raise ValueError("invalid tenant data record")
            if doc.get("kind") != "profile" or not isinstance(doc.get("key"), str):
                continue
            key = doc["key"]
            if not key.startswith("history:"):
                continue
            tenant_id = doc.get("tenant_id")
            profile = profile_by_tenant.get(tenant_id)
            if profile is None:
                continue
            if doc.get("_id") != hashlib.sha256(
                (tenant_id + "\0profile\0" + key).encode("utf-8")
            ).hexdigest():
                raise ValueError("invalid tenant conversation address")
            revision = doc.get("revision")
            if type(revision) is not int or revision < 1:
                raise ValueError("invalid tenant conversation revision")
            room_key, history = _validated_tenant_history(
                tenant_id, profile["user_id"], key, doc.get("payload"),
            )
            raw_chat_id = history[0]["chat_id"]
            dst.execute(
                "INSERT INTO tenant_history VALUES (?,?,?,?,?,?)",
                (tenant_id, profile["user_id"], raw_chat_id, room_key, revision,
                 json.dumps(history, ensure_ascii=False, allow_nan=False, separators=(",", ":"))),
            )
            history_count += 1
        dst.commit()
        _quick_check(dst)
    return len(profile_by_tenant), history_count


def _validate_snapshot_database(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("snapshot database is not a regular file")
    # Finished artifacts are immutable: validation must never create bucket WAL
    # sidecars or depend on network filesystem SQLite locking.
    with closing(_connect_readonly(path, immutable=True)) as db:
        _quick_check(db)
        names = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not REQUIRED_TABLES.issubset(names) or not names.issubset(TABLE_COLUMNS):
            raise ValueError("snapshot contains unexpected SQLite tables")
        objects = db.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if any(kind != "table" for kind, _name in objects):
            raise ValueError("snapshot contains executable or auxiliary SQLite schema objects")
        for table in names:
            actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            allowed = set(TABLE_COLUMNS[table])
            legacy = set(_LEGACY_EXCLUDED_COLUMNS.get(table, ()))
            if not actual.issubset(allowed | legacy):
                raise ValueError(f"snapshot has unreviewed columns in {table}")
            required = {
                "sessions": {"id"},
                "messages": {"id", "session_id", "role", "content", "timestamp"},
                "gateway_routing": {"scope", "session_key", "entry_json", "updated_at"},
                "conversation_generations": {"source", "session_key", "generation"},
                "tenant_access": {
                    "tenant_id", "user_id", "allowed", "banned",
                    "authorization_generation", "credits", "weight", "start_count",
                },
                "tenant_history": {
                    "tenant_id", "user_id", "chat_id", "room_key", "revision", "payload",
                },
            }.get(table, set())
            if not required.issubset(actual):
                raise ValueError(f"snapshot lacks required columns in {table}")
            for column in legacy & actual:
                if db.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1'
                ).fetchone():
                    raise ValueError("snapshot contains excluded session configuration")
        if "tenant_access" in names:
            access_columns = {row[1] for row in db.execute('PRAGMA table_info("tenant_access")')}
            has_scopes = {"allowed_scopes", "sudo_scopes"}.issubset(access_columns)
            if ("allowed_scopes" in access_columns) != ("sudo_scopes" in access_columns):
                raise ValueError("incomplete tenant scoped access fields")
            selected = "tenant_id,user_id,allowed,banned,authorization_generation,credits,weight,start_count"
            if has_scopes:
                selected += ",allowed_scopes,sudo_scopes"
            for row in db.execute(f"SELECT {selected} FROM tenant_access"):
                tenant_id, user_id, allowed, banned, generation, credits, weight, starts = row[:8]
                if (not isinstance(tenant_id, str) or tenant_id == "owner"
                        or not isinstance(user_id, str) or not user_id
                        or _tenant_id_for_user(user_id) != tenant_id
                        or allowed not in (0, 1) or banned not in (0, 1)
                        or any(type(value) is not int or value < 0 for value in (generation, starts))
                        or any(isinstance(value, bool) or not isinstance(value, (int, float))
                               or not math.isfinite(value) for value in (credits, weight))):
                    raise ValueError("invalid tenant access state in snapshot")
                if has_scopes:
                    allowed_scopes = _tenant_scopes(json.loads(row[8]), field="allowed scopes")
                    sudo_scopes = _tenant_scopes(json.loads(row[9]), field="sudo scopes")
                    if (json.dumps(allowed_scopes, separators=(",", ":")) != row[8]
                            or json.dumps(sudo_scopes, separators=(",", ":")) != row[9]
                            or not set(sudo_scopes).issubset(allowed_scopes)):
                        raise ValueError("invalid tenant scoped access state in snapshot")
        if "tenant_history" in names:
            if "tenant_access" not in names:
                raise ValueError("tenant conversation has no access records")
            access = {
                tenant_id: user_id for tenant_id, user_id in db.execute(
                    "SELECT tenant_id,user_id FROM tenant_access"
                )
            }
            for tenant_id, user_id, chat_id, room_key, revision, raw_payload in db.execute(
                "SELECT tenant_id,user_id,chat_id,room_key,revision,payload FROM tenant_history"
            ):
                if (not isinstance(tenant_id, str) or access.get(tenant_id) != user_id
                        or not isinstance(chat_id, str) or type(revision) is not int or revision < 1):
                    raise ValueError("invalid tenant conversation routing state")
                history = json.loads(raw_payload)
                canonical_key = "history:" + room_key
                if not isinstance(room_key, str):
                    raise ValueError("invalid tenant conversation room key")
                checked_room, checked_history = _validated_tenant_history(
                    tenant_id, user_id, canonical_key, history,
                )
                if checked_room != room_key or checked_history[0]["chat_id"] != chat_id:
                    raise ValueError("tenant conversation room mismatch")
                if json.dumps(checked_history, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")) != raw_payload:
                    raise ValueError("tenant conversation contains unreviewed fields")
        if "gateway_routing" in names:
            for (entry_json,) in db.execute("SELECT entry_json FROM gateway_routing"):
                if _safe_routing_entry(entry_json) != entry_json:
                    raise ValueError("snapshot contains unsanitized routing metadata")


def _manifest_path(snapshot_path: Path) -> Path:
    return snapshot_path.with_suffix(".json")


def _read_valid_candidate(manifest_path: Path) -> tuple[Path, dict[str, Any]]:
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError("invalid snapshot manifest file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        raise ValueError("unsupported snapshot manifest")
    expected_name = manifest_path.name[:-5] + ".sqlite3"
    database_name = manifest.get("database")
    if database_name != expected_name or Path(database_name).name != database_name:
        raise ValueError("unsafe snapshot database path")
    database = manifest_path.parent / database_name
    if database.is_symlink() or not database.is_file():
        raise ValueError("missing or symlinked snapshot database")
    if manifest.get("size") != database.stat().st_size:
        raise ValueError("snapshot size mismatch")
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid snapshot checksum")
    if _sha256(database) != digest:
        raise ValueError("snapshot checksum mismatch")
    return database, manifest


@contextmanager
def _local_candidate(manifest_path: Path, state_home: Path):
    """Read bucket bytes as files; SQLite opens only this private local copy."""
    database, manifest = _read_valid_candidate(manifest_path)
    if state_home.is_symlink():
        raise OSError("state home is a symlink")
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".snapshot-check-", suffix=".db", dir=state_home)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, database.open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if staged.stat().st_size != manifest["size"] or _sha256(staged) != manifest["sha256"]:
            raise ValueError("snapshot changed while downloading")
        _validate_snapshot_database(staged)
        yield staged, manifest
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


class SnapshotStore:
    """Manages bounded atomic snapshots while leaving live state on local disk."""

    def __init__(
        self,
        state_home: Path,
        data_root: Path = DATA_ROOT,
        *,
        mount_check: Callable[[Path], bool] = is_mountpoint,
        space_check: Callable[[], bool] = is_hf_space,
        bucket_name: str | None = None,
    ) -> None:
        self.state_home = Path(state_home)
        self.db_path = self.state_home / "state.db"
        self.tenant_db_path = self.state_home / "tenant-state.db"
        self.data_root = Path(data_root)
        self.space_check = space_check
        self._bucket_name_injected = bucket_name is not None
        self.bucket_name = configured_bucket_name(bucket_name)
        self.directory = bucket_directory(self.data_root, SNAPSHOT_DIR_NAME, self.bucket_name)
        self.mount_check = mount_check
        self.enabled = False
        self.reason = "mounted /data persistence has not been checked"
        self.last_snapshot_at: float | None = None
        self.last_error: str | None = None
        self.restore_note = ""

    def _mounted(self) -> bool:
        try:
            return (not self.data_root.is_symlink() and self.data_root.is_dir()
                    and self.mount_check(self.data_root))
        except OSError:
            return False

    def _bucket_configuration_valid(self) -> bool:
        if self.bucket_name is None:
            return False
        return self._bucket_name_injected or configured_bucket_name() == self.bucket_name

    def _storage_available(self) -> bool:
        try:
            return self.space_check() and self._bucket_configuration_valid() and self._mounted()
        except OSError:
            return False

    def prepare(self) -> bool:
        """Validate mount and writability without confusing an image dir for a bucket."""
        try:
            if not self.space_check():
                raise OSError("SPACE_ID does not identify a Hugging Face Space")
            if not self._bucket_configuration_valid():
                raise OSError("BUCKET_NAME must identify the mounted bucket owner/name")
            if not self._mounted():
                raise OSError("/data is not a dedicated mounted volume")
            self.directory = _prepare_bucket_directory(
                self.data_root, SNAPSHOT_DIR_NAME, self.bucket_name,
            )
            self._cleanup_incomplete()
            fd, probe_name = tempfile.mkstemp(prefix=".write-check-", dir=self.directory)
            os.close(fd)
            os.unlink(probe_name)
            self.enabled = True
            self.reason = "mounted /data snapshots are available"
        except OSError as exc:
            self.enabled = False
            self.reason = f"history is ephemeral ({exc})"
        return self.enabled

    def restore_latest(self) -> str:
        """Recover missing stores independently; never replace surviving local history."""
        if not self.enabled:
            return ""
        if not self._storage_available():
            self.enabled = False
            self.reason = "Hugging Face bucket identity or mounted /data is no longer available"
            return ""
        local_state = self._local_state()
        if local_state == "unsafe":
            return "Existing local database is unreadable; automatic restore skipped to protect it."
        need_chat = local_state == "empty"
        tenant_state = self._tenant_local_state()
        if not need_chat and tenant_state != "empty":
            return "Existing local chat and tenant history preserved; automatic restore skipped."
        candidates = sorted(
            self.directory.glob(SNAPSHOT_PREFIX + "*.json"),
            key=lambda path: path.name,
            reverse=True,
        )
        for manifest_path in candidates:
            chat_restored = False
            try:
                with _local_candidate(manifest_path, self.state_home) as (database, manifest):
                    if need_chat:
                        self._restore_database(database)
                        chat_restored = True
                    tenant_restored = self._restore_tenant_access(database)
                if not need_chat and not tenant_restored:
                    continue
                self.restore_note = (
                    ("Restored the latest validated chat/session snapshot from /data "
                     if need_chat else "Existing local chat history preserved; recovered missing tenant state from /data ")
                    + f"(created {manifest.get('created_at', 'unknown')})."
                    + (" Non-owner tenant access and approved conversation history were restored."
                       if tenant_restored else " Existing tenant state was preserved or no tenant recovery data was available.")
                )
                return self.restore_note
            except (OSError, ValueError, sqlite3.Error, TypeError, json.JSONDecodeError):
                if chat_restored:
                    # The two local files cannot be atomically replaced together.
                    # Once one is published, do not mix in an older snapshot or
                    # incorrectly claim all candidates were invalid. A later boot
                    # can retry only the still-missing tenant store.
                    self.restore_note = (
                        "Chat/session state restored; tenant recovery failed. Local chat state was preserved; "
                        "missing tenant state will be retried at next startup. Inspect private runtime logs."
                    )
                    return self.restore_note
                continue
        if not need_chat:
            self.restore_note = "Existing local chat history preserved; no valid recoverable tenant history was found."
            return self.restore_note
        if candidates:
            self.restore_note = "No valid /data history snapshot found; current local history is ephemeral."
        else:
            self.restore_note = "No prior snapshot; new chat history will be persisted after the first snapshot."
        return self.restore_note

    def _cleanup_incomplete(self) -> None:
        """Leave bucket artifacts untouched; listings may be eventually consistent."""
        return

    def _local_state(self) -> str:
        sidecars = [Path(str(self.db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        if not self.db_path.exists():
            return "unsafe" if any(path.exists() or path.is_symlink() for path in sidecars) else "empty"
        if any(path.is_symlink() or not path.is_file() for path in sidecars if path.exists()):
            return "unsafe"
        if self.db_path.is_symlink() or not self.db_path.is_file():
            return "unsafe"
        try:
            with closing(_connect_readonly(self.db_path)) as db:
                _quick_check(db)
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )}
                if "sessions" not in tables:
                    return "unsafe"
                if db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
                    return "meaningful"
                if "messages" in tables and db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
                    return "meaningful"
                if "gateway_routing" in tables and db.execute(
                    "SELECT 1 FROM gateway_routing LIMIT 1"
                ).fetchone():
                    return "meaningful"
                if "conversation_generations" in tables and db.execute(
                    "SELECT 1 FROM conversation_generations LIMIT 1"
                ).fetchone():
                    return "meaningful"
                for table in tables - {"schema_version"}:
                    if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                        return "meaningful"
                return "empty"
        except (OSError, sqlite3.Error, ValueError):
            return "unsafe"

    def _tenant_local_state(self) -> str:
        sidecars = [Path(str(self.tenant_db_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        if not self.tenant_db_path.exists():
            return "unsafe" if any(path.exists() or path.is_symlink() for path in sidecars) else "empty"
        if self.tenant_db_path.is_symlink() or not self.tenant_db_path.is_file():
            return "unsafe"
        if any(path.is_symlink() or not path.is_file() for path in sidecars if path.exists()):
            return "unsafe"
        try:
            with closing(_connect_readonly(self.tenant_db_path)) as db:
                _quick_check(db)
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )}
                if "tenant_documents" not in tables:
                    return "unsafe"
                if db.execute("SELECT 1 FROM tenant_documents LIMIT 1").fetchone():
                    return "meaningful"
                if "tenant_metadata" in tables and db.execute(
                    "SELECT 1 FROM tenant_metadata LIMIT 1"
                ).fetchone():
                    return "meaningful"
                return "empty"
        except (OSError, sqlite3.Error, ValueError):
            return "unsafe"

    def _restore_tenant_access(self, source: Path) -> bool:
        """Restore only reviewed non-owner access and chat history into an empty DB."""
        if self._tenant_local_state() != "empty":
            return False
        with closing(_connect_readonly(source, immutable=True)) as src:
            tables = {row[0] for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if "tenant_access" not in tables:
                return False
            columns = {row[1] for row in src.execute('PRAGMA table_info("tenant_access")')}
            selected = "tenant_id,user_id,allowed,banned,authorization_generation,credits,weight,start_count"
            if {"allowed_scopes", "sudo_scopes"}.issubset(columns):
                selected += ",allowed_scopes,sudo_scopes"
            rows = src.execute(f"SELECT {selected} FROM tenant_access").fetchall()
            histories = src.execute(
                "SELECT tenant_id,user_id,chat_id,room_key,revision,payload FROM tenant_history"
            ).fetchall() if "tenant_history" in tables else []
        if any(Path(str(self.tenant_db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            return False
        self.state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        _fd, name = tempfile.mkstemp(prefix=".tenant-restore-", suffix=".db", dir=self.state_home)
        staged = Path(name)
        try:
            with closing(sqlite3.connect(staged, timeout=30)) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("""CREATE TABLE tenant_documents(
                    collection TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL CHECK(json_valid(body)),
                    PRIMARY KEY(collection,id))""")
                db.execute("CREATE TABLE tenant_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                for row in rows:
                    tenant_id, user_id, allowed, banned, generation, credits, weight, starts = row[:8]
                    profile = {
                        "_id": tenant_id, "user_id": user_id, "profile_id": tenant_id,
                        "allowed": bool(allowed), "banned": bool(banned),
                        "authorization_generation": generation, "credits": credits,
                        "weight": weight, "start_count": starts,
                    }
                    if len(row) >= 10:
                        profile["allowed_scopes"] = json.loads(row[8])
                        profile["sudo_scopes"] = json.loads(row[9])
                    body = json.dumps(profile, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    db.execute(
                        "INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)",
                        ("hermes_tenant_profiles", tenant_id, body),
                    )
                # Restored authorization is authoritative; do not re-seed a
                # different image allowlist into this existing durable state.
                db.execute(
                    "INSERT INTO tenant_metadata(key,value) VALUES('initial_allowlist_seeded','1')"
                )
                for tenant_id, user_id, chat_id, room_key, revision, raw_payload in histories:
                    key = "history:" + room_key
                    data_id = hashlib.sha256(
                        (tenant_id + "\0profile\0" + key).encode("utf-8")
                    ).hexdigest()
                    body = json.dumps({
                        "_id": data_id, "tenant_id": tenant_id, "kind": "profile",
                        "key": key, "payload": json.loads(raw_payload), "revision": revision,
                    }, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    db.execute(
                        "INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)",
                        ("hermes_tenant_data", data_id, body),
                    )
                db.commit()
                _quick_check(db)
            if self._tenant_local_state() != "empty":
                raise ValueError("local tenant state changed during restore")
            if any(Path(str(self.tenant_db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                return False
            os.chmod(staged, 0o600)
            os.replace(staged, self.tenant_db_path)
            _fsync_directory(self.state_home)
            return bool(rows or histories)
        finally:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass

    def _restore_database(self, source: Path) -> None:
        self.state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_home.is_symlink():
            raise OSError("state home is a symlink")
        # Build and verify the restored DB at a private local staging path. Only
        # an atomic replacement publishes it as the live state.db.
        fd, temp_name = tempfile.mkstemp(prefix=".state-restore-", suffix=".db", dir=self.state_home)
        os.close(fd)
        staged = Path(temp_name)
        try:
            with closing(_connect_readonly(source, immutable=True)) as src, closing(sqlite3.connect(staged, timeout=30)) as dst:
                src.backup(dst)
                # The bundle carries tenant recovery rows, but those belong only
                # in tenant-state.db after recovery, never as stale live copies.
                dst.execute("DROP TABLE IF EXISTS tenant_access")
                dst.execute("DROP TABLE IF EXISTS tenant_history")
                dst.commit()
                _quick_check(dst)
            _validate_snapshot_database(staged)
            if self._local_state() != "empty":
                raise ValueError("local state changed during restore")
            if any(Path(str(self.db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise OSError("SQLite sidecars appeared during restore")
            os.chmod(staged, 0o600)
            os.replace(staged, self.db_path)
            _fsync_directory(self.state_home)
        finally:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass

    def snapshot(self) -> bool:
        """Publish a filtered, checksummed SQLite backup with a manifest commit marker."""
        if not self.enabled:
            return False
        if not self._storage_available():
            self.enabled = False
            self.reason = "Hugging Face bucket identity or mounted /data is no longer available"
            return False
        if self.db_path.is_symlink() or not self.db_path.is_file():
            return False
        local_tmp: Path | None = None
        pending_database: Path | None = None
        pending_manifest: Path | None = None
        try:
            _safe_directory(self.directory)
            fd, raw_name = tempfile.mkstemp(prefix=".raw-state-", suffix=".db", dir=self.state_home)
            os.close(fd)
            local_tmp = Path(raw_name)
            fd, filtered_name = tempfile.mkstemp(prefix=".filtered-state-", suffix=".db", dir=self.state_home)
            os.close(fd)
            filtered = Path(filtered_name)
            try:
                deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

                def check_deadline(_status: int, _remaining: int, _total: int) -> None:
                    if time.monotonic() > deadline:
                        raise TimeoutError("SQLite backup exceeded its time budget")

                with closing(_connect_readonly(self.db_path)) as live, closing(sqlite3.connect(local_tmp, timeout=60)) as raw:
                    live.backup(raw, pages=256, sleep=0.05, progress=check_deadline)
                    raw.commit()
                    _quick_check(raw)
                counts = _copy_allowlisted_database(local_tmp, filtered)
                tenant_counts = _copy_tenant_access(self.state_home, filtered)
                if not any((*counts, *tenant_counts)):
                    return False
                _validate_snapshot_database(filtered)
                fd, name = tempfile.mkstemp(prefix=".pending-", suffix=".sqlite3", dir=self.directory)
                os.close(fd)
                pending_database = Path(name)
                # Copy through a file handle, then fsync before atomic rename.
                with filtered.open("rb") as src, pending_database.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                digest = _sha256(pending_database)
                created_ns = time.time_ns()
                stem = f"{SNAPSHOT_PREFIX}{created_ns:020d}-{uuid.uuid4().hex[:12]}"
                final_database = self.directory / f"{stem}.sqlite3"
                final_manifest = self.directory / f"{stem}.json"
                os.replace(pending_database, final_database)
                pending_database = None
                manifest = {
                    "format": FORMAT_VERSION,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "database": final_database.name,
                    "size": final_database.stat().st_size,
                    "sha256": digest,
                    "tables": sorted(
                        row[0] for row in sqlite3.connect(filtered).execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        )
                    ),
                }
                fd, manifest_name = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=self.directory)
                pending_manifest = Path(manifest_name)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(pending_manifest, final_manifest)
                pending_manifest = None
                _fsync_directory(self.directory)
                self._prune_old_snapshots()
                self.last_snapshot_at = time.time()
                return True
            finally:
                try:
                    filtered.unlink()
                except OSError:
                    pass
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.last_error = type(exc).__name__
            return False
        finally:
            for path in (local_tmp, pending_database, pending_manifest):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def _prune_old_snapshots(self) -> None:
        valid: list[tuple[str, Path, Path]] = []
        for manifest_path in self.directory.glob(SNAPSHOT_PREFIX + "*.json"):
            try:
                with _local_candidate(manifest_path, self.state_home) as (_local, manifest):
                    database = self.directory / manifest["database"]
                    valid.append((manifest_path.name, manifest_path, database))
            except (OSError, ValueError, sqlite3.Error, TypeError, json.JSONDecodeError):
                continue
        valid.sort(reverse=True)
        for _name, manifest, database in valid[RETAIN_SNAPSHOTS:]:
            try:
                manifest.unlink()
                database.unlink()
            except OSError:
                continue
        _fsync_directory(self.directory)

    def status_message(self) -> str:
        if not self.enabled:
            return f"Ephemeral history: {self.reason}."
        if self.last_error:
            return f"Ephemeral history warning: latest snapshot failed ({self.last_error}); recent chat state remains local."
        suffix = f" {self.restore_note}" if self.restore_note else ""
        return "History snapshots are enabled on mounted /data; live SQLite/WAL remains local." + suffix
