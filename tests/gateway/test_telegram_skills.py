import io
from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from gateway.telegram_skills import (
    MAX_ARCHIVE_BYTES, MAX_DOWNLOAD_BYTES, MAX_UPLOAD_BYTES, SkillValidationError,
    export_personal_skill, include_environment_files, install_skill, list_personal_skills,
    personal_skills_root, prepare_github, prepare_skill, prepare_upload,
    read_personal_skill, remove_personal_skill, _github_url,
)

SKILL = "---\nname: daily-notes\ndescription: Helps write daily notes.\n---\n\n# Instructions\nWrite a concise note.\n"


def test_frontmatter_and_instruction_validation():
    candidate = prepare_skill(SKILL)
    assert (candidate.name, candidate.description) == ("daily-notes", "Helps write daily notes.")
    assert candidate.files["SKILL.md"].decode() == SKILL
    for source, error in [
        ("# no frontmatter", "frontmatter"),
        ("---\nname: BAD NAME\ndescription: ok\n---\nbody", "slug"),
        ("---\nname: valid\ndescription: ''\n---\nbody", "description"),
        ("---\nname: valid\ndescription: ok\n---\n  ", "nonempty"),
        ("---\nname: [bad\ndescription: nope\n---\nx", "YAML"),
    ]:
        with pytest.raises(SkillValidationError, match=error):
            prepare_skill(source)


def test_upload_markdown_zip_and_zip_security():
    assert prepare_upload(SKILL.encode(), "thing.md")[0].name == "daily-notes"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("daily-notes/SKILL.md", SKILL)
        z.writestr("daily-notes/examples/example.txt", "example")
    candidate = prepare_upload(buffer.getvalue(), "upload.zip")[0]
    assert candidate.files["examples/example.txt"] == b"example"
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../SKILL.md", SKILL)
    with pytest.raises(SkillValidationError, match="Unsafe"):
        prepare_upload(bad.getvalue(), "bad.zip")


def test_rejects_credentials_and_non_skill_uploads():
    with pytest.raises(SkillValidationError, match="credential"):
        prepare_skill(SKILL + "ghp_" + "A" * 40)
    hidden_secret = io.BytesIO()
    with zipfile.ZipFile(hidden_secret, "w") as z:
        z.writestr("skill/SKILL.md", SKILL)
        z.writestr("skill/.env", "API_KEY=example-secret")
    candidate = prepare_upload(hidden_secret.getvalue(), "secret.zip")[0]
    assert ".env" not in candidate.files
    assert candidate.environment_files == {".env": b"API_KEY=example-secret"}
    assert any("explicit choice" in warning for warning in candidate.warnings)
    with pytest.raises(SkillValidationError, match=".md or .zip"):
        prepare_upload(b"x", "payload.py")


@pytest.mark.parametrize("filename", [".env", ".env.sample", ".env.test", ".env.template"])
def test_dotenv_variants_require_explicit_include_before_private_storage(tmp_path, filename):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("daily-notes/SKILL.md", SKILL)
        z.writestr(f"daily-notes/{filename}", "API_KEY=publisher-value")
    candidate = prepare_upload(archive.getvalue(), "skill.zip")[0]

    assert candidate.environment_files == {filename: b"API_KEY=publisher-value"}
    with pytest.raises(SkillValidationError, match="explicit Include or Exclude"):
        install_skill(tmp_path, "123", candidate)

    included = include_environment_files(candidate)
    assert included.environment_files == {}
    assert included.environment_files_included is True
    assert included.files[filename] == b"API_KEY=publisher-value"
    installed = install_skill(
        tmp_path, "123", included, include_environment_files=True,
    )
    assert (installed / filename).read_text(encoding="utf-8") == "API_KEY=publisher-value"
    assert read_personal_skill(tmp_path, "123", "daily-notes").files[filename] == b"API_KEY=publisher-value"


def test_skill_archive_and_github_download_limits_are_25_mib():
    assert MAX_ARCHIVE_BYTES == 25 * 1024 * 1024
    assert MAX_UPLOAD_BYTES == 25 * 1024 * 1024
    assert MAX_DOWNLOAD_BYTES == 25 * 1024 * 1024


def test_private_user_storage_and_replace(tmp_path):
    first = prepare_skill(SKILL)
    stored = install_skill(tmp_path, "123", first)
    assert stored == personal_skills_root(tmp_path, "123") / "daily-notes"
    assert stored.stat().st_mode & 0o777 == 0o700
    assert (stored / "SKILL.md").stat().st_mode & 0o777 == 0o600
    assert read_personal_skill(tmp_path, "123", "daily-notes").digest == first.digest
    assert list_personal_skills(tmp_path, "123") == [{"name": "daily-notes", "description": "Helps write daily notes."}]
    assert list_personal_skills(tmp_path, "456") == []
    with pytest.raises(FileExistsError):
        install_skill(tmp_path, "123", first)
    assert export_personal_skill(tmp_path, "123", "daily-notes").startswith(b"PK")
    assert remove_personal_skill(tmp_path, "123", "daily-notes")
    assert not remove_personal_skill(tmp_path, "123", "daily-notes")


def test_nested_private_skill_is_discovered_after_restart(tmp_path):
    root = personal_skills_root(tmp_path, "123")
    nested = root / "legacy-container" / "private-runbook"
    nested.mkdir(parents=True)
    skill_text = SKILL.replace("daily-notes", "private-runbook")
    (nested / "SKILL.md").write_text(skill_text, encoding="utf-8")

    fresh_home = Path(str(tmp_path))
    expected = [{"name": "private-runbook", "description": "Helps write daily notes."}]
    assert list_personal_skills(fresh_home, "123") == expected
    assert read_personal_skill(fresh_home, "123", "private-runbook").files["SKILL.md"] == skill_text.encode()
    assert list_personal_skills(fresh_home, "456") == []
    assert (nested / "SKILL.md").is_file()


def test_github_urls_are_restricted_before_network():
    for url in (
        "http://github.com/owner/repo", "https://user@github.com/owner/repo",
        "https://127.0.0.1/owner/repo", "https://example.com/owner/repo",
        "https://github.com/owner/repo/blob/main/../SKILL.md",
    ):
        with pytest.raises(SkillValidationError):
            prepare_github(url)


def test_zip_at_archive_root_keeps_companions():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("SKILL.md", SKILL)
        z.writestr("reference.txt", "companion")
    candidate = prepare_upload(buffer.getvalue(), "root.zip")[0]
    assert candidate.files["reference.txt"] == b"companion"


def test_profile_and_user_storage_uses_runtime_numeric_paths(tmp_path):
    first_home, second_home = tmp_path / "profile-a", tmp_path / "profile-b"
    skill = prepare_skill(SKILL)
    one = install_skill(first_home, "123", skill)
    two = install_skill(first_home, "456", skill)
    three = install_skill(second_home, "123", skill)
    assert one == first_home / "personal/telegram/users/123/skills/daily-notes"
    assert len({one, two, three}) == 3
    assert list_personal_skills(first_home, "123") and not list_personal_skills(second_home, "456")
    with pytest.raises(ValueError):
        personal_skills_root(first_home, "../123")


def test_install_rejects_tampered_candidate_name(tmp_path):
    candidate = prepare_skill(SKILL)
    candidate.name = "../outside"
    with pytest.raises(SkillValidationError, match="Candidate name"):
        install_skill(tmp_path, "123", candidate)


def test_install_boundary_rejects_unreviewed_environment_file_even_with_flag(tmp_path):
    candidate = replace(
        prepare_skill(SKILL),
        files={"SKILL.md": SKILL.encode(), ".env.sample": b"API_KEY=publisher-value"},
    )
    with pytest.raises(SkillValidationError, match="explicit user approval"):
        install_skill(tmp_path, "123", candidate, include_environment_files=True)


def test_dangerous_scanner_verdict_is_a_deterministic_owner_review_warning(monkeypatch):
    from tools import skills_guard
    original = skills_guard.scan_skill
    class Scan:
        verdict = "dangerous"
        summary = "critical behavior detected"
        findings = []
    monkeypatch.setattr(skills_guard, "scan_skill", lambda *a, **k: Scan())
    candidate = prepare_skill(SKILL)
    assert candidate.dangerous is True
    assert any("DANGEROUS SECURITY SCAN VERDICT" in warning and "critical behavior detected" in warning
               for warning in candidate.warnings)
    monkeypatch.setattr(skills_guard, "scan_skill", original)


def test_github_tree_keeps_selected_subtree_and_normal_repo_metadata(monkeypatch):
    assert _github_url("https://github.com/o/r/tree/main/skills/demo") == (
        "tree", "https://codeload.github.com/o/r/zip/refs/heads/main", "skills/demo")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("repo-main/.gitignore", "ignored")
        z.writestr("repo-main/.github/workflows/ci.yml", "ignored")
        z.writestr("repo-main/README.md", "unrelated")
        z.writestr("repo-main/skills/demo/SKILL.md", SKILL)
        z.writestr("repo-main/skills/demo/guide.txt", "selected companion")
        z.writestr("repo-main/skills/demo/.env.sample", "API_KEY=publisher-value")
    monkeypatch.setattr("gateway.telegram_skills._download", lambda _: buffer.getvalue())
    candidates = prepare_github("https://github.com/o/r/tree/main/skills/demo")
    assert len(candidates) == 1
    assert candidates[0].files["guide.txt"] == b"selected companion"
    assert candidates[0].environment_files == {".env.sample": b"API_KEY=publisher-value"}


def test_default_github_repo_zip_ignores_repository_metadata(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("repo-main/.gitignore", "ignored")
        z.writestr("repo-main/.github/workflows/ci.yml", "ignored")
        z.writestr("repo-main/README.md", "repository overview")
        z.writestr("repo-main/skills/demo/SKILL.md", SKILL)
        z.writestr("repo-main/skills/demo/reference.txt", "guide")
        z.writestr("repo-main/skills/demo/.env.test", "TOKEN=publisher-value")
    monkeypatch.setattr("gateway.telegram_skills._download", lambda _: buffer.getvalue())
    candidates = prepare_github("https://github.com/o/r")
    assert len(candidates) == 1
    assert candidates[0].files["reference.txt"] == b"guide"
    assert candidates[0].environment_files == {".env.test": b"TOKEN=publisher-value"}


def test_skill_only_repo_with_many_skills_is_sorted_and_keeps_companions_separate(monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("repo-main/README.md", "repository metadata")
        for index in reversed(range(24)):
            name = f"skill-{index:02d}"
            skill = SKILL.replace("daily-notes", name)
            z.writestr(f"repo-main/skills/{name}/SKILL.md", skill)
            z.writestr(f"repo-main/skills/{name}/reference.txt", name)
    monkeypatch.setattr("gateway.telegram_skills._download", lambda _: archive.getvalue())

    candidates = prepare_github("https://github.com/o/r")

    assert [candidate.name for candidate in candidates] == [f"skill-{i:02d}" for i in range(24)]
    assert all(candidate.files["reference.txt"] == candidate.name.encode() for candidate in candidates)
    assert all(len(candidate.files) == 2 for candidate in candidates)


def test_nested_skill_files_belong_to_nearest_skill_and_batch_install_is_no_overwrite(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("parent/SKILL.md", SKILL.replace("daily-notes", "parent"))
        z.writestr("parent/parent.txt", "parent companion")
        z.writestr("parent/child/SKILL.md", SKILL.replace("daily-notes", "child"))
        z.writestr("parent/child/child.txt", "child companion")
    candidates = prepare_upload(archive.getvalue(), "nested.zip")
    by_name = {candidate.name: candidate for candidate in candidates}

    assert by_name["parent"].files == {
        "SKILL.md": SKILL.replace("daily-notes", "parent").encode(),
        "parent.txt": b"parent companion",
    }
    assert by_name["child"].files["child.txt"] == b"child companion"

    from gateway.telegram_skills import install_skills
    installed = install_skills(tmp_path, "123", candidates)
    assert {path.name for path in installed} == {"parent", "child"}
    with pytest.raises(FileExistsError, match="already exists"):
        install_skills(tmp_path, "123", candidates)
    assert {item["name"] for item in list_personal_skills(tmp_path, "123")} == {"parent", "child"}


def test_nested_skills_keep_each_environment_file_with_its_nearest_skill():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("parent/SKILL.md", SKILL.replace("daily-notes", "parent"))
        z.writestr("parent/.env.sample", "PARENT=value")
        z.writestr("parent/child/SKILL.md", SKILL.replace("daily-notes", "child"))
        z.writestr("parent/child/.env.template", "CHILD=value")

    candidates = prepare_upload(archive.getvalue(), "nested.zip")
    by_name = {candidate.name: candidate for candidate in candidates}
    assert by_name["parent"].environment_files == {".env.sample": b"PARENT=value"}
    assert by_name["child"].environment_files == {".env.template": b"CHILD=value"}


def test_package_root_environment_file_prompts_and_explicitly_copies_to_all_skills(tmp_path):
    from gateway.telegram_skills import include_environment_files, install_skills

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("repo/skills/one/SKILL.md", SKILL.replace("daily-notes", "one"))
        z.writestr("repo/skills/two/SKILL.md", SKILL.replace("daily-notes", "two"))
        z.writestr("repo/.env.sample", "SHARED=value")

    candidates = prepare_upload(archive.getvalue(), "repo.zip")
    assert len(candidates) == 2
    assert all(candidate.environment_files == {".env.sample": b"SHARED=value"} for candidate in candidates)
    assert all(any("copy them into each of the 2 skills" in warning for warning in candidate.warnings)
               for candidate in candidates)

    approved = [include_environment_files(candidate) for candidate in candidates]
    installed = install_skills(tmp_path, "123", approved, include_environment_files=True)
    assert {(path / ".env.sample").read_bytes() for path in installed} == {b"SHARED=value"}


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _noise_image_bytes(width, height, fmt, **save_kwargs):
    """Incompressible-noise image: a real image whose bytes stay large."""
    import os

    from PIL import Image

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save_kwargs)
    return buffer.getvalue()


def _skill_zip_with_asset(name, body):
    return _zip_bytes({"daily-notes/SKILL.md": SKILL, f"daily-notes/{name}": body})


def test_large_png_asset_installs_without_per_file_cap():
    """A plugin icon PNG over 1 MiB installs: the per-file cap is gone (the
    4 MiB per-skill total still bounds the skill). Noise does not shrink on
    re-encode, so the bytes install unchanged."""
    png = _noise_image_bytes(1000, 1000, "PNG", optimize=True)
    assert len(png) > 1_048_576

    candidate = prepare_upload(_skill_zip_with_asset("assets/plugin.png", png), "skill.zip")[0]

    assert candidate.files["assets/plugin.png"] == png
    assert not any("pre-formatted" in warning for warning in candidate.warnings)


def test_oversized_image_is_preformatted_on_install():
    """An oversized image is downscaled/re-encoded at install (pre-format)
    and the install says so, instead of the skill being rejected."""
    from PIL import Image

    jpeg = _noise_image_bytes(2000, 1500, "JPEG", quality=95)
    assert len(jpeg) > 1_048_576

    candidate = prepare_upload(_skill_zip_with_asset("assets/photo.jpg", jpeg), "skill.zip")[0]

    installed = candidate.files["assets/photo.jpg"]
    assert len(installed) < len(jpeg)
    with Image.open(io.BytesIO(installed)) as image:
        assert max(image.size) <= 1024
    assert any("pre-formatted" in warning for warning in candidate.warnings)


def test_small_image_installs_byte_for_byte():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), color="red").save(buffer, format="PNG")
    small = buffer.getvalue()

    candidate = prepare_upload(_skill_zip_with_asset("assets/icon.png", small), "skill.zip")[0]

    assert candidate.files["assets/icon.png"] == small
    assert not any("pre-formatted" in warning for warning in candidate.warnings)


def test_non_image_binary_is_still_rejected():
    with pytest.raises(SkillValidationError, match="Binary content"):
        prepare_upload(_skill_zip_with_asset("assets/tool.bin", b"\x00\x01\x02" * 100), "skill.zip")


def test_skill_total_cap_still_enforced_without_per_file_cap():
    import os

    with pytest.raises(SkillValidationError, match="byte total limit"):
        prepare_upload(_skill_zip_with_asset("notes.txt", os.urandom(5 * 1024 * 1024)), "skill.zip")


def test_repository_zip_discovers_multiple_nested_skill_bundles_and_standalone_case_variant():
    one = SKILL.replace("daily-notes", "bundle-one")
    two = SKILL.replace("daily-notes", "bundle-two")
    standalone = SKILL.replace("daily-notes", "standalone")
    bundle_one = _zip_bytes({"SKILL.md": one, "guide.txt": "one guide"})
    bundle_two = _zip_bytes({"bundle/SKILL.md": two, "bundle/guide.txt": "two guide"})
    repository = _zip_bytes({
        "bundles/one.zip": bundle_one,
        "bundles/two.ZIP": bundle_two,
        "skills/skill.md": standalone,
        "skills/reference.txt": "standalone guide",
    })

    candidates = prepare_upload(repository, "repository.zip")
    by_name = {candidate.name: candidate for candidate in candidates}

    assert set(by_name) == {"bundle-one", "bundle-two", "standalone"}
    assert by_name["bundle-one"].files["guide.txt"] == b"one guide"
    assert by_name["bundle-two"].files["guide.txt"] == b"two guide"
    assert by_name["standalone"].files["reference.txt"] == b"standalone guide"


def test_nested_archives_reject_excess_depth_and_validate_inner_paths():
    leaf = _zip_bytes({"SKILL.md": SKILL})
    level_two = _zip_bytes({"three.zip": leaf})
    level_one = _zip_bytes({"two.zip": level_two})
    repository = _zip_bytes({"one.zip": level_one})
    with pytest.raises(SkillValidationError, match="depth"):
        prepare_upload(repository, "too-deep.zip")

    unsafe_inner = _zip_bytes({"../SKILL.md": SKILL})
    repository = _zip_bytes({"bundle.zip": unsafe_inner})
    with pytest.raises(SkillValidationError, match="Unsafe"):
        prepare_upload(repository, "traversal.zip")


def test_nested_archive_duplicate_skill_names_fail():
    duplicate = SKILL.replace("daily-notes", "same-name")
    first = _zip_bytes({"SKILL.md": duplicate})
    second = _zip_bytes({"SKILL.md": duplicate})
    repository = _zip_bytes({"a.zip": first, "b.zip": second})
    with pytest.raises(SkillValidationError, match="same skill name"):
        prepare_upload(repository, "duplicates.zip")
