"""Exercise the HF supervisor with synthetic Python children, never Hermes."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "deploy/huggingface"
sys.path.insert(0, str(HELPERS))
from free_policy import fresh_config, validate_config
from check_runtime import check_sqlite
SPEC = importlib.util.spec_from_file_location(
    "hf_supervisor", HELPERS / "supervisor.py"
)
hf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hf)


def test_preflight_requires_optins_and_rejects_paths_before_creation(tmp_path):
    home = tmp_path / "state"
    assert hf.preflight(home, "unconfigured", True, tmp_path)
    assert hf.preflight(home, "ephemeral", False, tmp_path)
    assert hf.preflight(home, "ephemeral", True, tmp_path) is None
    assert hf.preflight(tmp_path / "hermes-agent", "ephemeral", True, tmp_path)
    assert hf.preflight(Path("/data/test-hf"), "ephemeral", True)
    assert not home.exists()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert hf.preflight(link / "missing" / "home", "ephemeral", True, tmp_path)
    assert not (target / "missing").exists()


def test_preflight_fails_closed_for_unknown_or_network_filesystems(
    tmp_path, monkeypatch
):
    for fs in (None, "nfs4", "fuse.hf-mount", "cifs", "unknown"):
        monkeypatch.setattr(hf, "mount_type", lambda _path, value=fs: value)
        assert hf.preflight(tmp_path / "state", "ephemeral", True, tmp_path)
    assert not (tmp_path / "state").exists()


def test_config_copy_is_private_and_preserves_existing_configuration(tmp_path):
    home = tmp_path / "home"
    cfg = tmp_path / "input.yaml"
    cfg.write_text("model: example\n")
    hf.setup_state(home, str(cfg), None)
    dest = home / "config.yaml"
    assert dest.read_text() == cfg.read_text()
    assert dest.stat().st_mode & 0o777 == 0o600
    cfg.write_text("model: changed\n")
    hf.setup_state(home, str(cfg), None)
    assert dest.read_text() == "model: example\n"


def test_restore_uses_real_snapshot_and_refuses_nonempty_home(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HELPERS))
    import snapshot

    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text("model: restored\n")
    archive = tmp_path / "snapshot.tar.gz"
    snapshot.create(source, archive, False, True)
    home = tmp_path / "destination"
    hf.setup_state(home, None, str(archive))
    assert (home / "config.yaml").read_text() == "model: restored\n"
    with pytest.raises(ValueError, match="not empty"):
        hf.setup_state(home, None, str(archive))
    assert (home / "config.yaml").read_text() == "model: restored\n"


def get(port, path):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=2
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise AssertionError("timed out waiting for synthetic supervisor")


@contextmanager
def service(tmp_path, child_code, mode="ephemeral", gate=False, config=True, unsafe_sqlite=False, bucket=False, initial_env=None, wrong_uvloop=False):
    home = tmp_path / "home"
    home.mkdir()
    if initial_env is not None:
        (home / ".env").write_text(initial_env)
    if config:
        import yaml
        (home / "config.yaml").write_text(config if isinstance(config, str) else yaml.safe_dump(fresh_config()))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    wrapper = f"""
import sys,time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(HELPERS)!r})
import supervisor as hf
from full_state import FullStateStore
data_root = Path({str(tmp_path / "bucket")!r})
hf.FullStateStore = lambda home: FullStateStore(home, data_root,
    mount_check=lambda _path: {bucket!r}, space_check=lambda: {bucket!r},
    bucket_name="test-owner/test-bucket")
if {bucket!r}:
    from persistence import SnapshotStore
    data_root.mkdir(exist_ok=True)
    hf.SnapshotStore = lambda home, **_kwargs: SnapshotStore(
        home, data_root, mount_check=lambda _path: True,
        space_check=lambda: True, bucket_name="test-owner/test-bucket")
    hf.SNAPSHOT_INTERVAL_SECONDS = 0.1
if {unsafe_sqlite!r}:
    import check_runtime
    check_runtime.sqlite3.sqlite_version_info = (3, 50, 4)
    check_runtime.sqlite3.sqlite_version = "3.50.4"
if {wrong_uvloop!r}:
    import uvloop
    uvloop.__version__ = "0.0.0"
if {gate!r}:
    original = hf.setup_state
    def gated(*args, **kwargs):
        Path({str(tmp_path / "waiting")!r}).touch()
        deadline = time.monotonic() + 20
        while not Path({str(tmp_path / "release")!r}).exists():
            if time.monotonic() > deadline: raise TimeoutError('test gate')
            time.sleep(.05)
        return original(*args, **kwargs)
    hf.setup_state = gated
raise SystemExit(hf.main(command=lambda: [sys.executable, '-c', {child_code!r}],
    state_home=Path({str(home)!r}), base=Path({str(tmp_path)!r}),
    app_dir=Path({str(tmp_path)!r}), host='127.0.0.1', port={port}))
"""
    env = os.environ.copy()
    env.update(HF_STATE_MODE=mode, HF_ALLOW_EPHEMERAL="1")
    for key in ("HF_CONFIG_FILE", "HF_RESTORE_ARCHIVE"):
        env.pop(key, None)
    proc = subprocess.Popen(
        [sys.executable, "-c", wrapper],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield proc, port
    finally:
        (tmp_path / "release").touch()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        pidfile = tmp_path / "leader"
        if pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.stdout.close()
        proc.stderr.close()


def test_unconfigured_http_is_live_but_not_ready_and_stops_cleanly(tmp_path):
    with service(
        tmp_path, "raise RuntimeError('must not run')", mode="unconfigured"
    ) as (proc, port):
        wait_for(lambda: get(port, "/healthz")[0] == 200)
        assert get(port, "/readyz")[0] == 503
        assert get(port, "/")[1]["gateway"] == "unconfigured"
        proc.terminate()
        assert proc.wait(timeout=15) == 0


def test_http_binds_before_state_setup_and_then_reports_process_ready(tmp_path):
    with service(tmp_path, "import time; time.sleep(60)", gate=True) as (proc, port):
        wait_for(lambda: (tmp_path / "waiting").exists())
        assert get(port, "/healthz")[0] == 200
        assert get(port, "/readyz")[0] == 503
        (tmp_path / "release").touch()
        wait_for(lambda: get(port, "/readyz")[0] == 200)
        assert "Process liveness only" in get(port, "/readyz")[1]["note"]
        proc.terminate()
        assert proc.wait(timeout=15) == 0


@pytest.mark.parametrize("child_code", ["raise SystemExit(0)", "raise SystemExit(7)"])
def test_unexpected_child_exit_fails_container_even_for_exit_zero(tmp_path, child_code):
    with service(tmp_path, child_code) as (proc, _port):
        assert proc.wait(timeout=15) == 1


def test_missing_config_bootstraps_privately_without_serializing_secrets(tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-provider-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-bot-secret")
    marker = tmp_path / "started"
    with service(
        tmp_path,
        f"import time; from pathlib import Path; Path({str(marker)!r}).touch(); time.sleep(60)",
        config=False,
    ) as (proc, port):
        wait_for(lambda: marker.exists() and get(port, "/readyz")[0] == 200)
        home = tmp_path / "home"
        config = home / "config.yaml"
        text = config.read_text()
        assert yaml.safe_load(text) == fresh_config()
        validate_config(yaml.safe_load(text))
        assert yaml.safe_load(text)["auxiliary"]["free_only"] is True
        assert "synthetic-provider-secret" not in text
        assert "synthetic-bot-secret" not in text
        assert config.stat().st_mode & 0o777 == 0o600
        assert home.stat().st_mode & 0o777 == 0o700
        hf.setup_state(home, None, None)
        assert config.read_text() == text
        proc.terminate()
        assert proc.wait(timeout=15) == 0


@pytest.mark.parametrize("invalid", ["empty", "symlink", "seed"])
def test_invalid_explicit_configuration_is_not_replaced(tmp_path, invalid):
    home = tmp_path / "home"
    home.mkdir()
    dest = home / "config.yaml"
    seed = None
    if invalid == "empty":
        dest.touch()
    elif invalid == "symlink":
        dest.symlink_to(tmp_path / "missing")
    else:
        seed = str(tmp_path / "missing.yaml")
    with pytest.raises(ValueError):
        hf.setup_state(home, seed, None)
    if invalid == "symlink":
        assert dest.is_symlink()
    elif invalid == "empty":
        assert dest.read_text() == ""
    else:
        assert not dest.exists()


def test_shutdown_cleans_stubborn_descendant_process_group(tmp_path):
    leader = tmp_path / "leader"
    grandchild = tmp_path / "grandchild"
    code = f"""
import os,subprocess,sys,time
from pathlib import Path
Path({str(leader)!r}).write_text(str(os.getpid()))
p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
Path({str(grandchild)!r}).write_text(str(p.pid))
time.sleep(60)
"""
    with service(tmp_path, code) as (proc, port):
        wait_for(lambda: grandchild.exists() and get(port, "/readyz")[0] == 200)
        pid = int(grandchild.read_text())
        proc.terminate()
        assert proc.wait(timeout=15) == 0
        statfile = Path(f"/proc/{pid}/stat")
        wait_for(
            lambda: not statfile.exists() or statfile.read_text().split()[2] == "Z"
        )


def test_fresh_config_pins_every_available_auxiliary_task():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    cfg = fresh_config()
    assert cfg["model"]["default"] == "openrouter/free"
    assert cfg["fallback_providers"] == []
    assert cfg["delegation"]["fallback_providers"] == []
    for name, defaults in DEFAULT_CONFIG["auxiliary"].items():
        if isinstance(defaults, dict) and "provider" in defaults and "model" in defaults:
            assert cfg["auxiliary"][name]["provider"] == "openrouter"
            assert cfg["auxiliary"][name]["model"] == "openrouter/free"
    validate_config(cfg)


@pytest.mark.parametrize("patch", [
    {"model": {"default": "paid/model", "provider": "openrouter"}},
    {"fallback_providers": [{"provider": "openai", "model": "paid/model"}]},
    {"auxiliary": {"free_only": False}},
    {"auxiliary": {"vision": {"provider": "openai", "model": "paid/model"}}},
    {"auxiliary": {"compression": {"base_url": "https://example.invalid/api/v1"}}},
    {"delegation": {"fallback_providers": ["paid/model"]}},
    {"provider_routing": {"allow_fallbacks": True}},
    {"provider_routing": {"max_price": {"prompt": 1}}},
    {"auxiliary": {"vision": {"extra_body": {"models": ["paid/model"]}}}},
    {"auxiliary": {"vision": {"extra_body": {"plugins": [{"id": "web"}]}}}},
])
def test_preserved_unsafe_config_is_blocked_without_rewriting(tmp_path, patch):
    import yaml
    home = tmp_path / "state"
    home.mkdir()
    config = fresh_config()
    config.update(patch)
    text = yaml.safe_dump(config)
    (home / "config.yaml").write_text(text)
    hf.setup_state(home, None, None)
    with pytest.raises(ValueError, match="HF free-only"):
        validate_config(yaml.safe_load((home / "config.yaml").read_text()))
    assert (home / "config.yaml").read_text() == text


def test_restored_safe_config_preserves_user_settings(tmp_path, monkeypatch):
    import yaml
    import snapshot
    source = tmp_path / "source"
    source.mkdir()
    cfg = fresh_config()
    cfg["terminal"]["timeout"] = 73
    text = yaml.safe_dump(cfg)
    (source / "config.yaml").write_text(text)
    archive = tmp_path / "state.tar.gz"
    snapshot.create(source, archive, False, True)
    home = tmp_path / "restored"
    hf.setup_state(home, None, str(archive))
    validate_config(yaml.safe_load((home / "config.yaml").read_text()))
    assert (home / "config.yaml").read_text() == text


def test_sqlite_guard_rejects_previous_image_version(monkeypatch):
    import check_runtime
    monkeypatch.setattr(check_runtime.sqlite3, "sqlite_version_info", (3, 50, 4))
    monkeypatch.setattr(check_runtime.sqlite3, "sqlite_version", "3.50.4")
    with pytest.raises(RuntimeError, match=r"SQLite 3.50.4.*SQLite >=3.51.3"):
        check_sqlite()


@pytest.mark.parametrize("python", [Path(sys.executable), ROOT / ".venv/bin/python"])
def test_actual_application_venv_runtime_passes_or_fails_closed(python):
    # The image build runs the same script using its actual .venv/bin/python.
    if not python.exists():
        pytest.skip("application .venv integration: image build always checks its actual .venv")
    env = {**os.environ, "PYTHONPATH": f"{HELPERS}:{ROOT}"}
    result = subprocess.run([str(python), str(HELPERS / "check_runtime.py")],
                            env=env, capture_output=True, text=True, check=False)
    version = subprocess.check_output([str(python), "-c",
        "import sqlite3; print('.'.join(map(str, sqlite3.sqlite_version_info)))"], text=True).strip()
    probe = subprocess.run([str(python), "-c",
        "from check_runtime import check_gateway_loop; check_gateway_loop()"],
        cwd=ROOT, env=env,
        capture_output=True, text=True, check=False)
    if tuple(map(int, version.split("."))) >= (3, 51, 3) and probe.returncode == 0:
        assert result.returncode == 0, result.stderr
        assert "HF SQLite verified" in result.stdout
        assert "HF event loop verified: uvloop==0.22.1; gateway loop uvloop.Loop" in result.stdout
    else:
        assert result.returncode == 1
        assert "HF startup blocked" in result.stderr


def test_image_uses_system_python_and_checks_real_venv():
    dockerfile = (ROOT / "Dockerfile.hf").read_text()
    assert "UV_PYTHON_DOWNLOADS=never" in dockerfile
    assert "uv sync --python /usr/bin/python3" in dockerfile
    assert "RUN .venv/bin/python /home/Hermes/hf/check_runtime.py" in dockerfile


def test_gateway_child_cannot_disable_free_only_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STRICT_FREE_ONLY", "0")
    marker = tmp_path / "policy"
    code = f"import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(os.environ.get('HERMES_STRICT_FREE_ONLY', 'missing')); time.sleep(60)"
    with service(tmp_path, code) as (proc, port):
        wait_for(lambda: marker.exists() and get(port, "/readyz")[0] == 200)
        assert marker.read_text() == "1"
        proc.terminate()
        assert proc.wait(timeout=15) == 0


def test_unsafe_preserved_config_prevents_gateway_launch(tmp_path):
    marker = tmp_path / "must-not-start"
    code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    with service(tmp_path, code, config="model: paid/model\n") as (proc, _port):
        assert proc.wait(timeout=15) == 1
        assert not marker.exists()
        assert (tmp_path / "home/config.yaml").read_text() == "model: paid/model\n"
        assert "HF free-only policy" in proc.stderr.read()


def test_unsafe_actual_interpreter_blocks_before_state_setup(tmp_path):
    marker = tmp_path / "must-not-start"
    code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    with service(tmp_path, code, config=False, unsafe_sqlite=True) as (proc, _port):
        assert proc.wait(timeout=15) == 1
        assert not marker.exists()
        assert not (tmp_path / "home/config.yaml").exists()
        assert "loads SQLite 3.50.4" in proc.stderr.read()


def test_wrong_uvloop_blocks_before_state_setup_without_leaking_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-secret-must-not-appear")
    marker = tmp_path / "must-not-start"
    code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    with service(tmp_path, code, config=False, wrong_uvloop=True) as (proc, _port):
        assert proc.wait(timeout=15) == 1
        assert not marker.exists()
        assert not (tmp_path / "home/config.yaml").exists()
        output = proc.stdout.read() + proc.stderr.read()
        assert "uvloop==0.22.1 required" in output
        assert "synthetic-secret-must-not-appear" not in output


def test_bucket_restore_periodic_snapshot_and_graceful_shutdown(tmp_path, monkeypatch):
    """Only synthetic gateway processes run; no Space or real service is touched."""
    from persistence import SnapshotStore
    from hermes_state import SessionDB

    seed_home = tmp_path / "seed-home"
    seed_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(seed_home))
    seed = SessionDB(seed_home / "state.db")
    try:
        seed.create_session("restored-chat", "telegram", model="openrouter/free")
        seed.append_message("restored-chat", "user", "Continue our chat")
    finally:
        seed.close()
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    store = SnapshotStore(
        seed_home, bucket, mount_check=lambda _path: True,
        space_check=lambda: True, bucket_name="test-owner/test-bucket")
    assert store.prepare()
    assert store.snapshot(), store.last_error

    child_code = """
import os, sqlite3, time
from pathlib import Path
with sqlite3.connect(Path(os.environ['HERMES_HOME']) / 'state.db') as db:
    assert db.execute('SELECT content FROM messages').fetchone()[0] == 'Continue our chat'
time.sleep(60)
"""
    artifacts = bucket / "hermes-state"
    with service(tmp_path, child_code, bucket=True) as (proc, port):
        wait_for(lambda: get(port, "/readyz")[0] == 200)
        _code, body = get(port, "/readyz")
        assert "Restored the latest validated" in body["detail"]
        wait_for(lambda: len(list(artifacts.glob("chat-state-*.json"))) >= 2)
        proc.terminate()
        assert proc.wait(timeout=15) == 0
        assert len(list(artifacts.glob("chat-state-*.json"))) == 3
        output = proc.stdout.read()
        assert "final chat/session snapshot published" in output


@pytest.mark.parametrize(
    "owner,allowed,existing,error",
    [
        (None, "100,200", "", None),
        (None, "100,200", "OWNER_TELEGRAM_ID=300\n", None),
        ("100", None, "", None),
        ("100", "100,200", "# keep\nUNRELATED=untouched\n", None),
        ("100", "200,100,200", "UNRELATED=untouched", None),
        ("100", None, "TELEGRAM_ALLOWED_USERS=300\n", None),
        ("100", "200,100", "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS='100, 200'\n", None),
        ("100", "", "", None),
        ("", "100", "", "OWNER_TELEGRAM_ID"),
        ("@owner", "100", "", "OWNER_TELEGRAM_ID"),
        ("100\nOTHER=1", "100", "", "OWNER_TELEGRAM_ID"),
        ("0", "100", "", "OWNER_TELEGRAM_ID"),
        ("00100", "100", "", "OWNER_TELEGRAM_ID"),
        ("100", "00100", "", "TELEGRAM_ALLOWED_USERS"),
        ("100", "*", "", "TELEGRAM_ALLOWED_USERS"),
        ("100", "100,@tester", "", "TELEGRAM_ALLOWED_USERS"),
        ("100", "100\nOTHER=1", "", "TELEGRAM_ALLOWED_USERS"),
        ("100", "100,200", "OWNER_TELEGRAM_ID=300\n", "conflict"),
        ("100", "100,200", "TELEGRAM_ALLOWED_USERS=100\n", "conflict"),
        ("100", "100", "OWNER_TELEGRAM_ID=\n", "conflict"),
    ],
)
def test_hf_multiuser_runtime_is_narrow_nonpersistent_and_fail_closed(tmp_path, monkeypatch, owner, allowed, existing, error):
    from gateway.mongo_multiuser import _HF_RUNTIME_POLICY, _profile_env, hf_runtime_environment

    home = tmp_path / "home"
    home.mkdir()
    path = home / ".env"
    if existing:
        path.write_text(existing)
    before = path.read_bytes() if path.exists() else None
    mode = path.stat().st_mode if path.exists() else None
    for key, value in (("OWNER_TELEGRAM_ID", owner), ("TELEGRAM_ALLOWED_USERS", allowed)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv(_HF_RUNTIME_POLICY, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-bridge-provider")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "do-not-bridge-bot")
    local = _profile_env(home)
    # Raw HF variables alone must not change ordinary local behavior.
    assert local.get("OWNER_TELEGRAM_ID") != owner or owner is None or "OWNER_TELEGRAM_ID" in existing
    if error:
        with pytest.raises(ValueError, match=error) as exc:
            hf_runtime_environment(home, os.environ)
        assert not any(value in str(exc.value) for value in ("300", "@tester", "OTHER=1"))
    else:
        runtime = hf_runtime_environment(home, os.environ)
        assert _HF_RUNTIME_POLICY not in os.environ
        assert _profile_env(home) == local
        if owner is None:
            assert _HF_RUNTIME_POLICY not in runtime
        else:
            assert "do-not-bridge" not in runtime[_HF_RUNTIME_POLICY]
            values = _profile_env(home, _runtime_environment=runtime)
            assert values["OWNER_TELEGRAM_ID"] == owner
            if allowed is not None:
                assert set(values["TELEGRAM_ALLOWED_USERS"].replace(",", " ").split()) == set(allowed.replace(",", " ").split())
            sibling = tmp_path / "other-profile"
            sibling.mkdir()
            (sibling / ".env").write_text("OWNER_TELEGRAM_ID=900\nTELEGRAM_ALLOWED_USERS=901\n")
            assert _profile_env(home, _runtime_environment=runtime) == values
            assert _profile_env(sibling, _runtime_environment=runtime)["OWNER_TELEGRAM_ID"] == "900"
            assert _profile_env(home, _runtime_environment=runtime) == values
            missing = tmp_path / "unconfigured"
            assert _profile_env(missing, _runtime_environment=runtime) == {}
    assert (path.read_bytes() if path.exists() else None) == before
    assert (path.stat().st_mode if path.exists() else None) == mode
    assert set(p.name for p in home.iterdir()) == ({".env"} if existing else set())


@pytest.mark.parametrize("conflict", [False, True])
def test_hf_space_owner_reaches_real_multiuser_adapter_without_policy_file(tmp_path, monkeypatch, conflict):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "100")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "100,200")
    marker = tmp_path / "multiuser-ready"
    code = f"""
import time
from pathlib import Path
from types import SimpleNamespace
import sys
sys.path.insert(0, {str(ROOT)!r})
from hermes_constants import get_hermes_home
from gateway.mongo_multiuser import enabled, owner_user_id, store_for_home, principal
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
home = get_hermes_home()
assert not (home / '.env').exists()
assert enabled(home) and owner_user_id(home) == '100'
adapter = TelethonAdapter.__new__(TelethonAdapter)
adapter._is_sender_authorized = lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy gate'))
source = SimpleNamespace(user_id='200', chat_type='dm', chat_id='200', is_bot=False, thread_id=None)
assert adapter._multiuser_home(source) == home and adapter._authorized(source)
store = store_for_home(home)
store.ban_user(principal('100'), '200', True)
assert not adapter._authorized(source)
assert not store_for_home(home).get_access('999').get('allowed')
other = home / 'profiles' / 'other'
other.mkdir(parents=True)
(other / '.env').write_text('OWNER_TELEGRAM_ID=900\\nTELEGRAM_ALLOWED_USERS=901\\n')
adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda event: other if event.user_id == '901' else home)
other_source = SimpleNamespace(user_id='901', chat_type='dm', chat_id='901', is_bot=False, thread_id=None)
assert adapter._multiuser_home(source) == home
assert adapter._multiuser_home(other_source) == other
assert owner_user_id(other) == '900' and adapter._authorized(other_source)
assert not store_for_home(other).get_access('100').get('allowed')
assert adapter._multiuser_home(source) == home and not adapter._authorized(source)
unconfigured = home / 'profiles' / 'unconfigured'
unconfigured.mkdir()
assert not enabled(unconfigured) and not (unconfigured / '.env').exists()
assert not (home / '.env').exists()
Path({str(marker)!r}).touch()
time.sleep(60)
"""
    with service(tmp_path, code, initial_env="OWNER_TELEGRAM_ID=300\n" if conflict else None) as (proc, port):
        if conflict:
            assert proc.wait(timeout=15) == 1
            assert not marker.exists()
            assert (tmp_path / "home/.env").read_text() == "OWNER_TELEGRAM_ID=300\n"
        else:
            wait_for(lambda: marker.exists() and get(port, "/readyz")[0] == 200)
            proc.terminate()
            assert proc.wait(timeout=15) == 0
            assert not (tmp_path / "home/.env").exists()
        stdout, stderr = proc.communicate()
        assert "100" not in stdout + stderr and "200" not in stdout + stderr and "300" not in stdout + stderr
        assert "_HERMES_HF_MULTIUSER_RUNTIME_POLICY" not in stdout + stderr
