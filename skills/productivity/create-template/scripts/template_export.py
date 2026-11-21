#!/usr/bin/env python3
"""Template draft + export helper for the create-template skill.

Hermes has no Control UI, so this script is the packaging half the external agent's
Control UI owns: it mints a private draft under the active profile home,
validates it against the draft schema, and bundles it into a single
shareable ``.hermes-template`` file. A recipient imports that file back into
a draft under their own home.

Drafts live at ``<home>/template-drafts/<draft-id>.json`` where ``<home>``
is ``--home``, else ``$HERMES_HOME``, else ``~/.hermes``. A draft id is a
UUID v4: one id per request, never a shared pointer, so overlapping
Template creations stay isolated.

Validation ports the external agent's review gate (``src/templates/draft.ts``:
``parseProposal`` + ``assertPortableContent``) and its draft schema
(``packages/gateway-protocol/src/schema/template-drafts.ts``): exact
semver, lowercase slug name, all four scope fields non-blank, closed
shape, size caps, safe relative paths, no private path segments, no
duplicate files, no credential-like content. The credential check here
is the draft-gate subset of the redaction rules — well-known token
prefixes, ``KEY = value`` assignments, and PEM blocks — kept stdlib-only
like every other skill script; the full Hermes redactor is
``agent/redact.py`` and runs at log/egress time. Error messages name the
file or field, never the matched secret text.

Subcommands: ``new``, ``validate``, ``export``, ``import``. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

# --- Limits ported from the external agent -------------------------------------------
MAX_DRAFT_BYTES = 1_000_000
MAX_TEXT = 64_000
MAX_MANIFEST = 256_000
MAX_FILE_CONTENT = 256_000
MAX_FILES = 64

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DRAFT_ID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$"
)
# Exact semver, ported verbatim from the external agent's src/templates/schema-portability.ts.
EXACT_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
WINDOWS_INVALID_PATH_CHARS = re.compile(r'[<>:"|?*]')
WINDOWS_RESERVED_SEGMENT = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)

# Path segments that must never appear in a draft file: private or
# home-owned material. Ported from the external agent's assertPortableContent, with
# ``.hermes`` as the Hermes-home analogue of ``.openclaw``.
PRIVATE_SEGMENT_RE = re.compile(
    r"^(?:\.env(?:\..*)?|\.git|\.openclaw|\.hermes|memory(?:\.md)?|user\.md"
    r"|sessions?|transcripts?|credentials?|auth-profiles\.json|secrets?)$",
    re.IGNORECASE,
)

# Credential-like content: the draft-gate subset. Well-known token prefixes
# and PEM blocks are matched anywhere; assignment shapes need a key that
# names a credential and a non-placeholder value (see _is_placeholder).
_TOKEN_PREFIX_RES = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"ghu_[A-Za-z0-9]{20,}"),
    re.compile(r"ghs_[A-Za-z0-9]{20,}"),
    re.compile(r"ghr_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|auth[_-]?token|client[_-]?secret|app[_-]?secret|private[_-]?key"
    r"|secret[_-]?key|key[_-]?material|password|passwd|passphrase|credential"
    r"|authorization|secret|token)\s*[:=]\s*"
)
_BEARER_RE = re.compile(r"(?i)authorization\s*:\s*Bearer\s+(\S+)")
_PLACEHOLDER_RE = re.compile(
    r"^(?:<[^>]*>|[A-Z][A-Z0-9_]{3,}|your[-_ ].*|example[-_ ].*"
    r"|changeme.*|replace[-_ ]?me.*|xxx+.*|test[-_ ]?value)$",
    re.IGNORECASE,
)

FORMAT = "hermes-template"
FORMAT_VERSION = 1
ENVELOPE_SUFFIX = ".hermes-template"
DRAFTS_DIRNAME = "template-drafts"


class TemplateError(Exception):
    """User-facing failure; ``code`` mirrors the external agent's TemplateProjectError codes."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


# --- Home / draft paths ------------------------------------------------------

def resolve_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def drafts_dir(home: Path) -> Path:
    return home / DRAFTS_DIRNAME


def draft_path(home: Path, draft_id: str) -> Path:
    return drafts_dir(home) / f"{draft_id}.json"


def resolve_draft_ref(ref: str, home: Path) -> Path:
    """A bare draft id resolves under the home; anything else is a path."""
    if DRAFT_ID_RE.match(ref):
        return draft_path(home, ref)
    path = Path(ref).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


# --- Draft schema (ported from the external agent's TemplateDraftDocumentSchema) ------

def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip().strip("\"'")))


def _looks_credential_like(text: str) -> bool:
    for pattern in _TOKEN_PREFIX_RES:
        if pattern.search(text):
            return True
    for match in _SECRET_KEY_RE.finditer(text):
        rest = text[match.end():]
        value = rest.split(None, 1)[0] if rest.split(None, 1) else ""
        value = value.strip("\"'`")
        if len(value) >= 8 and not _is_placeholder(value):
            return True
    bearer = _BEARER_RE.search(text)
    if bearer and len(bearer.group(1)) >= 8 and not _is_placeholder(bearer.group(1)):
        return True
    return False


def is_safe_template_relative_path(value: str) -> bool:
    """Ported verbatim from the external agent's isSafeTemplateRelativePath."""
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    return all(
        segment != ""
        and segment != "."
        and segment != ".."
        and not WINDOWS_INVALID_PATH_CHARS.search(segment)
        and not any(ord(ch) <= 0x1F for ch in segment)
        and not segment.endswith(".")
        and not segment.endswith(" ")
        and not WINDOWS_RESERVED_SEGMENT.match(segment)
        for segment in normalized.split("/")
    )


def _path_key(value: str) -> str:
    """Case-insensitive NFC key, mirroring portableTemplatePathKey."""
    import unicodedata

    return unicodedata.normalize("NFC", value.replace("\\", "/")).lower()


def _check_portable_file(path: str, content: str) -> None:
    if "\\" in path or not is_safe_template_relative_path(path):
        raise TemplateError(
            "private_draft_file", f"Remove private or unsafe file {path!r} from the proposal.")
    segments = _path_key(path).split("/")
    if any(PRIVATE_SEGMENT_RE.match(part) for part in segments):
        raise TemplateError(
            "private_draft_file", f"Remove private or unsafe file {path!r} from the proposal.")
    if _path_key(path).endswith(".jsonl"):
        raise TemplateError(
            "private_draft_file", f"Remove private or unsafe file {path!r} from the proposal.")
    if "\0" in content:
        raise TemplateError(
            "sensitive_draft_content",
            f"Remove credential-like content from {path!r} before reviewing.")
    if _looks_credential_like(content):
        raise TemplateError(
            "sensitive_draft_content",
            f"Remove credential-like content from {path!r} before reviewing.")


def _check_manifest(manifest: str) -> None:
    """Structural check on the manifest: YAML frontmatter with schemaVersion
    1 and an agent id, then a non-empty body. (Stdlib has no YAML parser;
    the full manifest parse is the template builder's job, not the draft
    gate's — the same split the external agent makes between parseProposal and the
    project builder.)"""
    if not manifest.startswith("---"):
        raise TemplateError(
            "invalid_draft", "The draft manifest must start with YAML frontmatter.")
    end = manifest.find("\n---", 3)
    if end == -1:
        raise TemplateError("invalid_draft", "The draft manifest frontmatter is not closed.")
    frontmatter = manifest[3:end]
    if not re.search(r"(?m)^schemaVersion:\s*1\s*$", frontmatter):
        raise TemplateError(
            "invalid_draft", "The draft manifest must declare schemaVersion: 1.")
    if not re.search(r"(?m)^agent:\s*$", frontmatter):
        raise TemplateError(
            "invalid_draft", "The draft manifest must declare an agent: block.")
    if not re.search(r"(?m)^\s+id:\s*\S+\s*$", frontmatter):
        raise TemplateError(
            "invalid_draft", "The draft manifest agent: block must declare an id:.")
    if not manifest[end + 4:].strip():
        raise TemplateError("invalid_draft", "The draft manifest body must not be empty.")


_DRAFT_KEYS = {"name", "description", "version", "scope", "manifest", "files"}
_SCOPE_KEYS = {"input", "job", "output", "authority"}
_FILE_KEYS = {"path", "content"}


def _validate_files(files: object) -> list:
    """Shape, duplicate, and portability checks for the draft file list."""
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise TemplateError("invalid_draft", "The draft files must be a list.")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _FILE_KEYS:
            raise TemplateError(
                "invalid_draft", "Each draft file must have exactly path and content.")
        path, content = entry["path"], entry["content"]
        if not isinstance(path, str) or not (1 <= len(path) <= 512):
            raise TemplateError("invalid_draft", "Each draft file needs a path.")
        if not isinstance(content, str) or len(content) > MAX_FILE_CONTENT:
            raise TemplateError("invalid_draft", "Each draft file needs text content.")
        key = _path_key(path)
        if key in seen:
            raise TemplateError("duplicate_draft_file", f"Duplicate draft file {path!r}.")
        seen.add(key)
        _check_portable_file(path, content)
    return files


def _check_draft_texts(draft: dict) -> None:
    """Credential-like sweep over the manifest, description, and scope."""
    if _looks_credential_like(draft["manifest"]):
        raise TemplateError(
            "sensitive_draft_content",
            "Remove credential-like content from the manifest before reviewing.")
    if _looks_credential_like(draft.get("description") or ""):
        raise TemplateError(
            "sensitive_draft_content",
            "Remove credential-like content from the description before reviewing.")
    for value in draft["scope"].values():
        if _looks_credential_like(value):
            raise TemplateError(
                "sensitive_draft_content",
                "Remove credential-like content from the scope before reviewing.")


def validate_draft(draft: object) -> dict:
    """Validate a parsed draft document; returns it typed as a dict."""
    if not isinstance(draft, dict):
        raise TemplateError("invalid_draft", "The draft must be a JSON object.")
    unknown = set(draft) - _DRAFT_KEYS
    if unknown:
        raise TemplateError(
            "invalid_draft", f"The draft has unknown fields: {sorted(unknown)}.")
    name = draft.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise TemplateError(
            "invalid_draft",
            "The draft name must be a lowercase slug starting with a letter.")
    description = draft.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str) or len(description) > MAX_TEXT:
        raise TemplateError("invalid_draft", "The draft description must be text.")
    version = draft.get("version")
    if not isinstance(version, str) or not (1 <= len(version) <= 128):
        raise TemplateError("invalid_draft", "The draft must contain a semantic version.")
    if not EXACT_SEMVER_RE.match(version):
        raise TemplateError(
            "invalid_draft", "The draft version must be exact semver (e.g. 0.1.0).")
    scope = draft.get("scope")
    if not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
        raise TemplateError(
            "invalid_draft", "The draft must contain a scope with input, job, output, authority.")
    for key, value in scope.items():
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise TemplateError("invalid_draft", f"The draft scope field {key!r} must be text.")
    manifest = draft.get("manifest")
    if not isinstance(manifest, str) or len(manifest) > MAX_MANIFEST:
        raise TemplateError("invalid_draft", "The draft must contain a CLAW.md manifest.")
    _validate_files(draft.get("files"))
    if any(not value.strip() for value in scope.values()):
        raise TemplateError(
            "incomplete_draft",
            "Describe the input, job, output, and authority boundary before reviewing.")
    _check_manifest(manifest)
    _check_draft_texts(draft)
    return draft


def load_draft(path: Path) -> dict:
    if path.is_symlink():
        raise TemplateError("invalid_draft", "The draft must not be a symlink.")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise TemplateError("draft_not_found", f"No draft at {path}.")
    except OSError as exc:
        raise TemplateError("invalid_draft", f"Cannot read the draft at {path}: {exc}.")
    if len(raw) > MAX_DRAFT_BYTES:
        raise TemplateError("invalid_draft", "The draft exceeds 1,000,000 bytes.")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TemplateError(
            "invalid_draft", "The draft must be valid JSON. Save or finish the agent's draft before reviewing.")
    return validate_draft(parsed)


def skeleton_draft(name: str, description: str, version: str) -> dict:
    return {
        "name": name,
        "description": description,
        "version": version,
        "scope": {"input": "", "job": "", "output": "", "authority": ""},
        "manifest": (
            "---\nschemaVersion: 1\nagent:\n"
            f"  id: {name}\n"
            f"  name: {name.replace('-', ' ').title()}\n"
            "  description: \n---\n"
            "Portable instructions for a new owner.\n"
        ),
        "files": [],
    }


def _write_new(path: Path, payload: dict) -> None:
    """Exclusive create, mode 0600 — the draft must never be adopted over an
    existing file, mirroring the external agent's createTemplateDraft overwrite: false."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    text = f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise TemplateError("draft_exists", f"A draft already exists at {path}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


# --- Subcommands -------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    draft_id = args.draft_id or str(uuid.uuid4())
    if not DRAFT_ID_RE.match(draft_id):
        raise TemplateError("invalid_draft_id", "Invalid Template draft id.")
    if not NAME_RE.match(args.name):
        raise TemplateError(
            "invalid_draft", "The template name must be a lowercase slug starting with a letter.")
    if not EXACT_SEMVER_RE.match(args.version):
        raise TemplateError(
            "invalid_draft", "The template version must be exact semver (e.g. 0.1.0).")
    path = draft_path(home, draft_id)
    _write_new(path, skeleton_draft(args.name, args.description, args.version))
    print(json.dumps({"draftId": draft_id, "path": str(path)}, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    path = resolve_draft_ref(args.draft, home)
    draft = load_draft(path)
    print(json.dumps({
        "ok": True,
        "path": str(path),
        "name": draft["name"],
        "version": draft["version"],
        "files": len(draft["files"]),
    }, ensure_ascii=False))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    path = resolve_draft_ref(args.draft, home)
    draft = load_draft(path)
    envelope = {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "exportedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "draft": draft,
    }
    out = Path(args.out).expanduser() if args.out else (
        drafts_dir(home) / f"{draft['name']}-{draft['version']}{ENVELOPE_SUFFIX}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{json.dumps(envelope, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
    os.chmod(out, 0o600)
    print(json.dumps({
        "path": str(out),
        "name": draft["name"],
        "version": draft["version"],
        "files": len(draft["files"]),
    }, ensure_ascii=False))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    source = Path(args.file).expanduser()
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        raise TemplateError("invalid_export", f"No export file at {source}.")
    except OSError as exc:
        raise TemplateError("invalid_export", f"Cannot read the export file: {exc}.")
    if len(raw) > MAX_DRAFT_BYTES + 4096:
        raise TemplateError("invalid_export", "The export file is too large.")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TemplateError("invalid_export", "The export file must be valid JSON.")
    if not isinstance(envelope, dict):
        raise TemplateError("invalid_export", "The export file must be a JSON object.")
    if envelope.get("format") != FORMAT or envelope.get("formatVersion") != FORMAT_VERSION:
        raise TemplateError(
            "invalid_export", "The file is not a hermes-template export (format 1).")
    draft = validate_draft(envelope.get("draft"))
    draft_id = args.draft_id or str(uuid.uuid4())
    if not DRAFT_ID_RE.match(draft_id):
        raise TemplateError("invalid_draft_id", "Invalid Template draft id.")
    path = draft_path(home, draft_id)
    _write_new(path, draft)
    print(json.dumps({
        "draftId": draft_id,
        "path": str(path),
        "name": draft["name"],
        "version": draft["version"],
    }, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="template_export.py",
        description="Mint, validate, export, and import shareable agent templates.")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Mint an empty draft under the home.")
    new.add_argument("--name", required=True, help="Lowercase template slug.")
    new.add_argument("--description", default="", help="What another person can do with it.")
    new.add_argument("--version", default="0.1.0", help="Exact semver.")
    new.add_argument("--draft-id", default=None, help="UUID v4; minted when omitted.")
    new.add_argument("--home", default=None, help="Profile home (default: $HERMES_HOME or ~/.hermes).")
    new.set_defaults(func=cmd_new)

    validate = sub.add_parser("validate", help="Validate a draft against the schema and safety gate.")
    validate.add_argument("draft", help="Draft id or path to the draft JSON.")
    validate.add_argument("--home", default=None)
    validate.set_defaults(func=cmd_validate)

    export = sub.add_parser("export", help="Bundle a draft into one shareable file.")
    export.add_argument("draft", help="Draft id or path to the draft JSON.")
    export.add_argument("--out", default=None, help="Export path (default: <home>/template-drafts/<name>-<version>.hermes-template).")
    export.add_argument("--home", default=None)
    export.set_defaults(func=cmd_export)

    import_cmd = sub.add_parser("import", help="Import an exported file back into a draft.")
    import_cmd.add_argument("file", help="Path to the .hermes-template file.")
    import_cmd.add_argument("--draft-id", default=None, help="UUID v4; minted when omitted.")
    import_cmd.add_argument("--home", default=None)
    import_cmd.set_defaults(func=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TemplateError as exc:
        eprint(f"error: {exc.code}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
