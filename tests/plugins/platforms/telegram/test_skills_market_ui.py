"""Regression tests for the skill market front end (/skills Market button).

The market lists repos from market.json; a tap installs the WHOLE repo
directly into the tapper's profile (no staging, no approval queue), and the
same menu removes an installed repo from that profile only — never from the
registry, never from other profiles. A repo is installed when ALL its skill
dirs exist in the profile.
"""

import asyncio
import json
import pathlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram import skills_market_ui as market


def _skill_md(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: A market skill for {name}.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Use when the test needs it.\n"
    )


def _write_market_skill(launch_home: Path, name: str) -> Path:
    skill_dir = launch_home / "skills-market" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    return skill_dir


def _repo_row(name: str, url: str, skills: list[tuple[str, str]], **extra: object) -> dict:
    """One repo index row; *skills* is ``(skill name, repo-relative path)``."""
    row = {
        "name": name,
        "author": url.rstrip("/").split("/")[-2],
        "repo": url,
        "source_url": url,
        "skill_count": len(skills),
        "size_bytes": 100 * len(skills),
        "skills": [
            {
                "name": skill_name,
                "path": path,
                "skill_md": f"{path}/SKILL.md",
                "description": f"From the index: {skill_name}.",
                "files": 1,
                "size_bytes": 100,
            }
            for skill_name, path in skills
        ],
    }
    row.update(extra)
    return row


def _write_market_json(launch_home: Path, repos: list[dict]) -> None:
    (launch_home / "skills-market").mkdir(parents=True, exist_ok=True)
    (launch_home / "skills-market" / "market.json").write_text(
        json.dumps(repos), encoding="utf-8")


class _FakeRunner:
    def __init__(self, home: Path):
        self._home = home

    def _resolve_profile_home_for_source(self, source):
        return self._home


class _FakeAdapter:
    def __init__(self, home: Path):
        self.gateway_runner = _FakeRunner(home)
        self._client = object()
        self.sent = []
        self.edits = []
        self.deletes = []

    def _peer_for(self, chat_id):
        return f"peer:{chat_id}"

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((chat_id, message_id))
        return True

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return SimpleNamespace(id="99")

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self.sent.append({"source": source, "text": text, "markup": markup, "reply_to": reply_to})
        return SimpleNamespace(id="42")


class _FakeEvent:
    def __init__(self, message_id="7"):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append({"text": text, "alert": alert})


@pytest.fixture
def market_home(tmp_path, monkeypatch):
    launch_home = tmp_path / "launch"
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    market._TOKENS.clear()
    # Tests opt out of the shipped index unless they test it: point the
    # bundled fallback at a file that does not exist.
    monkeypatch.setattr(market, "_BUNDLED_MARKET_JSON", tmp_path / "no-bundle.json",
                        raising=False)
    return launch_home


def _token_for(name: str, kind: str) -> str:
    for token, entry in market._TOKENS.items():
        if entry["name"] == name and entry["kind"] == kind:
            return token
    raise AssertionError(f"no {kind} token minted for {name}")


def _markup_buttons(markup):
    """(label, data, bg_success, bg_danger) per keyboard button, row by row."""
    buttons = []
    for row in (markup.rows if markup is not None else []):
        for button in row.buttons:
            style = getattr(button, "style", None)
            buttons.append((
                button.text,
                bytes(getattr(button.type, "data", b"") or b""),
                bool(style and getattr(style, "bg_success", None)),
                bool(style and getattr(style, "bg_danger", None)),
            ))
    return buttons


def _install_setup(market_home, repos):
    """Host market.json with *repos*, no skill dirs; wired adapter/source."""
    _write_market_json(market_home, repos)
    target_home = market_home / "profiles" / "gc-1"
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="123", chat_type="dm", user_id="777", thread_id=None)
    return target_home, adapter, source


def _install_owned_repo(market_home, repo):
    """A profile with every skill of *repo* installed, plus a wired adapter."""
    target_home = market_home / "profiles" / "gc-1"
    for skill_name, _path in [(s["name"], s["path"]) for s in repo["skills"]]:
        owned = target_home / "skills" / skill_name
        owned.mkdir(parents=True)
        (owned / "SKILL.md").write_text(_skill_md(skill_name), encoding="utf-8")
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="123", chat_type="dm", user_id="777", thread_id=None)
    return target_home, adapter, source


def _open_manage_prompt(adapter, source, name):
    """Tap the green manage button; returns the prompt's buttons."""
    event = _FakeEvent(message_id="7")
    market._render(adapter.gateway_runner._home)
    token = _token_for(name, "manage")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:m:{token}")
    )
    assert len(adapter.sent) == 1, "the manage tap opens a disable/remove prompt"
    assert name in adapter.sent[0]["text"]
    return _markup_buttons(adapter.sent[0]["markup"])


def _demo_repo(name="demo-repo"):
    return _repo_row(name, f"https://github.com/example/{name}", [
        ("demo-a", "skills/demo-a"),
        ("demo-b", "skills/demo-b"),
    ])


def test_market_lists_repo_entries_with_skill_counts(market_home):
    """The market view shows repos with name, URL, skill count and size."""
    _write_market_json(market_home, [
        _demo_repo("demo-repo"),
        _repo_row("other-repo", "https://github.com/example/other", [
            ("other-a", "other-a"),
        ]),
    ])
    target_home = market_home / "profiles" / "gc-1"

    text, markup = market._render(target_home)

    assert "demo-repo" in text
    assert "https://github.com/example/demo-repo" in text
    assert "2 skills" in text
    assert "other-repo" in text
    assert "1 skills" in text
    buttons = _markup_buttons(markup)
    installs = [b for b in buttons if b[1].startswith(b"skm:i:")]
    assert len(installs) == 2
    assert all(not b[2] and not b[3] for b in installs), "install buttons stay plain"


def test_market_install_uses_local_copy_when_repo_on_host(market_home, monkeypatch):
    """Every skill dir on the host: install copies them, never fetches."""
    repo = _demo_repo()
    for skill_name, _path in [("demo-a", "x"), ("demo-b", "x")]:
        _write_market_skill(market_home, skill_name)
    target_home, adapter, source = _install_setup(market_home, [repo])
    event = _FakeEvent(message_id="7")

    def failing_fetch(source_url, dest):
        raise AssertionError("must not fetch when the repo is on the host")

    monkeypatch.setattr(market, "_fetch_repo_from_repo", failing_fetch, raising=False)

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert (target_home / "skills" / "demo-a" / "SKILL.md").is_file()
    assert (target_home / "skills" / "demo-b" / "SKILL.md").is_file()
    assert adapter.edits, "the market message re-renders after install"
    assert "Installed demo-repo" in adapter.edits[-1].message


def test_market_install_copies_whole_repo_and_remove_takes_it_out(market_home, monkeypatch):
    """Tap install: every skill lands in the profile at once. Tap remove:
    all of them go, from the profile only."""
    repo = _demo_repo()
    for skill_name, _path in [("demo-a", "x"), ("demo-b", "x")]:
        _write_market_skill(market_home, skill_name)
    target_home, adapter, source = _install_setup(market_home, [repo])
    event = _FakeEvent(message_id="7")

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert (target_home / "skills" / "demo-a" / "SKILL.md").is_file()
    assert (target_home / "skills" / "demo-b" / "SKILL.md").is_file()
    assert event.answers, "the tap must be answered"
    assert all(not a["alert"] for a in event.answers)

    # Installed now: the render mints a manage token; the prompt mints remove.
    market._render(target_home)
    token = _token_for("demo-repo", "manage")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:m:{token}")
    )
    assert adapter.sent, "the manage tap opens a disable/remove prompt"
    token = _token_for("demo-repo", "remove")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:r:{token}")
    )

    assert not (target_home / "skills" / "demo-a").exists()
    assert not (target_home / "skills" / "demo-b").exists()
    assert (market_home / "skills-market" / "demo-a" / "SKILL.md").is_file()


def test_market_group_install_falls_back_to_personal_profile(market_home):
    """In a group with no group profile, the install lands in the tapper's
    own personal profile — never in the launch profile."""
    repo = _demo_repo()
    for skill_name, _path in [("demo-a", "x"), ("demo-b", "x")]:
        _write_market_skill(market_home, skill_name)
    _write_market_json(market_home, [repo])
    target_home = market_home / "profiles" / "tg-777"
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="-100123", chat_type="group", user_id="777", thread_id=None)
    event = _FakeEvent(message_id="7")

    assert market.install_target_home(adapter, source) == target_home

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert (target_home / "skills" / "demo-a" / "SKILL.md").is_file()
    assert (target_home / "skills" / "demo-b" / "SKILL.md").is_file()


def test_market_stale_token_reports_expired(market_home):
    """A tap on a superseded listing says so plainly instead of installing."""
    _write_market_json(market_home, [_demo_repo()])
    target_home = market_home / "profiles" / "gc-1"
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="123", chat_type="dm", user_id="777", thread_id=None)
    event = _FakeEvent(message_id="7")

    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, "skm:i:deadbeef")
    )

    assert not (target_home / "skills" / "demo-a").exists()
    assert event.answers
    assert "expired" in (event.answers[0]["text"] or "")
    assert all(not a["alert"] for a in event.answers)


@pytest.mark.asyncio
async def test_market_open_sends_new_message(market_home):
    """Tapping Market sends the listing as a new message, not an edit."""
    _write_market_json(market_home, [_demo_repo()])
    target_home = market_home / "profiles" / "gc-1"
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="123", chat_type="dm", user_id="777", thread_id=None)
    event = _FakeEvent(message_id="5")

    await market.handle_market_callback(adapter, event, source, "skm:market")

    assert len(adapter.sent) == 1
    assert "demo-repo" in adapter.sent[0]["text"]
    assert adapter.sent[0]["reply_to"] == 5
    assert adapter.sent[0]["markup"] is not None


def test_market_render_ten_per_page_with_cancel_and_back(market_home):
    """Ten repos per page; Cancel (red) + Back (plain) sit below the list."""
    repos = [
        _repo_row(f"repo-{i:02d}", f"https://github.com/example/repo-{i:02d}", [
            (f"skill-{i:02d}", f"skill-{i:02d}"),
        ])
        for i in range(12)
    ]
    _write_market_json(market_home, repos)
    target_home = market_home / "profiles" / "gc-1"

    text, markup = market._render(target_home)
    assert "Page 1 of 2" in text
    for i in range(10):
        assert f"repo-{i:02d}" in text
    assert "repo-10" not in text
    assert "repo-11" not in text
    buttons = _markup_buttons(markup)
    assert len(buttons) == 12  # 10 install buttons + Cancel + Back
    assert buttons[-2][0] == "Cancel" and buttons[-2][3] is True
    assert buttons[-1][0] == "Back" and not buttons[-1][2] and not buttons[-1][3]
    assert buttons[-1][1] == b"skm:back:0"

    text, markup = market._render(target_home, page=1)
    assert "Page 2 of 2" in text
    assert "repo-10" in text and "repo-11" in text
    assert "repo-00" not in text


def test_market_installed_repo_has_badge_and_green_button(market_home):
    """Installed: green installed badge right after the name + green button.
    Not-installed: no badge, plain button that installs the whole repo."""
    _write_market_json(market_home, [
        _demo_repo("demo-repo"),
        _repo_row("other-repo", "https://github.com/example/other", [
            ("other-a", "other-a"),
        ]),
    ])
    target_home = market_home / "profiles" / "gc-1"
    for skill_name in ("demo-a", "demo-b"):
        owned = target_home / "skills" / skill_name
        owned.mkdir(parents=True)
        (owned / "SKILL.md").write_text(_skill_md(skill_name), encoding="utf-8")

    text, markup = market._render(target_home)
    name_line = next(line for line in text.splitlines() if "demo-repo" in line)
    assert "installed" in name_line
    assert ">installed<" in text
    other_line = next(line for line in text.splitlines() if "other-repo" in line)
    assert "installed" not in other_line
    buttons = _markup_buttons(markup)
    install = [b for b in buttons if b[1].startswith(b"skm:i:")]
    manage = [b for b in buttons if b[1].startswith(b"skm:m:")]
    assert len(install) == 1 and not install[0][2] and not install[0][3]
    assert len(manage) == 1 and manage[0][2] and not manage[0][3]


def test_market_dangling_repo_shows_not_available_without_button(market_home):
    """A market.json repo row with no repo URL gets a badge, never a button."""
    _write_market_json(market_home, [{
        "name": "ghost-repo",
        "author": "example",
        "repo": "",
        "source_url": "",
        "skill_count": 1,
        "size_bytes": 10,
        "skills": [{
            "name": "ghost-skill", "path": "ghost-skill",
            "skill_md": "ghost-skill/SKILL.md",
            "description": "Gone from the disk.",
            "files": 1, "size_bytes": 10,
        }],
    }])
    target_home = market_home / "profiles" / "gc-1"

    text, markup = market._render(target_home)

    ghost_line = next(line for line in text.splitlines() if "ghost-repo" in line)
    assert "not available" in ghost_line
    assert all(b"ghost-repo" not in data for _label, data, _s, _d in _markup_buttons(markup))


def test_market_badge_is_inline_not_a_pill_row(market_home):
    """The badge sits on the name line; no line is a lone pill button."""
    _write_market_json(market_home, [
        _demo_repo("demo-repo"),
        _repo_row("other-repo", "https://github.com/example/other", [
            ("other-a", "other-a"),
        ]),
    ])
    target_home = market_home / "profiles" / "gc-1"
    for skill_name in ("demo-a", "demo-b"):
        owned = target_home / "skills" / skill_name
        owned.mkdir(parents=True)
        (owned / "SKILL.md").write_text(_skill_md(skill_name), encoding="utf-8")

    text, _markup = market._render(target_home)

    for line in text.splitlines():
        if "<tg-button" in line:
            assert "demo-repo" in line, "a badge must share its line with the repo name"
    assert text.count("<tg-button") == 1


def test_market_manage_prompt_disables_all_skills_keeping_files(market_home):
    """Disable adds every repo skill to skills.disabled; the files stay put."""
    repo = _demo_repo()
    _write_market_json(market_home, [repo])
    target_home, adapter, source = _install_owned_repo(market_home, repo)
    prompt_buttons = _open_manage_prompt(adapter, source, "demo-repo")
    assert [label for label, _d, _s, _dd in prompt_buttons] == ["Disable", "Remove", "Cancel"]

    token = _token_for("demo-repo", "disable")
    prompt_event = _FakeEvent(message_id="42")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, prompt_event, source, f"skm:d:{token}")
    )

    from hermes_cli.config import read_user_config_raw

    cfg = read_user_config_raw(target_home / "config.yaml")
    assert cfg["skills"]["disabled"] == ["demo-a", "demo-b"]
    assert (target_home / "skills" / "demo-a" / "SKILL.md").is_file()
    assert (target_home / "skills" / "demo-b" / "SKILL.md").is_file()
    assert ("123", "42") in adapter.deletes, "the prompt message goes away"
    assert adapter.edits, "the market message re-renders after disable"
    assert adapter.edits[-1].id == 7, "the re-render edits the market message"


def test_market_manage_prompt_remove_uninstalls_all_skills(market_home):
    """Remove from the prompt uninstalls every repo skill from the profile only."""
    repo = _demo_repo()
    _write_market_json(market_home, [repo])
    target_home, adapter, source = _install_owned_repo(market_home, repo)
    _open_manage_prompt(adapter, source, "demo-repo")

    token = _token_for("demo-repo", "remove")
    prompt_event = _FakeEvent(message_id="42")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, prompt_event, source, f"skm:r:{token}")
    )

    assert not (target_home / "skills" / "demo-a").exists()
    assert not (target_home / "skills" / "demo-b").exists()
    assert ("123", "42") in adapter.deletes, "the prompt message goes away"
    assert adapter.edits, "the market message re-renders after remove"
    assert adapter.edits[-1].id == 7, "the re-render edits the market message"


def test_market_cancel_deletes_and_back_pages(market_home):
    """Cancel dismisses the message; Back steps a page, or leaves on page 1."""
    repos = [
        _repo_row(f"repo-{i:02d}", f"https://github.com/example/repo-{i:02d}", [
            (f"skill-{i:02d}", f"skill-{i:02d}"),
        ])
        for i in range(12)
    ]
    _write_market_json(market_home, repos)
    target_home = market_home / "profiles" / "gc-1"
    adapter = _FakeAdapter(target_home)
    source = SimpleNamespace(chat_id="123", chat_type="dm", user_id="777", thread_id=None)
    event = _FakeEvent(message_id="7")

    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, "skm:back:1")
    )
    assert adapter.edits, "back re-renders the previous page"
    assert adapter.edits[-1].id == 7
    assert "Page 1 of 2" in adapter.edits[-1].message

    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, "skm:back:0")
    )
    assert ("123", "7") in adapter.deletes, "back on page 1 leaves the market"

    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, "skm:cancel")
    )
    assert adapter.deletes.count(("123", "7")) == 2, "cancel dismisses the message"


# --- Market runs off the repo index (no skill dirs on the host) ---

def test_market_renders_repo_entries_from_bundled_json_with_no_skill_dirs(
        market_home, monkeypatch):
    """Bundled repo rows render as installable — no skill dirs needed."""
    bundled = market_home / "bundled.json"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_text(json.dumps([
        _demo_repo("bundled-repo"),
        _repo_row("other-repo", "https://github.com/example/other", [
            ("other-a", "other-a"),
        ]),
    ]), encoding="utf-8")
    monkeypatch.setattr(market, "_BUNDLED_MARKET_JSON", bundled, raising=False)
    target_home = market_home / "profiles" / "gc-1"

    text, markup = market._render(target_home)

    assert "bundled-repo" in text and "other-repo" in text
    assert "2 skills" in text
    assert "not available" not in text
    buttons = _markup_buttons(markup)
    installs = [b for b in buttons if b[1].startswith(b"skm:i:")]
    assert len(installs) == 2
    assert all(not b[2] and not b[3] for b in installs), "install buttons stay plain"


def test_market_install_fetches_whole_repo_on_demand(market_home, monkeypatch):
    """No local dirs: install fetches the whole repo, then copies every skill in."""
    target_home, adapter, source = _install_setup(market_home, [_demo_repo()])
    event = _FakeEvent(message_id="7")

    calls = []

    def fake_fetch(source_url, dest):
        calls.append((source_url, dest))
        for skill_name, path in [("demo-a", "skills/demo-a"), ("demo-b", "skills/demo-b")]:
            skill = Path(dest) / path
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(_skill_md(skill_name), encoding="utf-8")

    monkeypatch.setattr(market, "_fetch_repo_from_repo", fake_fetch, raising=False)

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert calls, "the install must fetch the repo"
    assert calls[0][0] == "https://github.com/example/demo-repo"
    assert (target_home / "skills" / "demo-a" / "SKILL.md").is_file()
    assert (target_home / "skills" / "demo-b" / "SKILL.md").is_file()
    assert adapter.edits, "the market message re-renders after install"
    assert "Installed demo-repo" in adapter.edits[-1].message


def test_market_repo_installed_only_when_all_skills_present(market_home):
    """Installed = every skill dir in the profile. One missing skill means
    the repo is not installed: plain Install button, no badge."""
    _write_market_json(market_home, [_demo_repo()])
    target_home = market_home / "profiles" / "gc-1"
    owned = target_home / "skills" / "demo-a"
    owned.mkdir(parents=True)
    (owned / "SKILL.md").write_text(_skill_md("demo-a"), encoding="utf-8")

    text, markup = market._render(target_home)
    assert "installed" not in next(line for line in text.splitlines() if "demo-repo" in line)
    buttons = _markup_buttons(markup)
    assert [b for b in buttons if b[1].startswith(b"skm:i:")]
    assert not [b for b in buttons if b[1].startswith(b"skm:m:")]

    owned_b = target_home / "skills" / "demo-b"
    owned_b.mkdir(parents=True)
    (owned_b / "SKILL.md").write_text(_skill_md("demo-b"), encoding="utf-8")

    text, markup = market._render(target_home)
    assert "installed" in next(line for line in text.splitlines() if "demo-repo" in line)
    buttons = _markup_buttons(markup)
    assert [b for b in buttons if b[1].startswith(b"skm:m:")]
    assert not [b for b in buttons if b[1].startswith(b"skm:i:")]


def test_market_fetch_failure_reports_plainly_and_leaves_nothing(market_home, monkeypatch):
    """A failed fetch says so on the card and leaves no half-copied skills."""
    target_home, adapter, source = _install_setup(market_home, [_demo_repo()])
    event = _FakeEvent(message_id="7")

    def failing_fetch(source_url, dest):
        raise RuntimeError("repository unreachable")

    monkeypatch.setattr(market, "_fetch_repo_from_repo", failing_fetch, raising=False)

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert not (target_home / "skills" / "demo-a").exists()
    assert not (target_home / "skills" / "demo-b").exists()
    assert adapter.edits, "the failure must be reported on the card"
    assert "Could not install demo-repo" in adapter.edits[-1].message


def test_market_fetch_missing_skill_reports_plainly_and_leaves_nothing(
        market_home, monkeypatch):
    """The fetch succeeds but one skill has no SKILL.md: nothing is installed."""
    target_home, adapter, source = _install_setup(market_home, [_demo_repo()])
    event = _FakeEvent(message_id="7")

    def partial_fetch(source_url, dest):
        skill = Path(dest) / "skills" / "demo-a"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(_skill_md("demo-a"), encoding="utf-8")
        # demo-b never arrives: no SKILL.md for it anywhere in the clone.

    monkeypatch.setattr(market, "_fetch_repo_from_repo", partial_fetch, raising=False)

    market._render(target_home)
    token = _token_for("demo-repo", "install")
    asyncio.get_event_loop().run_until_complete(
        market.handle_market_callback(adapter, event, source, f"skm:i:{token}")
    )

    assert not (target_home / "skills" / "demo-a").exists()
    assert not (target_home / "skills" / "demo-b").exists()
    assert "Could not install demo-repo" in adapter.edits[-1].message


def test_market_bundled_index_fallback_and_host_override(market_home, monkeypatch):
    """No host market.json: the shipped index renders. A host copy wins over it."""
    bundled = market_home / "bundled.json"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_text(json.dumps([_demo_repo("bundled-repo")]), encoding="utf-8")
    monkeypatch.setattr(market, "_BUNDLED_MARKET_JSON", bundled, raising=False)
    target_home = market_home / "profiles" / "gc-1"

    text, _markup = market._render(target_home)
    assert "bundled-repo" in text

    _write_market_json(market_home, [_demo_repo("host-repo")])
    text, _markup = market._render(target_home)
    assert "host-repo" in text
    assert "bundled-repo" not in text, "a host market.json wins over the shipped index"


def test_market_fetch_builds_sparse_blobless_clone(monkeypatch, tmp_path):
    """The whole-repo fetch is a blobless sparse clone, then the full tree."""
    calls = []

    def fake_run_git(argv, cwd, timeout, **kwargs):
        calls.append((list(argv), cwd, timeout))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(market, "_run_git", fake_run_git, raising=False)
    dest = tmp_path / "repo"

    market._fetch_repo_from_repo("https://github.com/example/demo-repo", dest)

    clone_argv = calls[0][0]
    assert Path(clone_argv[0]).name == "git"
    assert clone_argv[1:] == [
        "clone", "--filter=blob:none", "--depth", "1", "--sparse",
        "https://github.com/example/demo-repo", str(dest),
    ]
    checkout_argv = calls[1][0]
    assert Path(checkout_argv[0]).name == "git"
    assert checkout_argv[1:] == [
        "-C", str(dest), "sparse-checkout", "disable",
    ]


def test_market_fetch_rejects_failed_clone(monkeypatch, tmp_path):
    """A clone that fails raises plainly instead of installing nothing silently."""
    def failing_run_git(argv, cwd, timeout, **kwargs):
        # Faithful to _run_git: a nonzero git exit raises, it never returns it.
        raise RuntimeError("git clone failed: fatal: repository not found")

    monkeypatch.setattr(market, "_run_git", failing_run_git, raising=False)

    with pytest.raises(RuntimeError, match="clone failed"):
        market._fetch_repo_from_repo(
            "https://github.com/example/nope", tmp_path / "repo")


def test_market_bundled_index_has_the_three_repos():
    """The shipped index holds exactly caveman, anthropics/skills and
    obra/superpowers, with every skill, paths and verbatim descriptions."""
    raw = json.loads(
        (Path(market.__file__).with_name("market.json")).read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    by_name = {row["name"]: row for row in raw}
    assert set(by_name) == {"caveman", "skills", "superpowers"}
    assert by_name["caveman"]["author"] == "juliusbrussee"
    assert by_name["skills"]["author"] == "anthropics"
    assert by_name["superpowers"]["author"] == "obra"
    assert by_name["caveman"]["skill_count"] == 22
    assert by_name["skills"]["skill_count"] == 18
    assert by_name["superpowers"]["skill_count"] == 14
    for row in raw:
        assert row["repo"] == row["source_url"]
        assert row["repo"].startswith("https://github.com/")
        assert row["skill_count"] == len(row["skills"])
        assert row["size_bytes"] == sum(s["size_bytes"] for s in row["skills"])
        for skill in row["skills"]:
            assert market._NAME_RE.match(str(skill.get("name") or ""))
            assert market._safe_repo_path(str(skill.get("path") or ""))
            assert skill["skill_md"] == f"{skill['path']}/SKILL.md"
            assert str(skill.get("description") or "").strip()
            assert isinstance(skill["files"], int) and isinstance(skill["size_bytes"], int)
    cavecrew = next(
        s for s in by_name["caveman"]["skills"] if s["name"] == "cavecrew")
    assert cavecrew["description"] == (
        "When to delegate to `cavecrew-investigator` (locate code), "
        "`cavecrew-builder` (1-2 file edit) or `cavecrew-reviewer` (diff review) "
        "instead of working inline or using `Explore`. Their output is "
        "compressed, so main context lasts longer.")
    brainstorming = next(
        s for s in by_name["superpowers"]["skills"] if s["name"] == "brainstorming")
    assert brainstorming["description"] == (
        "You MUST use this before any creative work - creating features, building "
        "components, adding functionality, modifying behavior, or planning anything "
        "new, in software or out of it (a talk, a business, a renovation).")
    academy = next(
        s for s in by_name["skills"]["skills"] if s["name"] == "academy-guide")
    assert academy["description"].startswith(
        "Stop and check this skill before finishing any reply to a question about "
        "how to use Claude or a Claude product")
    assert academy["description"].rstrip().endswith("never invent Academy content.")
