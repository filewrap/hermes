"""Synthetic bootstrap contracts; these tests do not prove OS isolation."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied
from gateway.isolated_worker.inference import IsolatedOpenAIClient
from gateway.isolated_worker.supervisor import WorkerResources
from gateway.isolated_worker import worker


@pytest.fixture(autouse=True)
def import_agent_before_synthetic_mount_environment():
    # run_agent intentionally captures its process home at import. These tests
    # emulate a worker mount using /identity paths that do not exist on the test
    # host. Import against pytest's real temporary home first, so the synthetic
    # environment cannot freeze a nonexistent worker path into later tests.
    import run_agent


def _binding(identity: str, stamp: str = "a" * 64) -> Binding:
    return Binding(identity, stamp, "room-" + identity, "dm")


def _make_room(root: Path, resources: WorkerResources) -> None:
    room = root / "identity" / "rooms" / resources.binding.room
    room.mkdir(parents=True, mode=0o700, exist_ok=True)
    room.chmod(0o700)
    # prepare_worker_resources rejects group/other-writable ancestors, and
    # mkdir(parents=True) creates the intermediate identity/rooms dirs with the
    # process umask (007 in this sandbox leaves them 0770). Pin the
    # supervisor-provisioned parents private, as a real supervisor would.
    for parent in (root / "identity", root / "identity" / "rooms"):
        parent.chmod(0o700)


def _client() -> IsolatedOpenAIClient:
    # The callback is a synthetic RPC fixture, not a gateway callback or provider client.
    return IsolatedOpenAIClient(lambda _capability, _request: {"choices": []}, "test-capability")


def test_real_resources_route_a_b_a_without_gateway_environment(monkeypatch, tmp_path):
    bindings = [_binding("tenant-A"), _binding("tenant-B"), _binding("tenant-A")]
    resources = [WorkerResources(item) for item in bindings]
    for item in resources:
        _make_room(tmp_path, item)

    # Import and instantiate the real AIAgent subclass/factory, while replacing
    # only its expensive initializer so no tool, service, or model is launched.
    from agent import agent_init

    calls = []

    def fake_init(agent, **kwargs):
        calls.append(dict(kwargs))
        agent.model = kwargs["model"]
        agent.provider = kwargs["provider"]
        agent.api_mode = kwargs["api_mode"]
        agent._fallback_chain = []
        agent._client_kwargs = {"api_key": kwargs["api_key"], "base_url": kwargs["base_url"]}
        agent.client = agent._create_openai_client(dict(agent._client_kwargs), reason="test-init", shared=True)

    monkeypatch.setattr(agent_init, "init_agent", fake_init)
    clients = [_client() for _ in bindings]
    agents = []
    for resource, client in zip(resources, clients):
        monkeypatch.setattr(worker.os, "environ", dict(resource.environment()))
        agent = worker._create_worker_agent(resource.binding, resource, client, filesystem_root=tmp_path)
        agents.append(agent)
        assert agent._worker_binding == resource.binding
        assert agent.client is client
        assert set(os.environ) == set(resource.environment())
        assert not any("TELEGRAM" in name or "OPENROUTER_API_KEY" in name for name in os.environ)

    assert [call["user_id"] for call in calls] == ["tenant-A", "tenant-B", "tenant-A"]
    assert [call["model"] for call in calls] == ["openrouter/free"] * 3
    assert [call["provider"] for call in calls] == ["openrouter"] * 3
    assert [call["api_key"] for call in calls] == [worker._PLACEHOLDER_API_KEY] * 3
    assert [call["fallback_model"] for call in calls] == [None] * 3
    assert [call["cwd"] for call in calls] == [
        str(tmp_path / "identity" / "rooms" / item.binding.room / "workspace") for item in resources
    ]
    assert agents[0]._worker_binding == agents[2]._worker_binding
    assert agents[0]._worker_binding != agents[1]._worker_binding
    assert agents[0].client is not agents[1].client


def test_factory_rejects_environment_leaks_and_client_refresh_route_changes(monkeypatch, tmp_path):
    binding = _binding("tenant-A")
    resources = WorkerResources(binding)
    _make_room(tmp_path, resources)
    monkeypatch.setattr(worker.os, "environ", {**resources.environment(), "TELEGRAM_BOT_TOKEN": "synthetic-secret"})
    with pytest.raises(BoundaryDenied, match="environment"):
        worker._create_worker_agent(binding, resources, _client(), filesystem_root=tmp_path)

    monkeypatch.setattr(worker.os, "environ", dict(resources.environment()))
    from agent import agent_init

    def fake_init(agent, **kwargs):
        agent.model = kwargs["model"]
        agent.provider = kwargs["provider"]
        agent.api_mode = kwargs["api_mode"]
        agent._fallback_chain = []
        agent._client_kwargs = {"api_key": kwargs["api_key"], "base_url": kwargs["base_url"]}
        agent.client = agent._create_openai_client(dict(agent._client_kwargs), reason="test-init", shared=True)

    monkeypatch.setattr(agent_init, "init_agent", fake_init)
    client = _client()
    agent = worker._create_worker_agent(binding, resources, client, filesystem_root=tmp_path)
    # Refreshes return the same bound RPC facade, never instantiate an SDK client.
    assert agent._create_openai_client(dict(agent._client_kwargs), reason="refresh", shared=True) is client
    with pytest.raises(BoundaryDenied, match="route cannot be changed"):
        agent._create_openai_client({"api_key": worker._PLACEHOLDER_API_KEY, "base_url": "https://attacker.invalid/v1"}, reason="refresh", shared=True)


def test_protocol_rejects_stale_authorization_stamp_without_rebinding():
    current = _binding("tenant-A", stamp="a" * 64)
    frame = {
        "version": 1,
        "operation": "turn",
        "binding": asdict(_binding("tenant-A", stamp="b" * 64)),
        "request_id": "request-1",
        "text": "hello",
    }
    with pytest.raises(BoundaryDenied, match="stale or changed"):
        worker._turn_from_wire(frame, current)

    frame["binding"] = asdict(current)
    assert worker._turn_from_wire(frame, current) == ("request-1", "hello")


def test_room_symlink_is_not_accepted(monkeypatch, tmp_path):
    binding = _binding("tenant-A")
    resources = WorkerResources(binding)
    rooms = tmp_path / "identity" / "rooms"
    rooms.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (rooms / binding.room).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(worker.os, "environ", dict(resources.environment()))
    with pytest.raises(BoundaryDenied, match="private directories"):
        worker.prepare_worker_resources(resources, filesystem_root=tmp_path)


def test_worker_serving_fails_before_bootstrap_without_live_release_gate(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("worker touched a bootstrap channel before release approval")
    monkeypatch.setattr(worker, "_check_peer", forbidden)
    with pytest.raises(BoundaryDenied, match="release gate has not passed"):
        worker.serve_worker(None, supervisor_uid=0, inference_client=None)
