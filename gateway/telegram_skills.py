"""Safe preparation and private storage for Telegram users' instructional skills.

Nothing in a skill is executed here. Inputs are size/shape constrained, validated,
and passed through Hermes' existing skill security scanner before use.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml

MAX_SKILL_TOTAL_BYTES = 4 * 1_048_576
MAX_TOTAL_BYTES = 25 * 1_048_576
MAX_FILES = 64
MAX_ARCHIVE_BYTES = 25 * 1_048_576
MAX_UPLOAD_BYTES = MAX_ARCHIVE_BYTES
MAX_DOWNLOAD_BYTES = MAX_ARCHIVE_BYTES
MAX_DOWNLOADS = 64
MAX_ARCHIVE_ENTRIES = MAX_FILES * 16
MAX_NESTED_ARCHIVE_DEPTH = 2
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_FILE = re.compile(r"(?i)^\.env(?:$|[._-].*)$")
_CREDENTIAL = re.compile(r"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,}))")


class SkillValidationError(ValueError):
    """Input cannot safely be installed as a personal skill."""


@dataclass
class SkillCandidate:
    name: str
    description: str
    files: dict[str, bytes]
    warnings: list[str] = field(default_factory=list)
    dangerous: bool = False
    environment_files: dict[str, bytes] = field(default_factory=dict)
    environment_files_included: bool = False

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for rel, data in sorted(self.files.items()):
            h.update(b"file\0" + rel.encode("utf-8") + b"\0" + data)
        for rel, data in sorted(self.environment_files.items()):
            h.update(b"pending-env\0" + rel.encode("utf-8") + b"\0" + data)
        h.update(b"env-included\0" + (b"1" if self.environment_files_included else b"0"))
        return h.hexdigest()


def _relative_path(value: str, *, allow_hidden: bool = False) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise SkillValidationError(f"Invalid file path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts) or ":" in p.parts[0]:
        raise SkillValidationError(f"Unsafe file path: {value!r}")
    if not allow_hidden and any(part.startswith(".") for part in p.parts):
        raise SkillValidationError(f"Hidden files are not allowed: {value!r}")
    return p.as_posix()


def _is_credential_file(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return (name.endswith((".pem", ".key")) or name in {".env", "credentials.json", "secrets.json"}
            or name.startswith(".env."))


def _is_environment_file(path: str) -> bool:
    """Recognize dotenv variants by basename, including .env.sample/test/template."""
    return bool(_ENV_FILE.fullmatch(PurePosixPath(path).name))


def _validate_environment_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """Validate staged dotenv files without treating their secrets as accidental leaks."""
    normalized: dict[str, bytes] = {}
    total = 0
    for raw, data in files.items():
        path = _relative_path(raw, allow_hidden=True)
        if not _is_environment_file(path):
            raise SkillValidationError(f"Not an environment file: {path}")
        if path in normalized:
            raise SkillValidationError(f"Duplicate file path: {path}")
        if not isinstance(data, bytes):
            raise SkillValidationError(f"File {path} is not bytes")
        total += len(data)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise SkillValidationError(f"Environment files exceed {MAX_SKILL_TOTAL_BYTES} byte total limit")
        if b"\x00" in data:
            raise SkillValidationError(f"Binary content is not allowed: {path}")
        normalized[path] = data
    return normalized


# Image assets a skill may carry (plugin icons and the like). Pre-formatting
# shrinks only oversized ones; small images install byte-for-byte.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"}
_PREFORMAT_MAX_DIMENSION = 1024
_PREFORMAT_MAX_BYTES = 256 * 1024


def _preformat_image(path: str, data: bytes) -> tuple[bytes, str | None]:
    """Downscale/re-encode an oversized skill image; returns (data, note).

    Only images larger than 1024 px on a side or 256 KiB are touched; the
    rest pass through unchanged. Animated GIFs are left alone (resizing
    would drop frames). Never raises: an unreadable image, a missing Pillow,
    or a re-encode that would not shrink the file installs as-is — size
    alone never rejects a skill file.
    """
    if PurePosixPath(path).suffix.casefold() not in _IMAGE_SUFFIXES:
        return data, None
    try:
        from PIL import Image
    except ImportError:
        return data, None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return data, None
    width, height = image.size
    if getattr(image, "is_animated", False):
        return data, None
    if max(width, height) <= _PREFORMAT_MAX_DIMENSION and len(data) <= _PREFORMAT_MAX_BYTES:
        return data, None
    try:
        image.thumbnail((_PREFORMAT_MAX_DIMENSION, _PREFORMAT_MAX_DIMENSION), Image.LANCZOS)
        fmt = (image.format or PurePosixPath(path).suffix.lstrip(".")).upper()
        if fmt == "JPG":
            fmt = "JPEG"
        if fmt not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
            fmt = "PNG"
        if fmt == "JPEG" and image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGB")
        out = io.BytesIO()
        save_kwargs: dict = {"format": fmt}
        if fmt == "PNG":
            save_kwargs["optimize"] = True
        elif fmt in {"JPEG", "WEBP"}:
            save_kwargs["quality"] = 85
        image.save(out, **save_kwargs)
        shrunk = out.getvalue()
    except Exception:
        return data, None
    if len(shrunk) >= len(data):
        return data, None
    new_width, new_height = image.size
    note = (
        f"Image {path} pre-formatted to {new_width}x{new_height} "
        f"({len(shrunk)} bytes, was {len(data)})"
    )
    return shrunk, note


def _validate_files(files: dict[str, bytes], *, allow_environment_files: bool = False) -> tuple[dict[str, bytes], list[str]]:
    """Validate a skill's files; returns (files, pre-format notes).

    There is no per-file size cap: a single large asset (a plugin icon PNG
    over 1 MiB) installs, bounded instead by the per-skill total. Image
    assets are pre-formatted (downscaled/re-encoded when oversized) and skip
    the binary/credential text scans, which are meaningless on image bytes;
    every other binary is still rejected.
    """
    if not files or len(files) > MAX_FILES:
        raise SkillValidationError(f"Skill must contain 1–{MAX_FILES} files")
    normalized: dict[str, bytes] = {}
    preformat_notes: list[str] = []
    total = 0
    for raw, data in files.items():
        is_env = _is_environment_file(raw)
        if is_env and not allow_environment_files:
            raise SkillValidationError(f"Environment file requires explicit confirmation: {raw}")
        path = _relative_path(raw, allow_hidden=is_env and allow_environment_files)
        if path in normalized:
            raise SkillValidationError(f"Duplicate file path: {path}")
        if not isinstance(data, bytes):
            raise SkillValidationError(f"File {path} is not bytes")
        is_image = PurePosixPath(path).suffix.casefold() in _IMAGE_SUFFIXES
        if is_image:
            data, note = _preformat_image(path, data)
            if note:
                preformat_notes.append(note)
        total += len(data)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise SkillValidationError(f"Skill exceeds {MAX_SKILL_TOTAL_BYTES} byte total limit")
        if _is_credential_file(path) and not (is_env and allow_environment_files):
            raise SkillValidationError(f"Credential file is not allowed: {path}")
        if not is_image:
            if b"\x00" in data:
                raise SkillValidationError(f"Binary content is not allowed: {path}")
            text = data.decode("utf-8", errors="replace")
            if _CREDENTIAL.search(text) and not (is_env and allow_environment_files):
                raise SkillValidationError(f"Possible credential found in {path}")
        normalized[path] = data
    if "SKILL.md" not in normalized:
        raise SkillValidationError("Missing SKILL.md")
    return normalized, preformat_notes


def _candidate(
    files: dict[str, bytes], warnings: list[str] | None = None, *,
    environment_files: dict[str, bytes] | None = None, allow_environment_files: bool = False,
) -> SkillCandidate:
    environment_files = _validate_environment_files(environment_files or {})
    if allow_environment_files and environment_files:
        if set(files).intersection(environment_files):
            raise SkillValidationError("Duplicate file path between skill and environment files")
        files = {**files, **environment_files}
        environment_files = {}
    files, preformat_notes = _validate_files(files, allow_environment_files=allow_environment_files)
    warnings = list(warnings or []) + preformat_notes
    if len(files) + len(environment_files) > MAX_FILES:
        raise SkillValidationError(f"Skill must contain 1–{MAX_FILES} files")
    if sum(len(data) for data in files.values()) + sum(len(data) for data in environment_files.values()) > MAX_SKILL_TOTAL_BYTES:
        raise SkillValidationError(f"Skill exceeds {MAX_SKILL_TOTAL_BYTES} byte total limit")
    try:
        text = files["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError("SKILL.md must be UTF-8 text") from exc
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise SkillValidationError("SKILL.md must begin with YAML frontmatter (---)")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z", text, re.DOTALL)
    if not match:
        raise SkillValidationError("SKILL.md has missing or unterminated YAML frontmatter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillValidationError(f"Invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillValidationError("YAML frontmatter must be a mapping")
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or len(name) > 64 or not _SLUG.fullmatch(name):
        raise SkillValidationError("Frontmatter 'name' must be a lowercase hyphenated slug of at most 64 characters")
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError("Frontmatter 'description' must be nonempty text")
    if len(description) > 1000:
        raise SkillValidationError("Frontmatter description exceeds 1000 characters")
    if not match.group(2).strip():
        raise SkillValidationError("SKILL.md instructions must be nonempty")
    candidate_warnings = list(warnings or [])
    if environment_files:
        candidate_warnings.append("Environment files are present and require your explicit choice before inclusion.")
    candidate = SkillCandidate(
        name, description.strip(), files, candidate_warnings, False, environment_files,
        allow_environment_files)
    # Run scanner in a temporary, isolated directory; scanner warnings are advisory.
    try:
        from tools.skills_guard import scan_skill
        with tempfile.TemporaryDirectory(prefix="hermes-telegram-skill-") as tmp:
            root = Path(tmp) / name
            root.mkdir()
            for rel, data in files.items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            scan = scan_skill(root, source="community")
            # Scanner verdicts are deterministic review signals, not an install
            # authorization decision for a private, owner-confirmed skill.
            candidate.dangerous = getattr(scan, "verdict", "") == "dangerous"
            if candidate.dangerous:
                summary = str(getattr(scan, "summary", "Scanner marked this skill dangerous"))[:300]
                candidate.warnings.append(
                    "DANGEROUS SECURITY SCAN VERDICT — owner review required: " + summary
                )
            if getattr(scan, "findings", None):
                candidate.warnings.append(str(scan.summary)[:300])
                for finding in scan.findings[:4]:
                    candidate.warnings.append(str(getattr(finding, "description", finding))[:200])
    except SkillValidationError:
        raise
    except Exception as exc:
        # Fail closed if the security scanner is unavailable or fails unexpectedly.
        raise SkillValidationError(f"Security scan failed: {exc}") from exc
    candidate.warnings = sorted(set(candidate.warnings), key=lambda warning: (warning.casefold(), warning))
    return candidate


def include_environment_files(candidate: SkillCandidate) -> SkillCandidate:
    """Promote staged dotenv files only after the owning user explicitly chooses Include."""
    if not candidate.environment_files:
        return candidate
    warnings = [warning for warning in candidate.warnings
                if not warning.startswith("Environment files are present and require")]
    return _candidate(
        candidate.files, warnings, environment_files=candidate.environment_files,
        allow_environment_files=True,
    )


def exclude_environment_files(candidate: SkillCandidate) -> SkillCandidate:
    """Discard staged dotenv files after an explicit Exclude choice."""
    if not candidate.environment_files:
        return candidate
    warnings = [warning for warning in candidate.warnings
                if not warning.startswith("Environment files are present and require")]
    return _candidate(candidate.files, warnings)


def prepare_skill(text: str) -> SkillCandidate:
    """Prepare a single complete SKILL.md document."""
    if not isinstance(text, str):
        raise SkillValidationError("Skill content must be text")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SkillValidationError("Skill content is not valid UTF-8") from exc
    return _candidate({"SKILL.md": data})


def _safe_zip_entries(data: bytes) -> dict[str, bytes]:
    """Expand bounded ZIP bundles, sharing entry and byte budgets recursively."""
    budget = {"entries": 0, "expanded": 0}

    def unpack(archive_data: bytes, prefix: str = "", depth: int = 0) -> dict[str, bytes]:
        if len(archive_data) > MAX_ARCHIVE_BYTES:
            raise SkillValidationError(f"ZIP exceeds {MAX_ARCHIVE_BYTES} byte limit")
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_data))
        except (zipfile.BadZipFile, OSError) as exc:
            raise SkillValidationError("Upload is not a valid ZIP archive") from exc
        extracted: dict[str, bytes] = {}
        seen: set[str] = set()
        for info in archive.infolist():
            budget["entries"] += 1
            if budget["entries"] > MAX_ARCHIVE_ENTRIES:
                raise SkillValidationError("ZIP bundle tree contains too many entries")
            raw = info.filename.rstrip("/")
            if not raw:
                continue
            rel = _relative_path(raw, allow_hidden=True)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SkillValidationError(f"ZIP symlinks are not allowed: {rel}")
            if info.is_dir():
                continue
            is_environment_file = _is_environment_file(rel)
            if _is_credential_file(rel) and not is_environment_file:
                raise SkillValidationError(f"Credential file is not allowed: {rel}")
            if rel in seen:
                raise SkillValidationError(f"Duplicate ZIP path: {rel}")
            seen.add(rel)
            is_bundle = PurePosixPath(rel).suffix.casefold() == ".zip"
            if is_bundle and info.file_size > MAX_ARCHIVE_BYTES:
                raise SkillValidationError(f"ZIP file {rel} exceeds {MAX_ARCHIVE_BYTES} byte limit")
            budget["expanded"] += info.file_size
            if budget["expanded"] > MAX_TOTAL_BYTES:
                raise SkillValidationError("ZIP bundle tree expands beyond total size limit")
            if (info.file_size > 0 and info.compress_size == 0) or (
                info.compress_size and info.file_size / info.compress_size > 200
            ):
                raise SkillValidationError(f"Suspicious compression ratio in {rel}")

            # Repository metadata is not skill content, but still consumes the
            # shared budget and is checked for traversal and symlinks above.
            if (not is_environment_file
                    and any(part.startswith(".") for part in PurePosixPath(rel).parts)):
                continue
            body = archive.read(info)
            output_path = _relative_path(f"{prefix}/{rel}" if prefix else rel, allow_hidden=True)
            if is_bundle:
                if depth >= MAX_NESTED_ARCHIVE_DEPTH:
                    raise SkillValidationError(
                        f"Nested ZIP depth exceeds the limit of {MAX_NESTED_ARCHIVE_DEPTH}"
                    )
                bundle_prefix = str(PurePosixPath(output_path).with_suffix(""))
                for child_path, child_data in unpack(body, bundle_prefix, depth + 1).items():
                    if child_path in extracted:
                        raise SkillValidationError(f"Duplicate ZIP path: {child_path}")
                    extracted[child_path] = child_data
            else:
                if output_path in extracted:
                    raise SkillValidationError(f"Duplicate ZIP path: {output_path}")
                extracted[output_path] = body
        return extracted

    return unpack(data)



def _candidates_from_entries(extracted: dict[str, bytes], *, source: str = "Selected GitHub tree") -> list[SkillCandidate]:
    """Build independent skills, assigning companions to the nearest skill directory.

    Repositories commonly nest skills (or put many skills below one directory). A
    parent skill must not absorb a nested skill or its files, and a skill-only
    repository must not be rejected because unrelated repo files exceed per-skill
    limits. Sorting makes candidate order independent of ZIP member order.
    """
    # GitHub repositories sometimes spell the index as skill.md or Skill.md;
    # normalize that one canonical filename while rejecting case collisions.
    normalized_entries: dict[str, bytes] = {}
    for raw_path, body in extracted.items():
        entry_path = PurePosixPath(raw_path)
        normalized = (entry_path.parent / "SKILL.md").as_posix() if entry_path.name.casefold() == "skill.md" else raw_path
        if normalized in normalized_entries:
            raise SkillValidationError(f"Duplicate ZIP path: {normalized}")
        normalized_entries[normalized] = body
    extracted = normalized_entries
    skill_paths = sorted(
        (PurePosixPath(path) for path in extracted if PurePosixPath(path).name == "SKILL.md"),
        key=lambda path: (path.as_posix().casefold(), path.as_posix()),
    )
    if not skill_paths:
        raise SkillValidationError(f"{source} contains no SKILL.md")
    all_parts = [PurePosixPath(path).parts for path in extracted]
    common_root = (
        all_parts[0][0]
        if all_parts and all(len(parts) > 1 and parts[0] == all_parts[0][0] for parts in all_parts)
        else None
    )
    prefixes = [path.parts[:-1] for path in skill_paths]
    groups = [{"SKILL.md": extracted[path.as_posix()]} for path in skill_paths]
    environment_groups: list[dict[str, bytes]] = [{} for _ in skill_paths]
    unattached_environment_files: list[tuple[str, bytes]] = []
    for path, body in sorted(extracted.items(), key=lambda item: (item[0].casefold(), item[0])):
        entry = PurePosixPath(path)
        if entry.name == "SKILL.md":
            continue
        is_environment_file = _is_environment_file(path)
        owners = [i for i, prefix in enumerate(prefixes)
                  if len(entry.parts) > len(prefix) and entry.parts[:len(prefix)] == prefix]
        if not owners:
            if is_environment_file:
                if len(skill_paths) == 1:
                    owners = [0]
                else:
                    unattached_environment_files.append((path, body))
                    continue
            else:
                continue
        owner = max(owners, key=lambda i: (len(prefixes[i]), -i))
        relative = "/".join(entry.parts[len(prefixes[owner]):])
        if is_environment_file:
            environment_groups[owner][relative] = body
        else:
            groups[owner][relative] = body
    shared_warning = None
    if unattached_environment_files:
        # Package-level dotenv files have no safe implicit owner. Stage a copy for
        # every skill so an explicit Include choice remains possible and visible.
        for group in environment_groups:
            for path, body in unattached_environment_files:
                relative = PurePosixPath(path)
                if common_root and relative.parts[0] == common_root:
                    relative = PurePosixPath(*relative.parts[1:])
                group[relative.as_posix()] = body
        shared_warning = (
            "Package-level environment files have no single skill owner; choosing Include will copy them into "
            f"each of the {len(skill_paths)} skills: "
            + ", ".join(
                PurePosixPath(path).relative_to(common_root).as_posix()
                if common_root and path.startswith(common_root + "/") else path
                for path, _body in unattached_environment_files
            )
        )
    candidates = [
        _candidate(
            files,
            [shared_warning] if shared_warning else None,
            environment_files=environment_groups[index],
        )
        for index, files in enumerate(groups)
    ]
    names = [candidate.name for candidate in candidates]
    if len(names) != len(set(names)):
        raise SkillValidationError("Multiple SKILL.md files declare the same skill name")
    return candidates


def _zip_candidate_from_entries(extracted: dict[str, bytes]) -> list[SkillCandidate]:
    return _candidates_from_entries(extracted)

def _zip_candidate(data: bytes) -> list[SkillCandidate]:
    extracted = _safe_zip_entries(data)
    return _candidates_from_entries(extracted, source="ZIP")


def prepare_upload(data: bytes, filename: str) -> list[SkillCandidate]:
    """Prepare a Markdown document or ZIP upload; always returns candidates list."""
    if not isinstance(data, bytes):
        raise SkillValidationError("Upload must be bytes")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SkillValidationError(f"Upload exceeds {MAX_ARCHIVE_BYTES} byte limit")
    if filename.lower().endswith(".zip"):
        return _zip_candidate(data)
    if filename.lower().endswith(".md"):
        try:
            return [prepare_skill(data.decode("utf-8"))]
        except UnicodeDecodeError as exc:
            raise SkillValidationError("Markdown upload must be UTF-8") from exc
    raise SkillValidationError("Upload must be a .md or .zip file")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SkillValidationError("GitHub download redirects are not allowed")


def _github_url(url: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "raw.githubusercontent.com", "codeload.github.com"}:
            raise ValueError
        if parsed.query or parsed.fragment:
            raise ValueError
        # Reject resolved IP literals and odd host encodings regardless of host allowlist.
        ipaddress.ip_address(host)
    except ValueError:
        # ip_address's expected ValueError means normal hostname; validate strict parsed parts.
        if 'parsed' not in locals() or parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or (parsed.hostname or "").lower() not in {"github.com", "raw.githubusercontent.com", "codeload.github.com"} or parsed.query or parsed.fragment:
            raise SkillValidationError("Only public HTTPS GitHub repository, tree, blob, or raw URLs are supported")
    host = parsed.hostname.lower()
    path = urllib.parse.unquote(parsed.path)
    if ".." in path.split("/") or "\\" in path:
        raise SkillValidationError("Unsafe GitHub URL path")
    if host == "raw.githubusercontent.com":
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            raise SkillValidationError("Raw GitHub URL must identify owner/repository/branch/file")
        return "raw", url, ""
    if host == "codeload.github.com":
        return "zip", url, ""
    parts = path.strip("/").split("/")
    if len(parts) == 2 and all(parts):
        return "zip", f"https://codeload.github.com/{parts[0]}/{parts[1]}/zip/HEAD", ""
    if len(parts) >= 5 and parts[2] in {"blob", "tree"}:
        owner, repo, kind = parts[0], parts[1], parts[2]
        # GitHub refs may contain slashes; resolve the ref/path split by
        # treating the first segment as the ref (normal branches), preserving remaining skill path.
        ref = parts[3]
        rest = "/".join(parts[4:])
        if kind == "blob":
            return "raw", f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}", ""
        return "tree", f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}", rest
    raise SkillValidationError("GitHub URL must be a repository, tree, blob, or raw file URL")


def _download(url: str) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-Telegram-Skills/1.0", "Accept": "application/octet-stream"})
    try:
        with opener.open(request, timeout=10) as response:
            if response.geturl() != url:
                raise SkillValidationError("GitHub download changed URL unexpectedly")
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SkillValidationError(f"GitHub download failed: {exc}") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise SkillValidationError("GitHub download exceeds size limit")
    return data


def prepare_github(url: str) -> list[SkillCandidate]:
    """Download only from explicit GitHub HTTPS hosts and prepare bounded content."""
    kind, target, selected_path = _github_url(url)
    data = _download(target)
    if kind == "raw":
        if not target.endswith("/SKILL.md"):
            raise SkillValidationError("GitHub blob/raw URL must point to SKILL.md")
        return [prepare_skill(data.decode("utf-8"))]
    if kind == "zip":
        entries = _safe_zip_entries(data)
        paths = [PurePosixPath(path) for path in entries]
        if paths and len({p.parts[0] for p in paths if len(p.parts) > 1}) == 1:
            prefix = next(iter({p.parts[0] for p in paths if len(p.parts) > 1}))
            entries = {"/".join(PurePosixPath(path).parts[1:]): body for path, body in entries.items()
                       if len(PurePosixPath(path).parts) > 1 and PurePosixPath(path).parts[0] == prefix}
        return _zip_candidate_from_entries(entries)
    # A tree URL selects a skill subtree. First validate every archive entry for
    # traversal/symlinks, then ignore repository-level and unrelated files.
    entries = _safe_zip_entries(data)
    matched = {}
    for rel, body in entries.items():
        # Codeload wraps archive content in one repo-ref directory.
        parts = PurePosixPath(rel).parts
        relative = "/".join(parts[1:]) if len(parts) > 1 else ""
        if relative == selected_path or relative.startswith(selected_path.rstrip("/") + "/"):
            matched[relative] = body
    return _zip_candidate_from_entries(matched)


def personal_skills_root(home: Path, user_id: str) -> Path:
    """Per-user skill directory alongside this profile's Telegram settings."""
    from gateway.telegram_personal_settings import personal_settings_path
    profile = Path(home).expanduser().resolve()
    root = personal_settings_path(profile, user_id).parent / "skills"
    if root.is_symlink() or not root.resolve(strict=False).is_relative_to(profile):
        raise ValueError("Personal skills path escaped the active profile")
    return root


def _personal_skill_entries(home: Path, uid: str) -> list[tuple[SkillCandidate, Path]]:
    """Read valid private skills at any depth without following symlinks.

    Older personal-skill writers can place ``SKILL.md`` one or more directories
    below the library root. Keep those files discoverable without moving them.
    Nested skill directories are independent packages, not parent companions.
    """
    from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS

    root = personal_skills_root(home, uid)
    if root.is_symlink() or not root.is_dir():
        return []
    skill_dirs: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            name for name in dirs
            if name not in EXCLUDED_SKILL_DIRS
            and not (current_path / name).is_symlink()
            and not (current_path == root and name.startswith("."))
        )
        skill_md = current_path / "SKILL.md"
        if current_path != root and "SKILL.md" in files and not skill_md.is_symlink():
            skill_dirs.append(current_path)
            dirs[:] = [name for name in dirs if name not in SKILL_SUPPORT_DIRS]

    result: list[tuple[SkillCandidate, Path]] = []
    all_skill_dirs = set(skill_dirs)
    for skill_dir in skill_dirs:
        nested_skill_dirs = {
            path for path in all_skill_dirs
            if path != skill_dir and path.is_relative_to(skill_dir)
        }
        files: dict[str, bytes] = {}
        for current, dirs, filenames in os.walk(skill_dir, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name for name in dirs
                if name not in EXCLUDED_SKILL_DIRS
                and not (current_path / name).is_symlink()
                and current_path / name not in nested_skill_dirs
            )
            for filename in sorted(filenames):
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    files[path.relative_to(skill_dir).as_posix()] = path.read_bytes()
                except OSError:
                    continue
        try:
            has_environment_files = any(_is_environment_file(path) for path in files)
            result.append((_candidate(files, allow_environment_files=has_environment_files), skill_dir))
        except (OSError, SkillValidationError):
            continue
    return result


def install_skill(
    home: Path, uid: str, candidate: SkillCandidate, replace: bool = False, *,
    include_environment_files: bool = False,
) -> Path:
    """Install atomically beneath a private per-user directory."""
    # Reparse files and metadata at the trust boundary; dataclasses are mutable
    # and callers can forge a candidate after preparation.
    if candidate.environment_files:
        raise SkillValidationError("Environment files require an explicit Include or Exclude choice")
    has_environment_files = any(_is_environment_file(path) for path in candidate.files)
    if has_environment_files and not candidate.environment_files_included:
        raise SkillValidationError("Environment files require explicit user approval before installation")
    if has_environment_files and not include_environment_files:
        raise SkillValidationError("Environment files require explicit user approval before installation")
    validated = _candidate(candidate.files, allow_environment_files=include_environment_files)
    if not isinstance(candidate.name, str) or not _SLUG.fullmatch(candidate.name) or candidate.name != validated.name:
        raise SkillValidationError("Candidate name does not match its validated SKILL.md metadata")
    if candidate.description != validated.description:
        raise SkillValidationError("Candidate description does not match its validated SKILL.md metadata")
    root = personal_skills_root(home, uid)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("Personal skills path cannot use symlinks")
    os.chmod(root, 0o700)
    existing_paths = [path for existing, path in _personal_skill_entries(home, uid)
                      if existing.name == candidate.name]
    if len(existing_paths) > 1:
        raise SkillValidationError("Multiple private skills share this name")
    if existing_paths and not replace:
        raise FileExistsError(f"Personal skill {candidate.name!r} already exists")
    previous = existing_paths[0] if existing_paths else None
    dest = root / candidate.name
    if dest.is_symlink():
        raise ValueError("Personal skill destination cannot be a symlink")
    if dest.exists() and not replace:
        raise FileExistsError(f"Personal skill {candidate.name!r} already exists")
    if previous and previous != dest and any(
        path != previous and path.is_relative_to(previous)
        for _candidate_data, path in _personal_skill_entries(home, uid)
    ):
        raise ValueError("Remove nested private skills before replacing their parent")
    temp = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        for rel, data in validated.files.items():
            target = temp / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            os.chmod(target, 0o600)
        # Re-validate skill metadata at the installation trust boundary.
        installed = _candidate(
            {p.relative_to(temp).as_posix(): p.read_bytes() for p in temp.rglob("*") if p.is_file()},
            allow_environment_files=include_environment_files,
        )
        if installed.name != candidate.name:
            raise SkillValidationError("Candidate name does not match its validated SKILL.md metadata")
        if dest.exists():
            backup = root / (".old-" + candidate.name)
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(dest, backup)
            try:
                os.replace(temp, dest)
            except Exception:
                os.replace(backup, dest)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        else:
            os.replace(temp, dest)
        if previous and previous != dest:
            shutil.rmtree(previous, ignore_errors=True)
        return dest
    finally:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


def install_skills(
    home: Path, uid: str, candidates: Iterable[SkillCandidate], *,
    include_environment_files: bool = False,
) -> list[Path]:
    """Install a reviewed batch atomically without replacing existing skills."""
    candidates = list(candidates)
    if not candidates:
        raise SkillValidationError("No skills were selected")
    validated = []
    for candidate in candidates:
        if candidate.environment_files:
            raise SkillValidationError("Environment files require an explicit Include or Exclude choice")
        files, _preformat_notes = _validate_files(candidate.files, allow_environment_files=include_environment_files)
        checked = _candidate(files, allow_environment_files=include_environment_files)
        if (candidate.name != checked.name or candidate.description != checked.description
                or candidate.digest != checked.digest):
            raise SkillValidationError("A skill changed after batch review")
        validated.append(checked)
    names = [candidate.name for candidate in validated]
    if len(names) != len(set(names)):
        raise SkillValidationError("Batch contains duplicate skill names")

    root = personal_skills_root(home, uid)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("Personal skills path cannot use symlinks")
    os.chmod(root, 0o700)
    destinations = [root / name for name in names]
    existing_names = {candidate.name for candidate, _path in _personal_skill_entries(home, uid)}
    if existing_names.intersection(names) or any(path.exists() or path.is_symlink() for path in destinations):
        raise FileExistsError("A skill in this batch already exists; review it individually to replace it")

    staging = Path(tempfile.mkdtemp(prefix=".install-batch-", dir=root))
    installed: list[Path] = []
    try:
        for candidate in validated:
            staged = staging / candidate.name
            staged.mkdir(mode=0o700)
            for rel, data in candidate.files.items():
                target = staged / rel
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(data)
                os.chmod(target, 0o600)
            checked = _candidate(
                {p.relative_to(staged).as_posix(): p.read_bytes()
                 for p in staged.rglob("*") if p.is_file()},
                allow_environment_files=include_environment_files,
            )
            if checked.name != candidate.name:
                raise SkillValidationError("Candidate name does not match its validated SKILL.md metadata")
        for destination in destinations:
            os.replace(staging / destination.name, destination)
            installed.append(destination)
        return destinations
    except Exception:
        for path in installed:
            shutil.rmtree(path, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _list_personal_skill_candidates(home: Path, uid: str) -> list[SkillCandidate]:
    return [candidate for candidate, _path in _personal_skill_entries(home, uid)]


def list_personal_skills(home: Path, uid: str) -> list[dict[str, str]]:
    """UI-facing listing: only validated personal skills and display metadata."""
    return [{"name": candidate.name, "description": candidate.description}
            for candidate in _list_personal_skill_candidates(home, uid)]


def list_skills(home: Path, uid: str) -> list[dict[str, str]]:
    return list_personal_skills(home, uid)


def read_personal_skill(home: Path, uid: str, name: str) -> SkillCandidate:
    if not _SLUG.fullmatch(name):
        raise ValueError("Invalid skill name")
    matches = [candidate for candidate, _path in _personal_skill_entries(home, uid)
               if candidate.name == name]
    if not matches:
        raise FileNotFoundError(name)
    if len(matches) > 1:
        raise ValueError("Multiple private skills share this name")
    return matches[0]


def read_skill(home: Path, uid: str, name: str) -> str:
    """Return the full validated SKILL.md text for UI preview."""
    return read_personal_skill(home, uid, name).files["SKILL.md"].decode("utf-8")


def remove_personal_skill(home: Path, uid: str, name: str) -> bool:
    if not _SLUG.fullmatch(name):
        raise ValueError("Invalid skill name")
    entries = _personal_skill_entries(home, uid)
    matches = [path for candidate, path in entries if candidate.name == name]
    if len(matches) > 1:
        raise ValueError("Multiple private skills share this name")
    root = personal_skills_root(home, uid)
    target = matches[0] if matches else root / name
    if matches and any(path != target and path.is_relative_to(target) for _candidate, path in entries):
        raise ValueError("Remove nested private skills before removing their parent")
    if target.is_symlink():
        target.unlink()
        return True
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False


def remove_skill(home: Path, uid: str, name: str) -> bool:
    return remove_personal_skill(home, uid, name)


def export_personal_skill(home: Path, uid: str, name: str) -> bytes:
    candidate = read_personal_skill(home, uid, name)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, data in sorted(candidate.files.items()):
            archive.writestr(f"{candidate.name}/{rel}", data)
    return out.getvalue()


def export_skill(home: Path, uid: str, name: str) -> bytes:
    return export_personal_skill(home, uid, name)
