"""Regression tests for packaging metadata in pyproject.toml."""

from pathlib import Path
import tomllib

TELETHON_REQUIREMENT = "telethon @ git+https://codeberg.org/lonami/telethon@d8a33dad2f269cfd3f34a33e248e91ca59b4cdfb"

def _load_optional_dependencies():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["optional-dependencies"]


def test_wake_dependencies_and_runtime_gate_agree_on_supported_targets():
    from packaging.markers import Marker, default_environment
    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    optional = metadata["project"]["optional-dependencies"]
    gates = metadata["tool"]["hermes"]["extras-platforms"]
    targets = [
        ("darwin", "Darwin", "x86_64", False),
        ("darwin", "Darwin", "arm64", True),
        ("linux", "Linux", "x86_64", True),
        ("linux", "Linux", "aarch64", True),
        ("win32", "Windows", "AMD64", True),
        ("win32", "Windows", "ARM64", False),
    ]
    for system, platform_system, machine, supported in targets:
        environment = {**default_environment(), "sys_platform": system,
                       "platform_system": platform_system, "platform_machine": machine}
        for extra in ("wake", "wake-openwakeword"):
            requirement = next(req for spec in optional[extra]
                               if (req := Requirement(spec)).name == "pyopen-wakeword")
            assert requirement.marker.evaluate(environment) is supported, (extra, system, machine)
        assert Marker(gates["wake-openwakeword"]).evaluate(environment) is supported
        selected = {req.name for spec in optional["wake"]
                    if (req := Requirement(spec)).marker is None or req.marker.evaluate(environment)}
        assert {"sherpa-onnx", "pvporcupine", "sentencepiece", "pypinyin"} <= selected
        sherpa_deps = {req.name for spec in optional["wake-sherpa"]
                       if (req := Requirement(spec)).marker is None or req.marker.evaluate(environment)}
        assert {"sherpa-onnx", "sentencepiece", "pypinyin"} <= sherpa_deps
        for extra in ("wake-sherpa", "wake-porcupine"):
            assert all(req.marker is None or req.marker.evaluate(environment)
                       for req in map(Requirement, optional[extra]))
            gate = gates.get(extra)
            assert gate is None or Marker(gate).evaluate(environment)


def test_direct_overrides_preserve_the_declared_exact_version():
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8-sig"))
    # uv.lock records normalized names; pyproject keeps the declared spelling (Pillow).
    direct = {canonicalize_name(req.name): req for spec in metadata["project"]["dependencies"]
              if (req := Requirement(spec)).specifier}
    for spec in metadata["tool"]["uv"].get("override-dependencies", []):
        override = Requirement(spec)
        name = canonicalize_name(override.name)
        requirement = direct.get(name)
        if requirement is None or not any(s.operator == "==" for s in requirement.specifier):
            continue
        assert override.specifier == requirement.specifier, (
            f"{override.name}: override {override.specifier} contradicts exact requirement {requirement.specifier}"
        )
        versions = {row["version"] for row in locked["package"] if canonicalize_name(row["name"]) == name}
        assert versions and all(version in requirement.specifier for version in versions)


def test_matrix_extra_not_in_all():
    """The [matrix] extra pulls `mautrix[encryption]` -> `python-olm`,
    which has Linux-only wheels and no native build path on Windows or
    modern macOS (archived libolm, C++ errors with Clang 21+).

    With matrix in [all], `uv sync --locked` on Windows tried to build
    python-olm from sdist and failed on `make`. As of 2026-05-12 the
    [matrix] extra is excluded from [all] entirely and installs on first
    use (pm.ensure_import("matrix")), where the user is expected to have
    a toolchain.
    """
    optional_dependencies = _load_optional_dependencies()

    assert "matrix" in optional_dependencies, "[matrix] extra must still exist for `uv sync --extra matrix`"
    # Must NOT appear in [all] in any form — neither unconditional nor
    # platform-gated. Lazy-install handles it.
    matrix_in_all = [
        dep for dep in optional_dependencies["all"]
        if "matrix" in dep
    ]
    assert not matrix_in_all, (
        "matrix must not appear in [all] — it installs on first use via "
        f"pm.ensure_import('matrix'). Found: {matrix_in_all}"
    )


def test_lazy_installable_extras_excluded_from_all():
    """Policy (2026-05-12): opt-in backends stay out of [all].

    On-demand install exists so one quarantined PyPI release
    (e.g. mistralai 2.4.6) can't break every fresh install. Putting a
    backend in [all] defeats that — fresh installs eager-install it and
    inherit whatever's broken upstream. Opt-in backends are extras that
    install at first use via pm.ensure_import(extra).
    """
    optional_dependencies = _load_optional_dependencies()

    # The on-demand backends as of 2026-05-12. Deliberately a literal
    # list so the test stays a contract — adding a new opt-in backend
    # means updating this list AND verifying [all] doesn't contain it.
    lazy_covered_extras = {
        "anthropic", "bedrock",
        "exa", "firecrawl", "parallel-web",
        "fal",
        "edge-tts", "tts-premium",
        "voice",  # faster-whisper / sounddevice / numpy (composes stt-whisper + audio-io)
        "stt-whisper",
        "modal", "daytona", "vercel",
        "messaging", "slack", "matrix", "dingtalk", "feishu",
        "telegram", "discord",
        "wake", "wake-openwakeword", "wake-sherpa", "wake-porcupine",
        "google-chat",
        "keychain",  # keyring — vault key in the OS keychain, installed by `hermes vault key migrate`
        "mistral",  # mistralai — Voxtral STT/TTS, lazy-installed (stt.mistral / tts.mistral)
    }
    all_extra_specs = optional_dependencies["all"]
    for extra in lazy_covered_extras:
        offending = [
            spec for spec in all_extra_specs
            if f"hermes-agent[{extra}]" in spec
        ]
        assert not offending, (
            f"[{extra}] is in [all] but also in LAZY_DEPS. "
            f"Remove it from [all] in pyproject.toml — it lazy-installs "
            f"at first use. Found in [all]: {offending}"
        )


def _exact_pins(specs):
    pins = {}
    for spec in specs:
        requirement = spec.split(";", 1)[0].strip()
        if "==" not in requirement:
            continue
        package, version = requirement.split("==", 1)
        package = package.split("[", 1)[0].lower().replace("_", "-")
        pins[package] = version
    return pins


def test_extras_pin_each_package_at_one_version():
    """One package, one version, across every extra.

    tools/lazy_deps.py is gone — pyproject.toml is the single authority for
    optional-dependency pins (pm syncs the venv from uv.lock, which resolves
    from here). The drift class that killed us before (#31817: two documents
    pinning the same package differently, update ping-ponging the version)
    is now only possible BETWEEN extras — so pin consistency across extras
    is the whole remaining contract.
    """
    optional_dependencies = _load_optional_dependencies()

    pins: dict[str, dict[str, set[str]]] = {}
    for extra, specs in optional_dependencies.items():
        for package, version in _exact_pins(specs).items():
            pins.setdefault(package, {}).setdefault(version, set()).add(extra)

    drift = {
        package: {v: sorted(extras) for v, extras in versions.items()}
        for package, versions in pins.items()
        if len(versions) > 1
    }
    assert not drift, (
        "extras pin the same package at different versions — uv sync would "
        f"resolve whichever wins and silently downgrade the other: {drift}"
    )


def test_dingtalk_extra_includes_qrcode_for_qr_auth():
    """DingTalk's QR-code device-flow auth (hermes_cli/dingtalk_auth.py)
    needs the qrcode package."""
    optional_dependencies = _load_optional_dependencies()

    dingtalk_extra = optional_dependencies["dingtalk"]
    assert any(dep.startswith("qrcode") for dep in dingtalk_extra)


def test_telegram_uses_telethon_without_bot_api_dependency():
    optional_dependencies = _load_optional_dependencies()
    from tools.lazy_deps import LAZY_DEPS

    assert TELETHON_REQUIREMENT in optional_dependencies["messaging"]
    assert TELETHON_REQUIREMENT in optional_dependencies["termux"]
    assert "python-socks[asyncio]==2.8.2" in optional_dependencies["messaging"]
    assert "python-socks[asyncio]==2.8.2" in optional_dependencies["termux"]
    assert LAZY_DEPS["platform.telegram"] == (TELETHON_REQUIREMENT, "python-socks[asyncio]==2.8.2")
    assert not any("python-telegram-bot" in spec for specs in optional_dependencies.values() for spec in specs)


def test_telegram_plugin_requires_all_mtproto_credentials_as_secret_metadata():
    import yaml

    plugin_path = Path(__file__).resolve().parents[1] / "plugins/platforms/telegram/plugin.yaml"
    plugin = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
    required = {item["name"]: item for item in plugin["requires_env"]}
    assert set(required) == {"TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"}
    assert required["TELEGRAM_API_ID"]["password"] is False
    assert required["TELEGRAM_API_HASH"]["password"] is True
    assert required["TELEGRAM_BOT_TOKEN"]["password"] is True


def _uv_lock_version(package: str) -> str:
    """Resolved version of ``package`` in uv.lock, or fail loudly."""
    versions = _uv_lock_versions(package)
    assert versions, f"{package} not found in uv.lock"
    assert len(versions) == 1, f"{package} resolves to multiple versions in uv.lock: {versions}"
    return next(iter(versions))


def _uv_lock_versions(package: str) -> set[str]:
    """All resolved versions of ``package`` in uv.lock (normally 0 or 1)."""
    import re

    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    return {
        m.group(1)
        for m in re.finditer(
            rf'\[\[package\]\]\nname = "{re.escape(package)}"\nversion = "([^"]+)"',
            lock,
        )
    }


def test_every_lazy_deps_exact_pin_matches_uv_lock():
    """Class invariant for #60783/#60685: one version per package, everywhere.

    Any package that is BOTH exact-pinned in ``tools/lazy_deps.py`` AND
    resolved in the committed uv.lock is a *shared* package: the core
    install ships the locked version, and the ``hermes update`` lazy-refresh
    pass re-asserts the LAZY_DEPS pin whenever the package is present
    (``active_features()``). If the two disagree, every update churns the
    package — and when the lazy pin is older, it force-DOWNGRADES a version
    another consumer needs (huggingface-hub==1.2.3 vs transformers'
    >=1.5.0 broke Hindsight local embeddings; stale aiohttp pins reopened
    patched CVEs in #31817). Contract: for every such package, pin ==
    locked version. When bumping a pin, regenerate the lock in the same
    commit (`uv lock --upgrade-package <name>`), and vice versa.
    """
    from tools.lazy_deps import LAZY_DEPS

    drift = {}
    seen = set()
    for feature, specs in LAZY_DEPS.items():
        for package, pin in _exact_pins(specs).items():
            if (package, pin) in seen:
                continue
            seen.add((package, pin))
            locked = _uv_lock_versions(package)
            if not locked:
                # Lazy-only package never resolved by the core lock — no
                # shared-version hazard.
                continue
            if pin not in locked:
                drift.setdefault(package, {})[feature] = {
                    "lazy_pin": pin,
                    "uv_lock": sorted(locked),
                }

    assert not drift, (
        "LAZY_DEPS exact pins must match the uv.lock resolved version for "
        "every package the core lock also ships — otherwise `hermes update` "
        "churns/downgrades the shared package out from under its other "
        "consumers (#60783, #31817). Bump the pin AND run "
        "`uv lock --upgrade-package <name>` in the same commit. Drift: "
        f"{drift}"
    )


def test_huggingface_hub_lazy_pin_inside_transformers_window():
    """The hub pin must stay in transformers' accepted range (#60783).

    transformers (pulled by sentence-transformers for Hindsight
    local/local_embedded embeddings) requires huggingface-hub>=1.5.0,<2.
    An exact pin outside that window makes the lazy-refresh downgrade the
    shared package below what the embedding stack imports, and the
    Hindsight daemon fails on startup. Contract, not a snapshot: any
    future exact pin is fine as long as it stays inside the window.
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    from tools.lazy_deps import LAZY_DEPS

    pin = _exact_pins(LAZY_DEPS["tool.trace_upload"]).get("huggingface-hub")
    assert pin, "tool.trace_upload must exact-pin huggingface-hub"
    transformers_window = SpecifierSet(">=1.5.0,<2")
    assert Version(pin) in transformers_window, (
        f"huggingface-hub=={pin} falls outside transformers' accepted "
        "range (>=1.5.0,<2). The lazy refresh would downgrade the shared "
        "package and break Hindsight local embeddings (#60783)."
    )
