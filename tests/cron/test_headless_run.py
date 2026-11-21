"""The generic headless-run entry point: one ad-hoc run per runId through the
shared cron seam, with launch idempotency instead of a second run.

A ``no_agent`` script job exercises the whole path without a model: execution
ledger, detached-worker seam, fire-claim fence, output save. Delivery stays
``local`` (save only), so no adapter is needed.
"""

import pytest

from cron.headless_run import build_headless_job, run_headless
from gateway.platforms.launch_idempotency import LaunchIdempotencyConflict


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants

    importlib.reload(hermes_constants)
    import cron.jobs

    importlib.reload(cron.jobs)
    import cron.scheduler

    importlib.reload(cron.scheduler)

    return home


def _script(hermes_env):
    scripts = hermes_env / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    marker = hermes_env / "probe-marker.txt"
    (scripts / "probe.sh").write_text(
        f"#!/bin/sh\necho probe-ran >> {marker}\necho headless-probe-ok\n"
    )
    return marker


def test_run_headless_runs_once_and_replays(hermes_env):
    marker = _script(hermes_env)
    job_id = build_headless_job(
        run_id="probe-1", prompt="p", no_agent=True, script="probe.sh"
    )["id"]

    assert (
        run_headless(run_id="probe-1", prompt="probe", script="probe.sh", no_agent=True)
        is True
    )
    assert (
        run_headless(run_id="probe-1", prompt="probe", script="probe.sh", no_agent=True)
        is True
    )

    assert marker.read_text().splitlines() == ["probe-ran"]
    outputs = sorted((hermes_env / "cron" / "output" / job_id).glob("*.md"))
    assert len(outputs) == 1
    assert "headless-probe-ok" in outputs[0].read_text()


def test_run_headless_conflicts_on_same_id_different_spec(hermes_env):
    _script(hermes_env)
    assert (
        run_headless(run_id="probe-2", prompt="one", script="probe.sh", no_agent=True)
        is True
    )
    with pytest.raises(LaunchIdempotencyConflict):
        run_headless(
            run_id="probe-2", prompt="different", script="probe.sh", no_agent=True
        )


def test_run_headless_requires_a_run_id(hermes_env):
    with pytest.raises(ValueError, match="non-empty run_id"):
        run_headless(run_id="  ", prompt="p", no_agent=True, script="probe.sh")


def test_build_headless_job_is_transient_and_local_by_default():
    job = build_headless_job(run_id="a/b:c", prompt="p")
    assert job["id"] == "headless-a-b-c"
    assert job["headless_run_id"] == "a/b:c"
    assert job["deliver"] == "local"
    assert job["no_agent"] is False
    assert "model" not in job and "provider" not in job
