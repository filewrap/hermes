#!/usr/bin/env python3
"""Small HF front door; explicitly opt into a gateway with volatile local state."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable
from persistence import SnapshotStore, is_mountpoint
from full_state import FullStateStore

APP_DIR = Path("/home/Hermes/hermes-agent")
HOME_ROOT = Path("/home/Hermes")
PORT = int(os.environ.get("HF_PORT", "7860"))
MODE = os.environ.get("HF_STATE_MODE", "unconfigured").strip().lower()
ALLOW_EPHEMERAL = os.environ.get("HF_ALLOW_EPHEMERAL") == "1"
STATE_HOME = Path(os.environ.get("HERMES_HOME", "/home/Hermes/.hermes"))
SNAPSHOT_INTERVAL_SECONDS = 60.0
child: subprocess.Popen | None = None
lock = threading.Lock()
stop = threading.Event()
status = "starting"
detail = "Checking explicit state configuration."


def publish(state: str, message: str = "") -> None:
    global status, detail
    with lock:
        status, detail = state, message


def mount_type(path: Path) -> str | None:
    """Use the most specific Linux mount; inability to inspect fails closed."""
    try:
        target = path.resolve(strict=False)
        best = None
        for row in (
            Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        ):
            left, right = row.split(" - ", 1)
            encoded = left.split()[4]
            for escaped, char in (
                ("\\040", " "),
                ("\\011", "\t"),
                ("\\012", "\n"),
                ("\\134", "\\"),
            ):
                encoded = encoded.replace(escaped, char)
            point = Path(encoded)
            if point != target and point not in target.parents:
                continue
            if best is None or len(str(point)) > best[0]:
                best = (len(str(point)), right.split()[0])
        return best[1] if best else None
    except (OSError, ValueError, IndexError):
        return None


def preflight(
    home: Path = STATE_HOME,
    mode: str = MODE,
    allow: bool = ALLOW_EPHEMERAL,
    base: Path = HOME_ROOT,
) -> str | None:
    if mode not in {"local", "ephemeral"}:
        return "Gateway disabled. Set HF_STATE_MODE=ephemeral and HF_ALLOW_EPHEMERAL=1 to accept volatile local state."
    if not allow:
        return "Gateway disabled. Set HF_ALLOW_EPHEMERAL=1 to acknowledge possible state loss."
    if not home.is_absolute() or ".." in home.parts:
        return "HERMES_HOME must be an absolute normalized path."
    if any(p.is_symlink() for p in (home, *home.parents)):
        return "Refusing symlinked or symlink-traversing HERMES_HOME."
    if home == Path("/data") or Path("/data") in home.parents:
        return "Refusing /data as live state; use it only for closed artifacts."
    if base not in home.parents:
        return "HERMES_HOME must be an isolated local path under /home/Hermes."
    reserved = (base / "hermes-agent", base / "hf", base / "hf-tools", base / ".cache")
    if any(home == p or p in home.parents or home in p.parents for p in reserved):
        return "HERMES_HOME must not overlap application, helper, tool or cache directories."
    fs = mount_type(home)
    if not fs:
        return "Cannot establish state filesystem type; refusing state writes."
    # Explicit allowlist: not identifying a filesystem is not proof of SQLite safety.
    if fs not in {"overlay", "ext4", "ext3", "ext2", "xfs", "btrfs", "tmpfs", "zfs"}:
        return (
            "Unsupported live-state filesystem; use ordinary container-local storage."
        )
    return None


def gateway_command() -> list[str]:
    return [sys.executable, "-m", "hermes_cli.main", "gateway", "run"]


def setup_state(
    home: Path, config: str | None, archive: str | None, _helper_dir: Path | None = None
) -> None:
    if archive:
        path = Path(archive)
        if (
            not path.is_absolute()
            or path.suffixes[-2:] != [".tar", ".gz"]
            or not path.is_file()
        ):
            raise ValueError(
                "HF_RESTORE_ARCHIVE must name an existing absolute .tar.gz"
            )
        if home.exists() and (not home.is_dir() or any(home.iterdir())):
            raise ValueError("restore destination is not empty; refusing merge")
        # Running this file makes its own helper directory the import root.
        from snapshot import restore

        home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        restore(path, home)
    else:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    dest = home / "config.yaml"
    if dest.is_symlink():
        raise ValueError("symlinked config refused")
    if config:
        path = Path(config)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("HF_CONFIG_FILE must name an existing absolute file")
        if not dest.exists():
            # No-clobber: existing or restored configuration always wins.
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with path.open("rb") as source, os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target)
    elif not dest.exists():
        # First-run setup must work without a dataset mount or an interactive CLI.
        # Explicit free-only defaults; runtime guards also protect restored config.
        import yaml
        from free_policy import fresh_config
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write("# Generated on first HF startup; credentials stay in Space secrets.\n")
            yaml.safe_dump(fresh_config(), target, sort_keys=False)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ValueError("Existing or supplied config.yaml must be a nonempty file")


def signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def terminate_group(proc: subprocess.Popen) -> None:
    # Also terminate descendants if their original gateway leader has already exited.
    signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        signal_group(proc, signal.SIGKILL)
        proc.wait()
    finally:
        # The leader can exit before a stubborn child; clean the whole session.
        signal_group(proc, signal.SIGKILL)


def main(
    command: Callable[[], list[str]] = gateway_command,
    state_home: Path = STATE_HOME,
    *,
    app_dir: Path = APP_DIR,
    base: Path = HOME_ROOT,
    host: str = "0.0.0.0",
    port: int = PORT,
) -> int:
    """Command/root injection is only a Python testing seam, not a runtime env bypass."""
    global child
    stop.clear()
    child = None
    publish("starting", "Checking explicit state configuration.")

    def shutdown(_sig: int, _frame: object) -> None:
        global status
        # A signal can interrupt the main thread while it owns `lock`.
        if status != "failed":
            status = "stopping"
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    # Bind BEFORE preflight, archive I/O, imports or gateway initialization.
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    failed = False
    persistence: SnapshotStore | None = None
    full_persistence: FullStateStore | None = None
    full_state_enabled = False
    full_state_status = "not configured"
    next_snapshot_at = 0.0

    def persistence_status() -> str:
        chat = persistence.status_message() if persistence is not None else "Chat history backup unavailable."
        return f"{chat} Full-profile bucket backup: {full_state_status}."

    try:
        failure = preflight(state_home, MODE, ALLOW_EPHEMERAL, base)
        if failure:
            publish("unconfigured", failure)
            print("HF gateway not started: " + failure, flush=True)
        else:
            try:
                from check_runtime import check_gateway_loop, check_sqlite
                check_sqlite()
                loop_info = check_gateway_loop()
                import sqlite3
                print(f"HF SQLite verified: {sys.executable} uses {sqlite3.sqlite_version}", flush=True)
                print(f"HF event loop verified: {loop_info}", flush=True)
                # Force policy even if a Space variable tried to disable it.
                os.environ["HERMES_STRICT_FREE_ONLY"] = "1"
                try:
                    from agent import strict_free_policy
                except ImportError as exc:
                    raise RuntimeError("HF startup blocked: pinned source lacks strict free-only guards; sync the reviewed Dockerfile and rebuild") from exc
                if getattr(strict_free_policy, "POLICY_VERSION", 0) < 1:
                    raise RuntimeError("HF startup blocked: pinned source lacks the required free-only policy capability")
                from free_policy import validate_config
                from hermes_cli.config import read_user_config_raw

                full_persistence = FullStateStore(state_home)
                full_state_enabled = full_persistence.prepare()
                if full_state_enabled:
                    full_state_status = "enabled on verified mounted /data"
                    if not os.environ.get("HF_RESTORE_ARCHIVE"):
                        print("HF " + full_persistence.restore_latest(), flush=True)
                else:
                    full_state_status = "disabled; requires SPACE_ID and writable mounted /data"
                    print("HF full-profile backup unavailable; /data is not a writable mounted volume.", flush=True)
                setup_state(
                    state_home,
                    os.environ.get("HF_CONFIG_FILE"),
                    os.environ.get("HF_RESTORE_ARCHIVE"),
                )
                # Runtime-only policy is scoped to this exact launch home.
                try:
                    from gateway.mongo_multiuser import hf_runtime_environment
                except ImportError as exc:
                    raise RuntimeError("HF startup blocked: pinned source lacks the runtime-only multiuser bridge; sync the reviewed Dockerfile and rebuild") from exc
                gateway_env = hf_runtime_environment(state_home, os.environ)
                # /data is only a snapshot destination when it is an actual
                # dedicated mount. An image-layer directory is never treated as
                # durable storage; the gateway still runs with explicit ephemeral
                # opt-in and status makes that loss risk visible.
                persistence = SnapshotStore(
                    state_home,
                    mount_check=lambda path: full_state_enabled and is_mountpoint(path),
                )
                persistence.prepare()
                persistence.restore_latest()
                print("HF " + persistence.status_message(), flush=True)
                saved_config = read_user_config_raw(state_home / "config.yaml")
                validate_config(saved_config)
                strict_free_policy.assert_config_safe(saved_config)
                print("HF strict free-only policy active: openrouter/free; no alternate providers or model fallbacks", flush=True)
                if not os.access(state_home, os.W_OK):
                    raise PermissionError("State directory is not writable")
                if not stop.is_set():
                    gateway_env["HERMES_HOME"] = str(state_home)
                    proc = subprocess.Popen(
                        command(), cwd=app_dir, start_new_session=True, env=gateway_env
                    )
                    with lock:
                        child = proc
                    publish("running", persistence_status())
                    next_snapshot_at = time.monotonic() + SNAPSHOT_INTERVAL_SECONDS
                    print(
                        "Hermes gateway subprocess started; verify upstream connectivity separately.",
                        flush=True,
                    )
            except Exception as exc:
                if isinstance(exc, (ValueError, RuntimeError)) and str(exc).startswith(("HF free-only", "HF startup blocked")):
                    print(str(exc), file=sys.stderr, flush=True)
                failed = True
                publish(
                    "failed",
                    "State setup or gateway launch failed; inspect private runtime logs.",
                )
                print(
                    f"HF startup failed ({type(exc).__name__}); check config, archive and permissions.",
                    file=sys.stderr,
                    flush=True,
                )
                stop.set()
        while not stop.wait(0.1):
            with lock:
                proc = child
            if proc is not None and proc.poll() is not None:
                failed = True
                publish(
                    "failed", f"Gateway subprocess exited with code {proc.returncode}."
                )
                print(
                    f"HF gateway exited unexpectedly (code {proc.returncode}).",
                    file=sys.stderr,
                    flush=True,
                )
                stop.set()
                continue
            if proc is not None and (
                (persistence is not None and persistence.enabled)
                or (full_persistence is not None and full_state_enabled)
            ):
                if time.monotonic() >= next_snapshot_at:
                    if full_persistence is not None and full_state_enabled:
                        try:
                            full_archive = full_persistence.snapshot()
                            if full_archive is not None:
                                full_state_status = "enabled; latest full-profile snapshot verified"
                                print("HF full-profile snapshot verified on mounted /data.", flush=True)
                            else:
                                full_state_status = "snapshot unavailable; live state remains local"
                                print("HF full-profile snapshot unavailable; live state remains local.", file=sys.stderr, flush=True)
                        except Exception as exc:
                            full_state_status = f"snapshot failed ({type(exc).__name__}); live state remains local"
                            print(f"HF full-profile snapshot failed ({type(exc).__name__}); live state remains local.", file=sys.stderr, flush=True)
                    if persistence is not None and persistence.enabled:
                        if persistence.snapshot():
                            persistence.last_error = None
                            print("HF chat/session snapshot published to mounted /data.", flush=True)
                        elif persistence.last_error:
                            print(
                                "HF chat/session snapshot failed; recent history remains local "
                                f"({persistence.last_error}).",
                                file=sys.stderr,
                                flush=True,
                            )
                    publish("running", persistence_status())
                    next_snapshot_at = time.monotonic() + SNAPSHOT_INTERVAL_SECONDS
    finally:
        with lock:
            proc = child
        if proc is not None:
            terminate_group(proc)
        if full_persistence is not None and full_state_enabled:
            try:
                if full_persistence.snapshot() is not None:
                    full_state_status = "enabled; final full-profile snapshot verified"
                    print("HF final full-profile snapshot verified on mounted /data.", flush=True)
                else:
                    full_state_status = "final snapshot unavailable; live state remains local"
                    print("HF final full-profile snapshot unavailable; live state remains local.", file=sys.stderr, flush=True)
            except Exception as exc:
                full_state_status = f"final snapshot failed ({type(exc).__name__}); live state remains local"
                print(f"HF final full-profile snapshot failed ({type(exc).__name__}); live state remains local.", file=sys.stderr, flush=True)
        # The gateway is fully stopped before the SQLite backup. That final
        # snapshot closes the graceful-shutdown gap; periodic backups cover OOMs.
        if persistence is not None and persistence.enabled:
            if persistence.snapshot():
                persistence.last_error = None
                print("HF final chat/session snapshot published to mounted /data.", flush=True)
            elif persistence.last_error:
                print(
                    "HF final chat/session snapshot failed; recent history remains local "
                    f"({persistence.last_error}).",
                    file=sys.stderr,
                    flush=True,
                )
            if status != "failed":
                publish("stopping", persistence_status())
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return 1 if failed else 0


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        with lock:
            current, message, proc = status, detail, child
        alive = proc is not None and proc.poll() is None
        if proc is not None and not alive:
            current, message = "failed", "Gateway subprocess exited."
        if self.path == "/healthz":
            code = 503 if current == "failed" else 200
            body = {"status": "failed" if current == "failed" else "alive"}
        elif self.path == "/readyz":
            code = 200 if current == "running" and alive else 503
            body = {
                "status": current,
                "detail": message,
                "note": "Process liveness only; messaging, model and database readiness are not tested.",
            }
        elif self.path in {"/", "/index.html"}:
            code = 503 if current == "failed" else 200
            body = {
                "service": "Hermes Agent",
                "gateway": current,
                "detail": message,
                "health": "/healthz",
                "readiness": "/readyz",
            }
        else:
            code, body = 404, {"error": "not found"}
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _fmt: str, *_args: object) -> None:
        # Do not echo arbitrary request URLs or query-string credentials into logs.
        return


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            f"HF supervisor failed ({type(exc).__name__}).", file=sys.stderr, flush=True
        )
        raise SystemExit(1)
