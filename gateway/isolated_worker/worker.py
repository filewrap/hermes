"""Unenabled, supervisor-internal whole-agent worker bootstrap.

This module is not a launcher and has no CLI. Production use requires a separately
provisioned supervisor to create the process, namespaces, dedicated UID, cgroup,
private room volume, network policy, and inherited authenticated Unix channel. A
channel peer credential is checked here, but this Python module does not attest
mount/network/cgroup isolation and is not an OS sandbox. ``production_ready()``
remains false until external provisioning and the live release gate are complete.

The supervisor is responsible for binding the inherited channel to one immutable
identity and for terminating the worker on revocation. The protocol cannot rebind
an existing process to a new or stale authorization stamp.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path, PurePosixPath
import socket
import stat
import struct
from typing import Any, Mapping

from . import Binding, BoundaryDenied
from .supervisor import WorkerResources, receive_frame, send_frame

MODEL = "openrouter/free"
PROVIDER = "openrouter"
BASE_URL = "https://openrouter.ai/api/v1"
_PLACEHOLDER_API_KEY = "isolated-worker-no-credential"
_MAX_TEXT_BYTES = 512 * 1024
_MAX_RESPONSE_BYTES = 512 * 1024


def _room_path(resources: WorkerResources, filesystem_root: Path) -> Path:
    """Map an in-root WorkerResources path onto a test root or the worker root."""
    home = resources.home
    expected = f"/identity/rooms/{resources.binding.room}"
    if home != expected:
        raise BoundaryDenied("Invalid worker room path")
    root = Path(filesystem_root)
    if not root.is_absolute() or ".." in root.parts:
        raise BoundaryDenied("Invalid worker filesystem root")
    # The caller cannot use a symlink as an alternate root. Production passes '/';
    # tests use a real temporary directory standing in for the private mount root.
    if root.is_symlink():
        raise BoundaryDenied("Invalid worker filesystem root")
    path = root.joinpath(*PurePosixPath(home).parts[1:])
    if not path.is_absolute() or ".." in path.parts:
        raise BoundaryDenied("Invalid worker room path")
    return path


def _ensure_real_directory(path: Path, *, create: bool = False, owner_uid: int | None = None,
                           private: bool = False) -> None:
    """Reject symlinks and non-directories along an identity-owned path."""
    try:
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError
        if owner_uid is not None and info.st_uid != owner_uid:
            raise ValueError
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError
        if private and info.st_mode & 0o077:
            raise ValueError
    except (OSError, ValueError):
        raise BoundaryDenied("Worker room resources are not private directories") from None


def prepare_worker_resources(resources: WorkerResources, *, filesystem_root: Path = Path("/"),
                             owner_uid: int | None = None) -> Path:
    """Validate the private room mount and its directories; return workspace path.

    ``filesystem_root`` exists solely to exercise real path checks against a temp
    mount in tests. Production callers use the worker's '/' mount namespace.
    Room roots must already have been provisioned by the supervisor; child paths
    are created only beneath that verified, worker-owned room.
    """
    if not isinstance(resources, WorkerResources):
        raise BoundaryDenied("Invalid worker resources")
    uid = os.getuid() if owner_uid is None else owner_uid
    room = _room_path(resources, Path(filesystem_root))
    root = Path(filesystem_root)
    # Walk from the supplied root, checking each existing component without
    # resolving away a symlink. identity/rooms and the room itself are supervisor
    # provisioned; only the known subdirectories may be created here.
    for component in (root / "identity", root / "identity" / "rooms"):
        _ensure_real_directory(component)
    _ensure_real_directory(room, owner_uid=uid, private=True)
    expected_dirs = tuple(room / name for name in (
        "workspace", "memory", "skills", "tasks", "sessions", "browser",
        "mcp", "connectors", "approvals", "processes", "config", "cache", "data",
    ))
    for directory in expected_dirs:
        _ensure_real_directory(directory, create=True, owner_uid=uid, private=True)
    return room / "workspace"


def validate_worker_environment(resources: WorkerResources, environ: Mapping[str, str] | None = None) -> None:
    """Require the freshly provisioned process environment to be exactly allowlisted.

    Unexpected variables are rejected rather than scrubbed: clearing an inherited
    credential after the process has already imported code is not an isolation
    contract. The supervisor must start a clean process with this exact mapping.
    """
    actual = os.environ if environ is None else environ
    expected = resources.environment()
    if (set(actual) != set(expected)
            or any(actual.get(key) != value for key, value in expected.items())):
        raise BoundaryDenied("Worker process environment is not clean")


def _validate_inference_client(client: Any) -> None:
    """Accept only the already-bound worker-side OpenAI-shaped RPC facade."""
    from .inference import IsolatedOpenAIClient

    if not isinstance(client, IsolatedOpenAIClient):
        raise BoundaryDenied("Worker inference client is not bound")
    completions = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
    if not callable(completions) or not callable(getattr(client, "with_options", None)):
        raise BoundaryDenied("Worker inference client is not bound")


def _worker_agent_kwargs(binding: Binding, workspace: Path) -> dict[str, Any]:
    """The sole fixed agent configuration; no gateway callbacks or env files."""
    return {
        "api_key": _PLACEHOLDER_API_KEY,
        "base_url": BASE_URL,
        "provider": PROVIDER,
        "api_mode": "chat_completions",
        "model": MODEL,
        "fallback_model": None,
        "enabled_toolsets": None,
        "disabled_toolsets": None,
        "save_trajectories": False,
        "platform": "isolated_worker",
        "session_id": "worker-" + binding.room,
        "user_id": binding.identity,
        "chat_id": binding.chat_id,
        "chat_type": binding.chat_type,
        "thread_id": binding.thread_id,
        "skip_context_files": False,
        "skip_memory": False,
        "cwd": str(workspace),
    }


def _create_worker_agent(binding: Binding, resources: WorkerResources, inference_client: Any,
                          *, filesystem_root: Path = Path("/")) -> Any:
    """Construct a room-bound AIAgent using an already-bound inference RPC client.

    Internal to a separately provisioned worker service. ``inference_client`` must
    be the concrete ``IsolatedOpenAIClient`` facade created from the inherited,
    supervisor-authenticated inference channel and capability; arbitrary callbacks,
    gateway clients and provider credentials are not accepted. No provider .env or
    host configuration is loaded by this factory.
    """
    if not isinstance(binding, Binding) or not isinstance(resources, WorkerResources) or resources.binding != binding:
        raise BoundaryDenied("Worker binding does not match resources")
    validate_worker_environment(resources)
    workspace = prepare_worker_resources(resources, filesystem_root=filesystem_root)
    _validate_inference_client(inference_client)

    # Import the large runtime only after the clean environment and private paths
    # have been verified. The subclass overrides the one client-construction hook
    # used by init and all refresh/rebuild paths, so none can instantiate OpenAI.
    from run_agent import AIAgent

    class _BoundWorkerAgent(AIAgent):
        def __init__(self, *, _inference_client: Any, **kwargs: Any):
            self._worker_inference_client = _inference_client
            self._worker_binding = binding
            super().__init__(**kwargs)

        def _create_openai_client(self, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
            # Ignore neither route nor credential changes: fail closed if a future
            # refresh tries to escape the fixed provider/model configuration.
            if (getattr(self, "provider", None) != PROVIDER
                    or getattr(self, "model", None) != MODEL
                    or getattr(self, "api_mode", None) != "chat_completions"
                    or not isinstance(client_kwargs, dict)
                    or client_kwargs.get("api_key") != _PLACEHOLDER_API_KEY
                    or client_kwargs.get("base_url") != BASE_URL):
                raise BoundaryDenied("Worker inference route cannot be changed")
            return self._worker_inference_client

    agent = _BoundWorkerAgent(_inference_client=inference_client, **_worker_agent_kwargs(binding, workspace))
    if (getattr(agent, "model", None) != MODEL
            or getattr(agent, "provider", None) != PROVIDER
            or getattr(agent, "_fallback_chain", None) not in (None, [])):
        try:
            agent.close()
        finally:
            raise BoundaryDenied("Worker agent route was changed during initialization")
    return agent


def _binding_from_wire(value: Any) -> Binding:
    if not isinstance(value, dict) or set(value) != {"identity", "authorization_stamp", "chat_id", "chat_type", "thread_id"}:
        raise BoundaryDenied("Invalid worker bootstrap")
    try:
        return Binding(**value)
    except (TypeError, BoundaryDenied):
        raise BoundaryDenied("Invalid worker bootstrap") from None


def _check_peer(channel: socket.socket, supervisor_uid: int) -> None:
    """Require the inherited stream to be a connected Unix socket from the provisioned service."""
    if (not isinstance(channel, socket.socket) or channel.family != socket.AF_UNIX
            or type(supervisor_uid) is not int or supervisor_uid < 0
            or supervisor_uid == os.getuid()):
        raise BoundaryDenied("Worker bootstrap channel is not trusted")
    try:
        if channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError
        _, uid, _ = struct.unpack("3i", channel.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != supervisor_uid:
            raise ValueError
        channel.getpeername()
    except (OSError, ValueError, struct.error):
        raise BoundaryDenied("Worker bootstrap channel is not trusted") from None


def _turn_from_wire(frame: dict[str, Any], binding: Binding) -> tuple[str, str]:
    if (not isinstance(frame, dict)
            or set(frame) != {"version", "operation", "binding", "request_id", "text"}
            or type(frame["version"]) is not int or frame["version"] != 1
            or frame["operation"] != "turn"
            or frame["binding"] != asdict(binding)):
        # A newer or older authorization stamp requires a fresh supervised worker;
        # an existing room process is never rebound across authorization epochs.
        raise BoundaryDenied("Worker turn binding is stale or changed")
    request_id, text = frame["request_id"], frame["text"]
    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
            or not isinstance(text, str) or not text
            or len(text.encode("utf-8")) > _MAX_TEXT_BYTES):
        raise BoundaryDenied("Invalid worker turn")
    return request_id, text


def _json_object(value: Any, *, max_bytes: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise ValueError
        return decoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BoundaryDenied("Invalid worker result") from None


def serve_worker(channel: socket.socket, *, supervisor_uid: int, inference_client: Any,
                 filesystem_root: Path = Path("/")) -> None:
    """Serve a fixed-binding worker over an inherited supervisor-owned channel.

    The calling service must have created this process inside its provisioned OS
    boundary and pass the connected inherited channel plus the already-bound
    ``IsolatedOpenAIClient``. This function is deliberately not a CLI or gateway
    launcher. The supervisor must authorize every turn and tear down this worker
    on revoke/disconnect; Python-side protocol checks do not attest OS isolation.
    """
    from .supervisor import production_ready
    if production_ready() is not True:
        raise BoundaryDenied("Isolated worker production release gate has not passed")
    _check_peer(channel, supervisor_uid)
    bootstrap = receive_frame(channel)
    if (set(bootstrap) != {"version", "operation", "binding"}
            or type(bootstrap["version"]) is not int or bootstrap["version"] != 1
            or bootstrap["operation"] != "bootstrap"):
        raise BoundaryDenied("Invalid worker bootstrap")
    binding = _binding_from_wire(bootstrap["binding"])
    resources = WorkerResources(binding)
    agent = _create_worker_agent(binding, resources, inference_client, filesystem_root=filesystem_root)
    send_frame(channel, {"version": 1, "operation": "ready", "binding": asdict(binding)})
    try:
        while True:
            frame = receive_frame(channel)
            request_id, text = _turn_from_wire(frame, binding)
            try:
                result = agent.run_conversation(text)
                if not isinstance(result, dict) or not isinstance(result.get("final_response"), str):
                    raise BoundaryDenied("Invalid worker result")
                body = {
                    "final_response": result["final_response"],
                    "usage": _json_object(result.get("usage") or {}, max_bytes=64 * 1024),
                }
                if len(body["final_response"].encode("utf-8")) > _MAX_RESPONSE_BYTES:
                    raise BoundaryDenied("Invalid worker result")
            except BoundaryDenied:
                raise
            except Exception:
                # Never return provider, filesystem, tool, or host exception details.
                raise BoundaryDenied("Worker turn failed") from None
            send_frame(channel, {"version": 1, "binding": asdict(binding),
                                "request_id": request_id, "result": body})
    finally:
        try:
            agent.close()
        except Exception:
            pass
