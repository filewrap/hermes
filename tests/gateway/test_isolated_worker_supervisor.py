"""Synthetic contracts only: no claims about live namespaces/cgroups/egress."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import struct
import threading
from types import SimpleNamespace

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied, Peer
from gateway.isolated_worker.admission import capture_admission
from gateway.isolated_worker.supervisor import (
    LaunchPlan, Limits, SupervisorClient, WorkerResources, production_ready,
    receive_frame, send_frame,
)


def binding(identity="11", **kw):
    return Binding(identity, "a" * 64, kw.get("chat_id", identity),
                   kw.get("chat_type", "dm"), kw.get("thread_id"))


@pytest.mark.parametrize("identity", ["../../owner", "/etc/shadow", "A", "B"])
def test_identity_never_becomes_host_path(identity):
    b = binding(identity)
    plan = LaunchPlan.create(b, uid=60001, runtime_digest="sha256:" + "b" * 64)
    spec = plan.specification()
    assert spec["mounts"]["/identity"] == "private-volume:" + b.key
    assert "/" not in b.key and "." not in b.key
    assert all(path.startswith("/identity/rooms/" + b.room + "/")
               for path in spec["directories"])
    assert spec["network"]["direct_access"] is False
    assert spec["security"]["host_proc"] is False
    assert spec["security"]["sudo"] is False
    assert spec["security"]["supplementary_groups"] == []
    assert spec["security"]["capabilities"] == []
    assert spec["security"]["no_new_privileges"] is True
    assert set(spec["namespaces"]) == {"mount", "pid", "ipc", "uts", "user", "network", "cgroup"}
    assert all(spec["lifecycle"].values())


def test_a_b_a_and_room_thread_resources(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-owner-canary")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setenv("MCP_CONFIG", "/owner/connectors")
    a, b, a2 = [WorkerResources(binding(i)) for i in ("11", "22", "11")]
    assert a == a2 and a.home != b.home
    assert a.environment() == a2.environment()
    assert "synthetic-owner-canary" not in json.dumps(a.environment())
    assert not {"OPENROUTER_API_KEY", "BROWSER_CDP_URL", "MCP_CONFIG", "SSH_AUTH_SOCK"} & a.environment().keys()
    for resource in ("memory", "tasks", "browser", "mcp", "connectors", "sessions", "processes"):
        assert a.home + "/" + resource in a.directories()
        assert b.home + "/" + resource in b.directories()
    group = WorkerResources(binding("11", chat_id="-100", chat_type="forum", thread_id="1"))
    other = WorkerResources(binding("11", chat_id="-100", chat_type="forum", thread_id="2"))
    assert len({a.home, b.home, group.home, other.home}) == 4


@pytest.mark.parametrize("kwargs", [
    {"memory_bytes": 1}, {"processes": 0}, {"processes": True}, {"cpu_quota_us": 0},
    {"wall_seconds": 0}, {"storage_bytes": 0}, {"cpu_period_us": 1},
])
def test_resource_limits_are_mandatory(kwargs):
    with pytest.raises(BoundaryDenied):
        Limits(**kwargs)


def test_no_root_or_missing_peer():
    for uid in (0, -1, True):
        with pytest.raises(BoundaryDenied):
            LaunchPlan.create(binding(), uid=uid, runtime_digest="sha256:" + "b" * 64)
    with pytest.raises(BoundaryDenied):
        Peer("a" * 32, binding(), 60001, "/sys/fs/cgroup/owner")
    assert production_ready() is False


def test_framing_rejects_oversize_duplicate_keys_and_nonfinite():
    for payload in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'{'):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(struct.pack("!I", len(payload)) + payload)
            with pytest.raises(BoundaryDenied):
                receive_frame(right)
    left, right = socket.socketpair()
    with left, right:
        left.sendall(struct.pack("!I", 2**31))
        with pytest.raises(BoundaryDenied):
            receive_frame(right)


def server(path, mutate=None):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path)); listener.listen(1)
    errors = []
    def run():
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    request = receive_frame(connection)
                    reply = {"version": 1, "binding": request["binding"],
                             "request_id": request["request_id"],
                             "result": {"final_response": "own answer", "usage": {}}}
                    if mutate:
                        mutate(reply)
                    send_frame(connection, reply)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def test_actual_unix_peer_credentials_and_bound_receipt(tmp_path):
    path = tmp_path / "supervisor.sock"
    thread, errors = server(path)
    client = SupervisorClient(path, supervisor_uid=os.getuid(), authorize=lambda _: True)
    assert client.turn(binding(), text="hello", request_id="one")["final_response"] == "own answer"
    thread.join(2)
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize("mutate", [
    lambda r: r["binding"].update(identity="22"),
    lambda r: r["binding"].update(authorization_stamp="b" * 64),
    lambda r: r.update(request_id="another"),
    lambda r: r["result"].update(command="host-shell"),
])
def test_cross_identity_stale_and_unexpected_receipts_deny(tmp_path, mutate):
    path = tmp_path / "supervisor.sock"
    thread, errors = server(path, mutate)
    client = SupervisorClient(path, supervisor_uid=os.getuid(), authorize=lambda _: True)
    with pytest.raises(BoundaryDenied):
        client.turn(binding(), text="hello", request_id="one")
    thread.join(2)
    assert not thread.is_alive() and not errors


def test_revocation_inflight_discards_reply(tmp_path):
    allowed = [True]
    path = tmp_path / "supervisor.sock"
    thread, errors = server(path, lambda _: allowed.__setitem__(0, False))
    client = SupervisorClient(path, supervisor_uid=os.getuid(), authorize=lambda _: allowed[0])
    with pytest.raises(BoundaryDenied):
        client.turn(binding(), text="hello", request_id="one")
    thread.join(2)
    assert not errors


def test_unavailable_supervisor_has_no_local_fallback(tmp_path, monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("gateway attempted local subprocess fallback")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    client = SupervisorClient(tmp_path / "absent.sock", supervisor_uid=60001, authorize=lambda _: True)
    with pytest.raises(BoundaryDenied, match="fallback forbidden"):
        client.turn(binding(), text="hello", request_id="one")


def source(uid="11", chat=None, kind="dm", thread=None):
    return SimpleNamespace(user_id=uid, chat_id=chat or uid, chat_type=kind, thread_id=thread)


def test_durable_admission_owner_global_everywhere_and_guests_deny(tmp_path):
    from agent.local_tenants import LocalTenantStore
    from agent.mongo_tenants import Principal
    store = LocalTenantStore(tmp_path, owner_user_id="99", allowed_user_ids=["11", "22"])
    for uid in ("99", "11", "22", "11"):
        for kind, chat in (("dm", uid), ("group", "-100"), ("forum", "-200")):
            a = capture_admission(store, source(uid, chat, kind, "7" if kind == "forum" else None))
            assert a.current(a.binding)
            assert a.binding.identity == uid
    with pytest.raises(BoundaryDenied):
        capture_admission(store, source("33"))
    # A scoped admin grant is still not a global allow identity.
    store.start_user("33")
    store.grant_sudo(Principal("99"), "33", "-100")
    with pytest.raises(BoundaryDenied):
        capture_admission(store, source("33", "-100", "group"))


def test_admission_generation_and_forged_source_fail_closed(tmp_path):
    from agent.local_tenants import LocalTenantStore
    from agent.mongo_tenants import Principal
    store = LocalTenantStore(tmp_path, owner_user_id="99", allowed_user_ids=["11"])
    s = source()
    admission = capture_admission(store, s)
    store.allow_user(Principal("99"), "11", False)
    assert not admission.current(admission.binding)
    store.allow_user(Principal("99"), "11", True)
    assert not admission.current(admission.binding)
    fresh = capture_admission(store, s)
    s.user_id = "99"
    assert not fresh.current(fresh.binding)
    with pytest.raises(BoundaryDenied):
        capture_admission(store, SimpleNamespace(owner=True))
