"""Durable contracts for the bundled create-template skill.

Ports the upstream template-draft guarantees (src/templates/draft.test.ts):
request-specific drafts, no shared pointer, private-path and
credential-like refusals, symlink refusal — with the Hermes packaging
half: export to one shareable file and import back into a draft.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "productivity" / "create-template"
SKILL_MD = SKILL_DIR / "SKILL.md"
DRAFT_FORMAT = SKILL_DIR / "references" / "draft-format.md"
SCRIPT = SKILL_DIR / "scripts" / "template_export.py"


def _parse_frontmatter(content: str) -> dict:
    from agent.skill_utils import parse_frontmatter

    frontmatter, _ = parse_frontmatter(content)
    return frontmatter


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    return _parse_frontmatter(skill_text)


def test_skill_file_and_frontmatter_are_complete(skill_text, frontmatter):
    assert SKILL_MD.is_file()
    assert DRAFT_FORMAT.is_file()
    assert SCRIPT.is_file()
    assert skill_text.startswith("---")
    assert frontmatter["name"] == "create-template"
    description = frontmatter["description"]
    assert len(description) <= 60 and description.endswith(".")
    assert frontmatter["version"] == "1.0.0"
    assert frontmatter["metadata"]["hermes"]["category"] == "productivity"
    assert frontmatter["metadata"]["hermes"]["tags"]


def test_skill_sections_follow_the_modern_order(skill_text):
    headings = re.findall(r"^## (.+)$", skill_text, re.MULTILINE)
    assert headings == [
        "When to Use",
        "Prerequisites",
        "How to Run",
        "Quick Reference",
        "Procedure",
        "Pitfalls",
        "Verification",
    ]
    assert skill_text.startswith("---\nname: create-template")


def test_skill_uses_hermes_inventory_and_reuses_skill_manage(skill_text):
    assert "skills_list" in skill_text
    assert "hermes plugins list" in skill_text
    assert "hermes plugins show" in skill_text
    assert "hermes skills search" in skill_text
    assert "skill_manage" in skill_text
    assert "template_export.py" in skill_text
    assert "template-drafts" in skill_text


def test_skill_carries_no_external_only_machinery(skill_text):
    assert "cli.mjs" not in skill_text
    assert ".openclaw/template-drafts" not in skill_text
    assert "Control UI opens" not in skill_text


def _run(*argv: str, home: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if home is not None:
        env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True, text=True, check=False, env=env,
    )


def _complete_draft(name: str = "research-brief") -> dict:
    return {
        "name": name,
        "description": "A collaborative research partner.",
        "version": "0.1.0",
        "scope": {
            "input": "A research question and source links.",
            "job": "Research the question and draft a brief.",
            "output": "A short brief with sources.",
            "authority": "Read public sources; never publish or spend.",
        },
        "manifest": (
            "---\nschemaVersion: 1\nagent:\n"
            f"  id: {name}\n"
            "  name: Research Brief\n"
            "  description: A collaborative research partner.\n"
            "---\nResearch the question and draft a brief with sources.\n"
        ),
        "files": [
            {"path": "workspace/AGENTS.md", "content": "Follow the brief checklist.\n"},
            {"path": "workspace/reference/checklist.md", "content": "1. Cite every claim.\n"},
        ],
    }


def _draft_path(home: Path, draft_id: str) -> Path:
    return home / "template-drafts" / f"{draft_id}.json"


def _new_draft(home: Path, name: str = "research-brief") -> str:
    result = _run("new", "--name", name, "--home", str(home))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["path"] == str(_draft_path(home, payload["draftId"]))
    return payload["draftId"]


def _write_draft(home: Path, draft_id: str, draft: dict) -> Path:
    path = _draft_path(home, draft_id)
    path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    return path


def test_new_mints_a_private_request_specific_draft(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    assert uuid.UUID(draft_id).version == 4
    path = _draft_path(home, draft_id)
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    draft = json.loads(path.read_text(encoding="utf-8"))
    assert draft["name"] == "research-brief"
    assert draft["version"] == "0.1.0"
    assert draft["scope"] == {"input": "", "job": "", "output": "", "authority": ""}
    assert draft["files"] == []


def test_new_refuses_bad_name_version_and_duplicate_id(tmp_path):
    home = tmp_path / "home"
    assert _run("new", "--name", "Bad Name", "--home", str(home)).returncode == 1
    assert _run("new", "--name", "ok-name", "--version", "1.0",
                "--home", str(home)).returncode == 1
    draft_id = str(uuid.uuid4())
    assert _run("new", "--name", "ok-name", "--draft-id", draft_id,
                "--home", str(home)).returncode == 0
    second = _run("new", "--name", "ok-name", "--draft-id", draft_id,
                  "--home", str(home))
    assert second.returncode == 1
    assert "draft_exists" in second.stderr
    assert _run("new", "--name", "ok-name", "--draft-id", "not-a-uuid",
                "--home", str(home)).returncode == 1


def test_fresh_draft_fails_validation_until_scope_is_complete(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    result = _run("validate", draft_id, "--home", str(home))
    assert result.returncode == 1
    assert "incomplete_draft" in result.stderr


def test_export_import_round_trip_reproduces_the_draft(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    draft_id = _new_draft(home_a)
    draft = _complete_draft()
    _write_draft(home_a, draft_id, draft)
    assert _run("validate", draft_id, "--home", str(home_a)).returncode == 0

    exported = _run("export", draft_id, "--home", str(home_a))
    assert exported.returncode == 0, exported.stderr
    out = json.loads(exported.stdout)
    export_path = Path(out["path"])
    assert export_path.name == "research-brief-0.1.0.hermes-template"
    assert (export_path.stat().st_mode & 0o777) == 0o600
    envelope = json.loads(export_path.read_text(encoding="utf-8"))
    assert envelope["format"] == "hermes-template"
    assert envelope["formatVersion"] == 1
    assert envelope["draft"] == draft

    imported = _run("import", str(export_path), "--home", str(home_b))
    assert imported.returncode == 0, imported.stderr
    payload = json.loads(imported.stdout)
    assert payload["draftId"] != draft_id
    assert payload["name"] == "research-brief"
    restored = json.loads(_draft_path(home_b, payload["draftId"]).read_text(encoding="utf-8"))
    assert restored == draft
    assert _run("validate", payload["draftId"], "--home", str(home_b)).returncode == 0

    reexported = _run("export", payload["draftId"], "--home", str(home_b))
    assert reexported.returncode == 0
    second = json.loads(Path(json.loads(reexported.stdout)["path"]).read_text(encoding="utf-8"))
    assert second["draft"] == draft


def test_export_resolves_a_bare_id_from_hermes_home_env(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    _write_draft(home, draft_id, _complete_draft())
    result = _run("export", draft_id, home=home)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "research-brief"


@pytest.mark.parametrize("path", [
    "MEMORY.md",
    "memory/notes.md",
    "USER.md",
    ".env",
    "sessions/log.jsonl",
    "history.JSONL",
    "../escape.md",
])
def test_export_refuses_private_or_escaping_draft_files(tmp_path, path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    draft = _complete_draft()
    draft["files"] = [{"path": path, "content": "private context"}]
    _write_draft(home, draft_id, draft)
    result = _run("export", draft_id, "--home", str(home))
    assert result.returncode == 1
    assert "private_draft_file" in result.stderr


def test_export_refuses_credential_like_text_without_echoing_it(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    secret = f"ghp_{'x' * 36}"
    draft = _complete_draft()
    draft["manifest"] = draft["manifest"] + f"\n{secret}\n"
    _write_draft(home, draft_id, draft)
    result = _run("export", draft_id, "--home", str(home))
    assert result.returncode == 1
    assert "sensitive_draft_content" in result.stderr
    assert secret not in result.stderr
    assert secret not in result.stdout


def test_export_refuses_duplicate_files_bad_version_and_bad_name(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    draft = _complete_draft()
    draft["files"] = [
        {"path": "workspace/AGENTS.md", "content": "one"},
        {"path": "Workspace/agents.md", "content": "two"},
    ]
    _write_draft(home, draft_id, draft)
    result = _run("export", draft_id, "--home", str(home))
    assert result.returncode == 1
    assert "duplicate_draft_file" in result.stderr

    draft = _complete_draft()
    draft["version"] = "1.0"
    _write_draft(home, draft_id, draft)
    assert _run("export", draft_id, "--home", str(home)).returncode == 1

    draft = _complete_draft()
    draft["name"] = "Bad Name"
    _write_draft(home, draft_id, draft)
    assert _run("export", draft_id, "--home", str(home)).returncode == 1


def test_export_allows_placeholder_credential_values(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    draft = _complete_draft()
    draft["files"] = [
        {"path": "workspace/setup.md",
         "content": "Set api_key: YOUR_API_KEY before first use.\n"},
    ]
    _write_draft(home, draft_id, draft)
    assert _run("export", draft_id, "--home", str(home)).returncode == 0


def test_import_rejects_non_template_and_colliding_id(tmp_path):
    home = tmp_path / "home"
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    result = _run("import", str(plain), "--home", str(home))
    assert result.returncode == 1
    assert "invalid_export" in result.stderr

    draft_id = _new_draft(home)
    _write_draft(home, draft_id, _complete_draft())
    exported = _run("export", draft_id, "--home", str(home))
    export_path = Path(json.loads(exported.stdout)["path"])
    colliding = _run("import", str(export_path), "--draft-id", draft_id, "--home", str(home))
    assert colliding.returncode == 1
    assert "draft_exists" in colliding.stderr


def test_validate_refuses_a_symlinked_draft(tmp_path):
    home = tmp_path / "home"
    draft_id = _new_draft(home)
    _write_draft(home, draft_id, _complete_draft())
    real = _draft_path(home, draft_id)
    link_home = tmp_path / "link-home"
    link_home.mkdir()
    link = _draft_path(link_home, draft_id)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    result = _run("validate", str(link), "--home", str(link_home))
    assert result.returncode == 1
    assert "invalid_draft" in result.stderr


def test_import_rejects_a_draft_with_unknown_fields(tmp_path):
    home = tmp_path / "home"
    draft = _complete_draft()
    draft["surprise"] = True
    envelope = {"format": "hermes-template", "formatVersion": 1, "draft": draft}
    export_path = tmp_path / "bad.hermes-template"
    export_path.write_text(json.dumps(envelope), encoding="utf-8")
    result = _run("import", str(export_path), "--home", str(home))
    assert result.returncode == 1
    assert "invalid_draft" in result.stderr
