"""Read-only Hugging Face runtime diagnostics for the Telegram support surface.

The report never calls a model/provider API or prepares the snapshot store.
With an existing scoped HF token it reads only the expected bucket's metadata;
without one it reports remote verification as unavailable.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DATA_ROOT = Path("/data")
MOUNTINFO = Path("/proc/self/mountinfo")
SNAPSHOT_DIR = "hermes-state"
SNAPSHOT_PREFIX = "chat-state-"
EXPECTED_BUCKET = "xkailashhh/hermes-artifacts"
_SQLITE_TIMEOUT_SECONDS = 1.0
_SQLITE_QUERY_BUDGET_SECONDS = 2.0
_READY_TIMEOUT_SECONDS = 1.5
MAX_SNAPSHOT_DIRECTORY_ENTRIES = 4096


def _is_mountpoint(path: Path) -> bool:
    """Use the same dedicated-mount rule as the actual snapshot writer."""
    from deploy.huggingface.persistence import is_mountpoint
    return is_mountpoint(path, mountinfo=MOUNTINFO)


def _safe_probe(path: Path) -> tuple[bool, str]:
    """Create and remove one uniquely named probe file without touching backups."""
    if path.is_symlink() or not path.is_dir():
        return False, "No safe existing directory is available for a write probe."
    probe = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".hermes-diagnostics-write-check-", dir=path)
        probe = Path(raw_path)
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)
        return True, "Temporary write probe succeeded and was removed."
    except OSError as exc:
        return False, f"Temporary write probe failed ({type(exc).__name__})."
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def _snapshot_inventory(directory: Path) -> tuple[int, str, int]:
    """Count complete non-symlink snapshot pairs and sum their on-disk bytes."""
    if directory.is_symlink() or not directory.is_dir():
        return 0, "unavailable", 0
    try:
        entries = {}
        deadline = time.monotonic() + 2.0
        with os.scandir(directory) as scanned:
            for index, entry in enumerate(scanned):
                if index >= MAX_SNAPSHOT_DIRECTORY_ENTRIES or time.monotonic() > deadline:
                    return -1, "directory scan limit exceeded", 0
                if entry.name.startswith(SNAPSHOT_PREFIX) and entry.name.endswith((".json", ".sqlite3")):
                    entries[entry.name] = directory / entry.name
    except OSError:
        return -1, "directory cannot be read", 0
    pairs: list[tuple[float, int]] = []
    for name, manifest in entries.items():
        if time.monotonic() > deadline:
            return -1, "directory scan time limit exceeded", 0
        if not name.startswith(SNAPSHOT_PREFIX) or not name.endswith(".json"):
            continue
        stem = name[:-5]
        database = entries.get(stem + ".sqlite3")
        if database is None or manifest.is_symlink() or database.is_symlink():
            continue
        try:
            if not manifest.is_file() or not database.is_file():
                continue
            manifest_stat, database_stat = manifest.stat(follow_symlinks=False), database.stat(follow_symlinks=False)
            pairs.append((manifest_stat.st_mtime, manifest_stat.st_size + database_stat.st_size))
        except OSError:
            continue
    if not pairs:
        return 0, "none", 0
    latest = max(stamp for stamp, _size in pairs)
    age = max(0, int(time.time() - latest))
    if age < 60:
        age_label = f"{age}s ago"
    elif age < 3600:
        age_label = f"{age // 60}m ago"
    elif age < 86400:
        age_label = f"{age // 3600}h ago"
    else:
        age_label = f"{age // 86400}d ago"
    return len(pairs), age_label, sum(size for _stamp, size in pairs)


def _query_deadline(connection: sqlite3.Connection, seconds: float = _SQLITE_QUERY_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise OSError("database is missing or symlinked")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    connection.execute(f"PRAGMA busy_timeout={int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
    connection.execute("PRAGMA query_only=ON")
    _query_deadline(connection)
    return connection


def _database_counts(path: Path, required_tables: tuple[str, ...]) -> tuple[str, dict[str, int]]:
    """Inspect table presence and simple row counts on a bounded read-only handle."""
    connection = None
    try:
        connection = _readonly_connection(path)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        counts = {}
        for table in required_tables:
            if table in tables:
                # Table identifiers are fixed literals supplied by this module.
                counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        missing = sorted(set(required_tables) - tables)
        if missing:
            return "schema incomplete: " + ", ".join(missing), counts
        check = connection.execute("PRAGMA quick_check(1)").fetchone()
        if check is None or check[0] != "ok":
            return "integrity check FAILED", counts
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        journal = journal if journal in {"delete", "truncate", "persist", "memory", "wal", "off"} else "unknown"
        return f"read-only check passed; quick_check ok; journal mode `{journal}`", counts
    except (sqlite3.Error, OSError, TimeoutError) as exc:
        return f"check unavailable ({type(exc).__name__})", {}
    finally:
        if connection is not None:
            connection.close()


def _hf_runtime() -> bool:
    """Recognize the HF supervisor environment without displaying its values."""
    if Path("/home/Hermes/hf/supervisor.py").is_file():
        return True
    return any(os.environ.get(name) for name in ("SPACE_ID", "HF_SPACE_ID", "HF_RUNTIME"))


def _supervisor_status() -> str:
    """Query only the fixed local supervisor endpoint; report process status, not readiness."""
    response = None
    status_code = None
    raw = b""
    try:
        response = urlopen("http://127.0.0.1:7860/readyz", timeout=_READY_TIMEOUT_SECONDS)
        status_code = getattr(response, "status", response.getcode())
        raw = response.read(4096)
    except HTTPError as exc:
        status_code = exc.code
        try:
            raw = exc.read(4096)
        except OSError:
            raw = b""
    except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return f"Not reachable on fixed loopback /readyz ({type(exc).__name__}); supervisor liveness is unverified."
    finally:
        if response is not None:
            response.close()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError):
        payload = None
    if isinstance(payload, dict):
        raw_status = payload.get("status")
        state = raw_status if isinstance(raw_status, str) and re.fullmatch(r"[A-Za-z_-]{1,32}", raw_status) else "unknown"
        note = payload.get("note")
        liveness_note = " Supervisor endpoint explicitly describes process liveness only." if isinstance(note, str) and "liveness" in note.lower() else ""
        detail = payload.get("detail")
        from agent.redact import redact_for_egress
        detail_note = (" Status detail: " + redact_for_egress(detail[:1500]).replace("\n", " ")
                       if isinstance(detail, str) else "")
        return f"HTTP {status_code}; supervisor state `{state}`.{liveness_note} This does not establish Telegram, model, or database readiness.{detail_note}"
    return f"HTTP {status_code}; response body was not parseable JSON. Supervisor readiness remains unverified."


def _identifier(value: Any, *, model: bool = False) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip()
    pattern = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}" if model else r"[A-Za-z][A-Za-z0-9_-]{0,39}"
    if not re.fullmatch(pattern, value) or "://" in value:
        return "not shown (not a safe identifier)"
    return value


def _model_config(home: Path) -> tuple[str, str, str]:
    config_path = home / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        return "unavailable", "unknown", "unknown"
    try:
        if config_path.stat().st_size > 1_048_576:
            return "unavailable (config exceeds inspection limit)", "unknown", "unknown"
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            return "unavailable (config is not a mapping)", "unknown", "unknown"
        model = config.get("model")
        if not isinstance(model, dict):
            return "read", "unknown", "unknown"
        return "read", _identifier(model.get("provider")), _identifier(model.get("default"), model=True)
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})", "unknown", "unknown"


def _hf_token() -> str:
    from gateway.platforms._shared import get_scoped_secret
    return str(get_scoped_secret("HF_TOKEN", "") or
               get_scoped_secret("HUGGING_FACE_HUB_TOKEN", "") or "").strip()


def _remote_bucket_status() -> str:
    """Use the official bucket_info endpoint, never expose the token or error body."""
    token = _hf_token()
    if not token:
        return "Remote bucket listing, account quotas, and remote connectivity: **unknown** (no scoped HF token; no remote requests were made)."
    import httpx
    try:
        # Fixed trusted origin, no redirects, no configurable endpoint carrying credentials.
        with httpx.Client(timeout=3.0, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", f"https://huggingface.co/api/buckets/{EXPECTED_BUCKET}",
                               headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status_code != 200:
                    return f"Remote bucket metadata: **unverified** (HTTP {response.status_code}; access or availability must be checked)."
                chunks = bytearray()
                deadline = time.monotonic() + 3.0
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 65536 or time.monotonic() > deadline:
                        raise ValueError("oversized metadata")
                metadata = json.loads(chunks)
        if not isinstance(metadata, dict) or metadata.get("id") != EXPECTED_BUCKET:
            raise ValueError("unexpected bucket identity")
        size, files, private = metadata.get("size"), metadata.get("totalFiles"), metadata.get("private")
        if type(size) is not int or size < 0 or type(files) is not int or files < 0 or type(private) is not bool:
            raise ValueError("unexpected bucket metadata")
        privacy = "private" if private else "PUBLIC — expected private; review permissions"
        return (f"Remote bucket metadata: **reachable**, `{EXPECTED_BUCKET}`, {privacy}; "
                f"{size:,} bytes across {files:,} files. Account quotas are not measured. "
                "This does not prove that /data is this bucket or that recent writes reached remote storage.")
    except (httpx.HTTPError, ValueError, TypeError):
        return "Remote bucket metadata: **unverified** (bounded request or metadata validation failed; credential and response details withheld)."


def collect_hf_diagnostics(home: Path, adapter: Any) -> str:
    """Collect local evidence plus optional read-only authenticated bucket metadata."""
    home = Path(home)
    lines = ["# Hugging Face runtime diagnostics", "", "## STARTUP"]
    hf_runtime = _hf_runtime()
    lines.append(f"- Hugging Face runtime detected: **{'yes' if hf_runtime else 'no / not identifiable'}**.")
    if hf_runtime:
        lines.append(f"- Local supervisor `/readyz`: {_supervisor_status()}")
        lines.append("- `/readyz` is process-liveness evidence only, not end-to-end readiness.")
    else:
        lines.append("- Local supervisor `/readyz`: **not queried** (runtime is not identified as Hugging Face).")

    lines.extend(["", "## DATABASE", "### Local state"])
    live_local = not home.resolve().is_relative_to(DATA_ROOT.resolve())
    lines.append(f"- SQLite runtime: `{sqlite3.sqlite_version}`; HF minimum 3.51.3: **{'passed' if sqlite3.sqlite_version_info >= (3, 51, 3) else 'FAILED'}**.")
    lines.append(f"- Selected profile state home: `{home}`; outside snapshot mount: **{'yes' if live_local else 'NO — unsafe live-state placement'}**.")
    state_path = home / "state.db"
    state_status, state_counts = _database_counts(state_path, ("sessions", "messages"))
    lines.append(f"- Live session database (`{state_path.name}`): {state_status}.")
    if state_counts:
        lines.append("- Read-only row counts: " + ", ".join(f"`{name}` {count:,}" for name, count in state_counts.items()) + ".")
    tenant_path = home / "tenant-state.db"
    tenant_status, tenant_counts = _database_counts(tenant_path, ("tenant_documents", "tenant_metadata"))
    lines.append(f"- Local Telegram tenant database (`{tenant_path.name}`): {tenant_status}.")
    if tenant_counts:
        lines.append("- Read-only row counts: " + ", ".join(f"`{name}` {count:,}" for name, count in tenant_counts.items()) + ".")

    lines.extend(["", "### Hugging Face bucket and snapshots"])
    data_root = DATA_ROOT
    is_mounted = _is_mountpoint(data_root)
    lines.append(f"- Dedicated `/data` mount: **{'detected' if is_mounted else 'not verified'}**.")
    snapshots = data_root / SNAPSHOT_DIR
    if is_mounted and not data_root.is_symlink():
        probe_target = snapshots if snapshots.exists() and not snapshots.is_symlink() and snapshots.is_dir() else data_root
        probe_ok, probe_text = _safe_probe(probe_target)
        lines.append(f"- `/data` write probe: **{'passed' if probe_ok else 'failed'}** — {probe_text}")
    else:
        lines.append("- `/data` write probe: **not run** (a dedicated mount was not verified).")
    count, age, total_bytes = _snapshot_inventory(snapshots)
    if count < 0:
        lines.append(f"- Snapshot inventory at `/data/hermes-state`: **unavailable** ({age}); counts and sizes were not measured.")
    else:
        lines.append(f"- Snapshot inventory at `/data/hermes-state`: {count} complete non-symlink pair(s); newest manifest mtime {age}; combined database + manifest size {total_bytes:,} bytes. Pair presence is not checksum or restore verification.")
    lines.append(f"- Expected private bucket label: `{EXPECTED_BUCKET}` (**expected, NOT verified identity**).")
    lines.append("- " + _remote_bucket_status())

    lines.extend(["", "## TELEGRAM"])
    client = getattr(adapter, "_client", None) if adapter is not None else None
    if client is None:
        lines.append("- Local Telegram transport: **unknown / not connected** (adapter client is absent).")
    else:
        try:
            connected = client.is_connected()
            lines.append(f"- Local Telegram transport (`adapter._client.is_connected()`): **{'connected' if connected else 'disconnected'}**; this is local transport status only, not proof of successful Telegram API operations.")
        except Exception as exc:
            lines.append(f"- Local Telegram transport status could not be read ({type(exc).__name__}); no Telegram request was made.")

    lines.extend(["", "## MODEL / PROVIDERS"])
    config_status, provider, model = _model_config(home)
    lines.append(f"- Local `config.yaml` model/provider identifiers ({config_status}): provider `{provider}`, model `{model}`.")
    lines.append("- Provider credentials and endpoint URLs are not displayed; provider network checks and live model API calls were not made.")

    report = "\n".join(lines)
    try:
        from agent.redact import redact_for_egress
        report = redact_for_egress(report)
    except Exception:
        # A redaction failure must not leak diagnostic content.
        return "# Hugging Face runtime diagnostics\n\nDiagnostics withheld because mandatory output redaction was unavailable."
    return report
