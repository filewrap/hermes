"""Skill market: snap-install whole skill repos into a profile, from /skills.

The /skills response carries a Market button. Tapping it opens a new message
rendering every market repo as a full entry — name, source URL, skill count,
total size — ten per page, with Cancel + Back below. Tapping a repo's plain
button installs the WHOLE repo DIRECTLY into that profile, right away — a
plain directory copy when its skill dirs are already on the host, or a sparse
fetch of the repo from its source URL when they are not — no staging, no
approval queue, no extra confirmation. The listing runs off the market.json
index (a host copy wins over the index shipped with this module), so no skill
files need to exist on the host upfront. An installed repo carries a small
green installed badge right after its name and a green button; tapping it
asks whether to disable all its skills (kept in the profile, listed in
``skills.disabled``) or remove them from that profile only — never from the
registry, never from other profiles.

Install target follows the per-profile default: a DM installs into the
sender's profile; a group installs into that group's profile when it exists,
else the sender's personal profile. The tapper always installs into their
own target — a button tapped by someone else installs into theirs.

Callbacks are ``skm:``-prefixed and stateless apart from a short-lived token
map (repo names can be 64 chars, longer than a callback payload allows
once prefixed). Tokens expire after a day; a stale tap just says the
listing expired. Every tap is answered silently — never an alert.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .callback_answer import answer_callback
from .mtproto_rich import edit_request, inline_markup, rich_button, rich_page_footer

logger = logging.getLogger(__name__)

_MARKET_PREFIX = "skm:"
_TOKEN_TTL_SECONDS = 24 * 60 * 60
_PAGE_SIZE = 10
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Index shipped with this module: the market lists from it when the host's
# market dir has no market.json, so no skill files need to exist on the host.
_BUNDLED_MARKET_JSON = Path(__file__).with_name("market.json")

_CLONE_TIMEOUT_SECONDS = 120
_SPARSE_CHECKOUT_TIMEOUT_SECONDS = 60

# token -> {"name", "kind", "page", "market_message_id", "created"}
_TOKENS: dict[str, dict[str, Any]] = {}


def _mint_token(name: str, kind: str, *, page: int = 0,
                market_message_id: Any = None) -> str:
    _prune_tokens()
    while True:
        token = secrets.token_hex(4)
        if token not in _TOKENS:
            _TOKENS[token] = {
                "name": name,
                "kind": kind,
                "page": int(page),
                "market_message_id": market_message_id,
                "created": time.monotonic(),
            }
            return token


def _take_token(token: str, kind: str) -> dict[str, Any] | None:
    _prune_tokens()
    entry = _TOKENS.pop(token, None)
    if entry is None or entry.get("kind") != kind:
        return None
    return entry


def _prune_tokens() -> None:
    now = time.monotonic()
    stale = [t for t, e in _TOKENS.items() if now - float(e.get("created", 0)) > _TOKEN_TTL_SECONDS]
    for token in stale:
        _TOKENS.pop(token, None)


def install_target_home(adapter: Any, source: Any) -> Path:
    """Profile home a market install/remove lands in for this tapper.

    DM: the source's runtime home. Group/forum: the group's profile home
    when it exists, else the tapper's personal (``tg-<id>``) profile.
    """
    chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
    if chat_type in {"group", "forum"}:
        chat_id = str(getattr(source, "chat_id", "") or "").strip()
        try:
            from gateway.allowgc import resolve_home

            gc_home = resolve_home(chat_id, reap=False) if chat_id else None
        except Exception:
            gc_home = None
        if gc_home is not None:
            return Path(gc_home)
        user_id = str(getattr(source, "user_id", "") or "").strip()
        try:
            from gateway.user_profiles import user_profile_name
            from hermes_cli.profiles import get_profile_dir

            name = user_profile_name(user_id)
            if name:
                return get_profile_dir(name)
        except Exception:
            logger.debug("Market personal-home lookup failed", exc_info=True)
    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    home = resolver(source) if callable(resolver) else None
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return Path(home)


def _skill_names(root: Path) -> list[str]:
    """Skill dir names under *root* (a dir with a SKILL.md counts as a skill)."""
    if not root.is_dir():
        return []
    names = []
    for child in root.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file() and _NAME_RE.match(child.name):
            names.append(child.name)
    return sorted(names)


def _safe_repo_path(path: str) -> bool:
    """A repo subdirectory an on-demand fetch may check out: relative, no ``..``.

    ``.`` means the repo root itself is the skill (a single-skill repo).
    """
    if path == ".":
        return True
    if not path or "\x00" in path:
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and not PurePosixPath(path).is_absolute() and ".." not in parts


def _repo_skill_names(row: dict[str, Any]) -> list[str]:
    """Skill dir names listed in a repo index row, in index order."""
    skills = row.get("skills")
    if not isinstance(skills, list):
        return []
    return [str(s.get("name") or "").strip() for s in skills
            if isinstance(s, dict) and _safe_name(str(s.get("name") or "").strip())]


def _fetchable_repo(row: dict[str, Any] | None) -> bool:
    """Whether a market.json repo row can be installed on demand from its repo."""
    if not isinstance(row, dict):
        return False
    url = str(row.get("source_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    skills = row.get("skills")
    if not isinstance(skills, list) or not skills:
        return False
    for skill in skills:
        if not isinstance(skill, dict):
            return False
        if not _safe_name(str(skill.get("name") or "").strip()):
            return False
        if not _safe_repo_path(str(skill.get("path") or "").strip()):
            return False
    return True


def _read_market_index(path: Path) -> dict[str, dict[str, Any]]:
    """One market.json file as ``{repo name: row}``; unreadable or malformed yields {}.

    A row is a repo entry: ``name``, ``author``, ``repo``, ``source_url``,
    ``skill_count``, ``size_bytes`` and a ``skills`` list of per-skill rows.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("repos") if isinstance(raw, dict) else raw
    if isinstance(items, dict):
        items = [{"name": key, **(value if isinstance(value, dict) else {})}
                 for key, value in items.items()]
    if not isinstance(items, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if _NAME_RE.match(name):
            entries[name] = item
    return entries


def _load_market_metadata() -> dict[str, dict[str, Any]]:
    """market.json best-effort: ``{repo name: repo row}``.

    A market.json in the host's market dir wins; otherwise the index shipped
    with this module is used, so the market lists without any skill files on
    the host. A present-but-malformed host file yields no metadata — it does
    NOT fall back to the shipped index.
    """
    from agent.skill_utils import get_market_skills_dir

    host_index = get_market_skills_dir() / "market.json"
    if host_index.is_file():
        return _read_market_index(host_index)
    return _read_market_index(_BUNDLED_MARKET_JSON)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.1f} GB"


def _market_entries(target_home: Path) -> list[dict[str, Any]]:
    """Ordered repo entries: installable, installed, then dangling market rows.

    Each entry carries ``name``, ``author``, ``repo``, ``source_url``,
    ``skill_count``, ``size_bytes``, ``installed``, ``available`` (its skill
    dirs exist on the host, or its repo can be fetched on demand) and its
    ``skills`` list. A repo is installed when ALL its skill dirs exist in
    the target profile.
    """
    from agent.skill_utils import get_market_skills_dir

    market = get_market_skills_dir()
    metadata = _load_market_metadata()
    installed_names = set(_skill_names(target_home / "skills"))
    market_names = set(_skill_names(market))
    entries: list[dict[str, Any]] = []
    for name in sorted(metadata):
        row = metadata[name]
        skill_names = _repo_skill_names(row)
        installed = bool(skill_names) and all(n in installed_names for n in skill_names)
        on_host = bool(skill_names) and all(n in market_names for n in skill_names)
        available = on_host or _fetchable_repo(row)
        entries.append({
            "name": name,
            "author": str(row.get("author") or "").strip(),
            "repo": str(row.get("repo") or "").strip(),
            "source_url": str(row.get("source_url") or "").strip(),
            "skill_count": int(row.get("skill_count") or len(skill_names)),
            "size_bytes": row.get("size_bytes"),
            "installed": installed,
            "available": available,
            "skills": row.get("skills") if isinstance(row.get("skills"), list) else [],
        })
    entries.sort(key=lambda e: (1 if e["installed"] else 0 if e["available"] else 2,
                               e["name"]))
    return entries


def _entry_html(entry: dict[str, Any]) -> str:
    """One repo as a single HTML block: name (+ badge), URL, skills, size.

    The badge is a small inline rich badge right after the name — never a
    standalone pill row.
    """
    name = html.escape(entry["name"])
    badge = ""
    if entry["installed"]:
        badge = " " + rich_button("installed", kind="disabled", style="success")
    elif not entry["available"]:
        badge = " " + rich_button("not available", kind="disabled", style="danger")
    parts = [f"<p><b>{name}</b>{badge}</p>"]
    url = str(entry.get("source_url") or "").strip()
    if url.startswith(("http://", "https://")):
        parts.append(f'<p><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></p>')
    size = entry.get("size_bytes")
    size_text = f" · Size: {_format_size(size)}" if isinstance(size, int) else ""
    parts.append(f"<p>{entry['skill_count']} skills{size_text}</p>")
    return "".join(parts)


def _render(target_home: Path, note: str = "", page: int = 0) -> tuple[str, Any]:
    """Market message text + keyboard for a target home, one page of ten.

    Tokens are re-minted on every render; taps on a superseded render report
    the listing expired. Installed repos carry a green badge and a green
    button; installable repos a plain install button; dangling market.json
    rows carry a not-available badge and no button.
    """
    entries = _market_entries(target_home)
    total = len(entries)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    shown = entries[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
    lines = [
        "<h1>Skill Market</h1>",
        "<p>Browse the shared market. A plain button installs a whole repo; "
        "a green button manages one already installed.</p>",
    ]
    if shown:
        lines.append("\n<hr />\n".join(_entry_html(entry) for entry in shown))
    else:
        lines.append("<p>The market is empty.</p>")
    lines.append(rich_page_footer(page + 1, pages, total))
    if note:
        lines.append(f"<p><b>Status:</b> {html.escape(note)}</p>")
    rows = []
    for entry in shown:
        if not entry["available"]:
            continue
        if entry["installed"]:
            rows.append([(
                f"Manage {entry['name']}",
                f"{_MARKET_PREFIX}m:{_mint_token(entry['name'], 'manage', page=page)}",
                "success",
            )])
        else:
            rows.append([(
                f"Install {entry['name']}",
                f"{_MARKET_PREFIX}i:{_mint_token(entry['name'], 'install', page=page)}",
                None,
            )])
    rows.append([
        ("Cancel", f"{_MARKET_PREFIX}cancel", "danger"),
        ("Back", f"{_MARKET_PREFIX}back:{page}", None),
    ])
    return "\n".join(lines), inline_markup(rows)


async def open_market(adapter: Any, source: Any, event: Any) -> None:
    """Send the market listing as a new message, replying to the /skills card."""
    target_home = install_target_home(adapter, source)
    text, markup = _render(target_home)
    reply_to = getattr(event, "message_id", None)
    try:
        reply_to_int = int(reply_to) if reply_to is not None else None
    except (TypeError, ValueError):
        reply_to_int = None
    await adapter._send_rich_ui(source, text, markup, reply_to=reply_to_int)


async def _edit_market(adapter: Any, source: Any, event: Any, note: str, *,
                       page: int = 0, message_id: Any = None) -> None:
    target_id = message_id if message_id is not None else getattr(event, "message_id", None)
    if adapter._client is None or not target_id:
        return
    target_home = install_target_home(adapter, source)
    text, markup = _render(target_home, note=note, page=page)
    try:
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(source.chat_id), int(target_id), text,
            reply_markup=markup, rich_format="html",
        ))
    except Exception:
        # The listing is already current (double tap) — nothing to do.
        logger.debug("Market card edit failed", exc_info=True)


async def _delete_market_message(adapter: Any, source: Any, event: Any) -> None:
    message_id = getattr(event, "message_id", None)
    delete = getattr(adapter, "delete_message", None)
    if not callable(delete) or not message_id:
        return
    try:
        await delete(source.chat_id, message_id)
    except Exception:
        logger.debug("Market message delete failed", exc_info=True)


def _is_skill_disabled(target_home: Path, name: str) -> bool:
    """Whether *name* is in the target profile's ``skills.disabled`` list."""
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(target_home) / "config.yaml")
        skills = config.get("skills") if isinstance(config, dict) else None
        disabled = skills.get("disabled") if isinstance(skills, dict) else None
        if isinstance(disabled, list):
            return name in {str(item) for item in disabled}
        return bool(disabled) and str(disabled) == name
    except Exception:
        return False


def _set_skills_disabled(target_home: Path, names: list[str], disabled: bool) -> bool:
    """Add/remove *names* in the target profile's ``skills.disabled`` list, one write."""
    try:
        from hermes_cli.config import atomic_config_write, require_readable_config_before_write

        path = Path(target_home) / "config.yaml"
        cfg = require_readable_config_before_write(path)
        skills = cfg.get("skills")
        if not isinstance(skills, dict):
            skills = {}
            cfg["skills"] = skills
        current = skills.get("disabled")
        if isinstance(current, list):
            current_names = {str(item) for item in current}
        elif isinstance(current, str) and current.strip():
            current_names = {current.strip()}
        else:
            current_names = set()
        if disabled:
            current_names.update(names)
        else:
            current_names.difference_update(names)
        skills["disabled"] = sorted(current_names)
        atomic_config_write(path, cfg)
    except Exception:
        logger.warning("Market disable failed for %s", names, exc_info=True)
        return False
    return True


def _set_skill_disabled(target_home: Path, name: str, disabled: bool) -> bool:
    """Add/remove *name* in the target profile's ``skills.disabled`` list."""
    return _set_skills_disabled(target_home, [name], disabled)


async def _open_manage_prompt(adapter: Any, source: Any, name: str,
                              market_message_id: Any, page: int) -> None:
    """Small prompt under the market message: Disable or Remove this repo."""
    target_home = install_target_home(adapter, source)
    row = _load_market_metadata().get(name) or {}
    skill_names = _repo_skill_names(row)
    disabled = [n for n in skill_names if _is_skill_disabled(target_home, n)]
    if skill_names and len(disabled) == len(skill_names):
        state = "Disabled — files kept."
    elif disabled:
        state = f"{len(disabled)} of {len(skill_names)} skills disabled — files kept."
    else:
        state = "Enabled."
    text = (
        f"<p><b>{html.escape(name)}</b> is installed in this profile "
        f"({len(skill_names)} skills). {state}</p>"
        "<p>Disable keeps the files. Remove uninstalls it from this profile.</p>"
    )
    rows = [[
        ("Disable", f"{_MARKET_PREFIX}d:{_mint_token(name, 'disable', page=page, market_message_id=market_message_id)}", None),
        ("Remove", f"{_MARKET_PREFIX}r:{_mint_token(name, 'remove', page=page, market_message_id=market_message_id)}", "danger"),
        ("Cancel", f"{_MARKET_PREFIX}cancel", None),
    ]]
    await adapter._send_rich_ui(source, text, inline_markup(rows))


# Env keys a git fetch child may inherit: lookup path, home (git config),
# and egress proxy settings. Everything else — profile secrets included —
# stays in the parent process.
_GIT_ENV_PASSTHROUGH = (
    "PATH", "HOME", "SYSTEMROOT",
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy",
)


def _git_env() -> dict[str, str]:
    """Minimal env for the fetch child: never prompt, never inherit secrets."""
    env = {key: os.environ[key] for key in _GIT_ENV_PASSTHROUGH if key in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(argv: list[str], cwd: Path | None, timeout: int, *, verb: str) -> Any:
    """One git invocation; raises RuntimeError naming the verb on failure."""
    try:
        result = subprocess.run(
            argv, cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, timeout=timeout, env=_git_env(),
            check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed or not in PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {verb} timed out after {timeout} seconds.") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(
            f"git {verb} failed: {detail[-1] if detail else 'unknown error'}")
    return result


def _fetch_repo_from_repo(source_url: str, dest: Path) -> None:
    """Blobless sparse clone of a whole repo, then the full tree checked out.

    Raises RuntimeError with a plain reason on any failure. The caller owns
    *dest*'s parent and cleans the whole temp dir up afterwards. Skill
    validation happens in the caller, which knows the repo's skill list.
    (``sparse-checkout set .`` only materialises top-level files, so the
    checkout is disabled instead — that is what gets the whole tree.)
    """
    if not source_url.startswith(("http://", "https://")):
        raise RuntimeError(f"repo not fetchable from {source_url}")
    git_exe = shutil.which("git")
    if git_exe is None:
        raise RuntimeError("git is not installed or not in PATH.")
    _run_git([git_exe, "clone", "--filter=blob:none", "--depth", "1", "--sparse",
              source_url, str(dest)], cwd=None,
             timeout=_CLONE_TIMEOUT_SECONDS, verb="clone")
    _run_git([git_exe, "-C", str(dest), "sparse-checkout", "disable"],
             cwd=None, timeout=_SPARSE_CHECKOUT_TIMEOUT_SECONDS,
             verb="sparse-checkout")


def _safe_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and name not in {".", ".."}


def _repo_skill_dirs(row: dict[str, Any]) -> list[tuple[str, str]]:
    """``(skill name, repo-relative path)`` pairs for a repo index row."""
    skills = row.get("skills")
    if not isinstance(skills, list):
        return []
    pairs = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name") or "").strip()
        path = str(skill.get("path") or "").strip()
        if _safe_name(name) and _safe_repo_path(path):
            pairs.append((name, path))
    return pairs


async def _install_repo(target_home: Path, row: dict[str, Any]) -> str:
    """Install a whole repo into *target_home*; returns the note for the card.

    Raises RuntimeError with a plain reason on failure. A skill dir already
    in the profile is left alone; any skill dir this install created is
    removed again on failure. The fetched clone's ``.git`` never lands in
    the profile.
    """
    from agent.skill_utils import get_market_skills_dir

    name = str(row.get("name") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    pairs = _repo_skill_dirs(row)
    if not pairs:
        raise RuntimeError(f"{name} has no installable skills.")
    target_skills = target_home / "skills"
    ignore_git = shutil.ignore_patterns(".git")
    market = get_market_skills_dir()
    if all((market / skill_name).is_dir()
           and (market / skill_name / "SKILL.md").is_file()
           for skill_name, _path in pairs):
        # Fast path: every skill dir is already on the host — plain copy.
        target_skills.mkdir(parents=True, exist_ok=True)
        for skill_name, _path in pairs:
            target = target_skills / skill_name
            if not target.exists():
                shutil.copytree(market / skill_name, target, ignore=ignore_git)
        return f"Installed {name} into this profile."
    if not _fetchable_repo(row):
        raise RuntimeError(f"{name} is no longer in the market.")
    created: list[Path] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="hermes-market-"))
    try:
        repo_dir = tmpdir / "repo"
        await asyncio.to_thread(_fetch_repo_from_repo, source_url, repo_dir)
        fetched: dict[str, Path] = {}
        for skill_name, path in pairs:
            skill_dir = repo_dir if path == "." else repo_dir / path
            if not (skill_dir / "SKILL.md").is_file():
                raise RuntimeError(f"skill {skill_name} not found in {source_url}")
            fetched[skill_name] = skill_dir
        target_skills.mkdir(parents=True, exist_ok=True)
        for skill_name, skill_dir in fetched.items():
            target = target_skills / skill_name
            if not target.exists():
                shutil.copytree(skill_dir, target, ignore=ignore_git)
                created.append(target)
    except Exception:
        for target in created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return f"Installed {name} into this profile."


def _remove_repo(target_home: Path, row: dict[str, Any]) -> str:
    """Remove every skill dir of a repo from *target_home*."""
    name = str(row.get("name") or "").strip()
    target_skills = target_home / "skills"
    removed = False
    for skill_name in _repo_skill_names(row):
        target = target_skills / skill_name
        if target.is_dir():
            try:
                shutil.rmtree(target)
                removed = True
            except Exception as exc:
                logger.warning("Market remove failed for %s", skill_name, exc_info=True)
                return f"Could not remove {name}: {exc}"
    return (f"Removed {name} from this profile." if removed
            else f"{name} is not installed here.")


async def _apply_install_remove(adapter: Any, source: Any, event: Any,
                                kind: str, entry: dict[str, Any]) -> None:
    """Install (plain button) or remove (prompt choice) a whole repo, then re-render."""
    name = entry["name"]
    page = int(entry.get("page") or 0)
    market_message_id = entry.get("market_message_id")
    target_home = install_target_home(adapter, source)
    row = _load_market_metadata().get(name)
    if not isinstance(row, dict) or not _repo_skill_names(row):
        await _edit_market(adapter, source, event,
                           note=f"{name} is no longer in the market.", page=page)
        return
    if kind == "install":
        installed_names = set(_skill_names(target_home / "skills"))
        if all(n in installed_names for n in _repo_skill_names(row)):
            await _edit_market(adapter, source, event,
                               note=f"{name} is already installed.", page=page)
            return
        try:
            note = await _install_repo(target_home, row)
        except Exception as exc:
            logger.warning("Market install failed for %s", name, exc_info=True)
            note = f"Could not install {name}: {exc}"
        await _edit_market(adapter, source, event, note=note, page=page)
        return
    note = _remove_repo(target_home, row)
    if market_message_id is not None:
        await _delete_market_message(adapter, source, event)
    await _edit_market(adapter, source, event, note, page=page, message_id=market_message_id)


async def handle_market_callback(adapter: Any, event: Any, source: Any, data: str) -> None:
    """Route one ``skm:`` tap. Answers silently; never an alert."""
    if data == f"{_MARKET_PREFIX}market":
        await answer_callback(event)
        await open_market(adapter, source, event)
        return
    if data == f"{_MARKET_PREFIX}cancel":
        await answer_callback(event)
        await _delete_market_message(adapter, source, event)
        return
    if data.startswith(f"{_MARKET_PREFIX}back:"):
        await answer_callback(event)
        try:
            page = int(data[len(_MARKET_PREFIX) + 5:])
        except ValueError:
            page = 0
        if page <= 0:
            await _delete_market_message(adapter, source, event)
        else:
            await _edit_market(adapter, source, event, "", page=page - 1)
        return
    if data.startswith(f"{_MARKET_PREFIX}m:"):
        entry = _take_token(data[len(_MARKET_PREFIX) + 2:], "manage")
        if entry is None or not _safe_name(entry["name"]):
            await answer_callback(event, "This market listing expired. Send /skills again.")
            return
        await answer_callback(event)
        await _open_manage_prompt(adapter, source, entry["name"],
                                  getattr(event, "message_id", None),
                                  int(entry.get("page") or 0))
        return
    if data.startswith(f"{_MARKET_PREFIX}d:"):
        entry = _take_token(data[len(_MARKET_PREFIX) + 2:], "disable")
        if entry is None or not _safe_name(entry["name"]):
            await answer_callback(event, "This market listing expired. Send /skills again.")
            return
        await answer_callback(event)
        name = entry["name"]
        target_home = install_target_home(adapter, source)
        row = _load_market_metadata().get(name) or {}
        skill_names = _repo_skill_names(row)
        if not skill_names:
            note = f"{name} is no longer in the market."
        elif all(_is_skill_disabled(target_home, n) for n in skill_names):
            note = f"{name} is already disabled."
        elif _set_skills_disabled(target_home, skill_names, True):
            note = f"Disabled {name} — files kept."
        else:
            note = f"Could not disable {name}."
        await _delete_market_message(adapter, source, event)
        await _edit_market(adapter, source, event, note,
                           page=int(entry.get("page") or 0),
                           message_id=entry.get("market_message_id"))
        return
    kind = None
    if data.startswith(f"{_MARKET_PREFIX}i:"):
        kind, token = "install", data[len(_MARKET_PREFIX) + 2:]
    elif data.startswith(f"{_MARKET_PREFIX}r:"):
        kind, token = "remove", data[len(_MARKET_PREFIX) + 2:]
    else:
        await answer_callback(event, "This market action is unavailable.")
        return
    entry = _take_token(token, kind) if kind else None
    if entry is None or not _safe_name(entry["name"]):
        await answer_callback(event, "This market listing expired. Send /skills again.")
        return
    await answer_callback(event)
    await _apply_install_remove(adapter, source, event, kind, entry)
