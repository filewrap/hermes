"""Detection variants for approval scanning (sibling of ``tools/approval_detection.py``).

One command reaches the hardline/dangerous patterns as several projections:
the normalized form, exec-via-flag payloads, Windows-path flattenings,
marked command starts, and deobfuscated command words. Moved out of
``approval_detection.py`` so that file stays under its line cap.

The orchestrator mirrors upstream's structure: ``_command_detection_variants``
scans each heredoc-bounded source separately (a heredoc body is inert text —
an unmatched quote inside it used to desync quote tracking so the command
after the heredoc stopped reaching the patterns, #140704), and
``_single_command_detection_variants`` builds the projections for one source,
with ``floors`` keeping the raw command for the hardline/deny floors and
``capped`` failing closed past the eval-nesting cap. On top of that the fork
keeps its regex exec-via-flag pass (``_exec_flag_payloads``), which upstream
lacks — it covers text the structural parser cannot tokenize.

The detection helpers (``_normalize_command_for_detection`` and friends)
stay on the facade; the orchestrator imports them lazily (the repo's
sibling pattern — no module-level cycle).
"""
from __future__ import annotations

import re

# Exec-via-flag payload extraction (port of Kilo-Org/kilocode#11890).
# Unlike upstream's unscoped long-flag regex, each flag is scoped to its
# OWNING command: ``pip install --pre 'rm -rf /'`` must not promote the
# payload (``--pre`` is not rg's preprocessor there), and neither must
# ``grep -P``. The fork's structural parser (_execution_flag_findings)
# owns that scoping invariant; this regex pass mirrors it so the two
# extractors agree. See test_execution_flag_detection.py.
_EXEC_FLAG_PAYLOAD_RE = re.compile(
    r"""(?:\bsort\b[^\n;|&]*?--compress-program
        |\brg\b[^\n;|&]*?--(?:pre|hostname-bin)
        |\bag\b[^\n;|&]*?--pager
        |\bman\b[^\n;|&]*?--(?:pager|html))
        (?:=|\s+)                     # =value or separate word
        (?P<payload>'[^']*'|"[^"]*"|\S+)""",
    re.VERBOSE,
)

# man -P/-H short flags (possibly bundled, e.g. ``man -Psh ls``).  Scoped to a
# ``man`` command context: ``-P`` is a common flag elsewhere (``grep -P`` is a
# perl-regex switch) and extracting its argument there would false-positive on
# legitimate searches for dangerous-looking strings.
_MAN_EXEC_FLAG_PAYLOAD_RE = re.compile(
    r"""\bman\b[^\n;|&]*?
        \s-\w*[PH]
        (?:=|\s*)                     # bundled (-Psh), separate word, or =
        (?P<payload>'[^']*'|"[^"]*"|\S+)""",
    re.VERBOSE,
)


def _strip_optional_shell_quotes(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word


def _exec_flag_payloads(command: str):
    """Yield the program values passed to exec-via-flag options.

    Flags like ``sort --compress-program``, ``rg --pre``, ``ag --pager`` and
    ``man -P`` make an otherwise read-only command execute an arbitrary
    program.  The flag value is normally opaque argument text, so a hardline
    payload smuggled through it (``sort --compress-program='rm -rf /' f``)
    never reached the unconditional floor.  Surfacing each payload as its own
    detection variant lets the hardline/dangerous patterns match the wrapped
    command directly.  Port of Kilo-Org/kilocode#11890.
    """
    for regex in (_EXEC_FLAG_PAYLOAD_RE, _MAN_EXEC_FLAG_PAYLOAD_RE):
        for match in regex.finditer(command):
            payload = _strip_optional_shell_quotes(match.group("payload"))
            payload = payload.strip()
            if payload and not payload.startswith("-"):
                yield payload


def _exec_flag_payload_variants(normalized, fresh):
    # Exec-via-flag payloads: expose the program argument of flags that make
    # a read-only command execute an arbitrary program, so a hardline command
    # hidden inside (e.g.) --compress-program='rm -rf /' anchors at command
    # position and hits the unconditional floor. Port of Kilo-Org/kilocode#11890.
    # (The structural parser in _execution_flag_findings surfaces the same
    # payloads below; this regex pass also covers text it cannot tokenize.)
    for payload in _exec_flag_payloads(normalized):
        if fresh(payload):
            yield payload


def _command_detection_variants(
        command: str, eval_depth: int = 0, capped: list[str] | None = None, *, floors: bool = False):
    """Variants of the command with provably inert heredoc bodies dropped (#140704).

    A quote in a body (`it's`) desynced every quote-aware pass and hid the command after the
    heredoc; body lines projected as commands. `cat` bodies are data for the dangerous-prompt layer
    (unless the string runs the written file); the hardline and deny floors (*floors*) also scan the
    raw command, as main does. python/osascript dropped copies are scanned
    IN ADDITION. Bodies a shell runs (`bash <<EOF`, `cat <<EOF | sh`, `ssh host <<EOF`) and
    unterminated/mis-parsed `<<` stay.
    """

    from tools.approval_detection import _SHELL_NAMES_RE, heredoc_scan_sources
    seen: set[str] = set()
    for source in heredoc_scan_sources(command, _SHELL_NAMES_RE, floors=floors):
        for variant in _single_command_detection_variants(source, eval_depth, capped, floors=floors):
            if variant not in seen:
                seen.add(variant)
                yield variant


def _single_command_detection_variants(
        command: str, eval_depth: int = 0, capped: list[str] | None = None, *, floors: bool = False):
    from tools.approval_ansi_c import expand_ansi_c_quotes
    from tools.approval_detection import (
        _deobfuscate_shell_word_for_detection,
        _grep_safe_detection_variant,
        _iter_shell_command_word_spans,
        _join_escaped_word_chars,
        _mark_command_starts,
        _mask_quoted_newlines,
        _nested_payloads,
        _normalize_command_for_detection,
        _splice,
    )

    from tools.approval_wrappers import literal_heredoc_bodies
    # Mask quoted newlines BEFORE normalization: normalization strips escapes (\" -> ") and ""
    # pairs, corrupting quote tracking (`echo "a\""` becomes an unterminated quote) so masking
    # afterwards could swallow a REAL unquoted newline separator. The raw command carries faithful quote state
    # once $'...' is rewritten as plain single quotes (`$'\''` otherwise desyncs every quote-aware scanner).
    command = expand_ansi_c_quotes(command)
    normalized = _normalize_command_for_detection(_mask_quoted_newlines(command))
    # Quote-aware grep parsing hides only structurally identified pattern operands; malformed or
    # ambiguous input stays byte-for-byte intact.
    grep_safe, _ = _grep_safe_detection_variant(normalized)
    seen = {grep_safe}
    yield grep_safe

    def fresh(variant: str) -> bool:
        if not variant or variant in seen:
            return False
        seen.add(variant)
        return True

    # Fork-only: the regex exec-via-flag pass upstream lacks (see _exec_flag_payloads).
    yield from _exec_flag_payload_variants(normalized, fresh)

    # Windows-path variant: normalization strips backslashes as shell escapes, so `del C:\Users\me\.ssh\id_rsa`
    # reaches the patterns as `del C:Usersme.sshid_rsa`. When the RAW command has a drive-letter or UNC backslash
    # path, also yield a variant with backslashes flattened to `/` BEFORE normalization. Gated on a real path shape so
    # POSIX escape semantics (`echo a\"b`) are untouched elsewhere.
    # See #69472.
    pending = [(normalized, eval_depth)]
    if re.search(r"(?:[A-Za-z]:|\\\\)[\\\\]", command) or re.search(r"[A-Za-z]:\\", command):
        win_variant = _normalize_command_for_detection(_mask_quoted_newlines(command.replace("\\", "/")))
        if fresh(win_variant):
            yield win_variant
            # Its payloads too: from the normalized text `pwsh -c "ls; ri -r C:\\"` loses the root's slash.
            pending.append((win_variant, eval_depth))
        # Its command starts (`a; rd /s /q C:\`): marked on the RAW command (faithful quote state), then flattened.
        marked_win = _normalize_command_for_detection(
            _mark_command_starts(_mask_quoted_newlines(command), marker=" \n").replace("\\", "/"))
        if fresh(marked_win):
            yield marked_win
    # Program-bearing options are parsed in their owning command's context; surfacing only the payload lets the
    # hardline floor inspect what will actually run without promoting similar flags or quoted prose.
    while pending:
        source, depth = pending.pop()
        for payload, payload_depth in _nested_payloads(source, depth, capped):
            if fresh(payload):
                yield payload
                # A payload may start with an option-looking program and then invoke a hardline command
                # after a separator; mark its starts.
                marked_payload = _mark_command_starts(payload)
                if marked_payload != payload and fresh(marked_payload):
                    yield marked_payload
                pending.append((payload, payload_depth))
    if capped:  # the caller fails closed; the passes below would only rescan the chain
        return
    # Payloads parsed from NORMALIZED text lost their escapes before their own quote expansion ran
    # (`bash -c "$'\\x72\\x6d' -rf /"` reached the patterns as `$'x72x6d'`). Parse the raw argument
    # too and treat it as new shell input; each payload is shorter than its source, so recursion ends.
    for payload, payload_depth in _nested_payloads(_mask_quoted_newlines(command), eval_depth, capped):
        if payload != command:
            for variant in _command_detection_variants(payload, payload_depth, capped, floors=floors):
                if fresh(variant):
                    yield variant
    # Subshell `(cmd)` / brace-group `{ cmd; }` openers put `cmd` at a real command position the flat `_CMDPOS`
    # patterns can't see (adding `(`/`{` there would match quoted prose like `--title "(reboot)"`). Insert a newline
    # at each start the QUOTE-AWARE tokenizer found instead; this covers every `_CMDPOS` rule in one place.
    marked = _mark_command_starts(grep_safe)
    if marked != grep_safe and fresh(marked):
        yield marked
    # Every variant above tracks quotes on NORMALIZED text, where `\"` has already become `"`. That
    # flips quote parity, so in `cat "f\"n.txt"; rm -rf /` the `; rm` start sat "inside" a phantom
    # quote, no start was marked, and the hardline floor let it through. Mark starts on the RAW
    # command (only quoted newlines masked), then normalize; the leading space keeps the marker
    # from being eaten as a `\<newline>` continuation when the preceding text ends in a backslash.
    faithful = _normalize_command_for_detection(_mark_command_starts(_mask_quoted_newlines(command), marker=" \n"))
    if fresh(faithful):
        yield faithful
    # An escaped separator is literal text inside its word (`git push -o ci\;skip -f`), but
    # normalization strips the backslash and leaves a real `;` that ends a segment-bounded match.
    # The same holds for an escaped space or quote inside an option value (`git -C my\\ repo push -f`).
    joined = _join_escaped_word_chars(command)
    if joined != command and fresh(variant := _normalize_command_for_detection(_mask_quoted_newlines(joined))):
        yield variant
    # Quoting/escaping can spell an executable in pieces (r\m, r''m). Keep that deobfuscation scoped
    # to command words so arguments don't false-positive.
    # One variant with EVERY command word deobfuscated, not one full-length variant per word: a heredoc
    # body of quoted lines has hundreds of quoted command words, and per-word variants made both
    # detection passes O(words * len) — minutes of GIL-held regex on a 15 KB command (#113535).
    # Spans arrive out of order (loop/conditional bodies after their keywords) and can nest (a
    # backtick word and the command inside it), so apply them sorted; spans overlapping an applied
    # one wait for the next round, one combined variant per nesting level.
    pending = sorted(
        ((word_start, word_end, deobfuscated) for word_start, word_end, word in _iter_shell_command_word_spans(normalized)
         if (deobfuscated := _deobfuscate_shell_word_for_detection(word)) and deobfuscated != word),
        key=lambda span: span[:2],
    )
    while pending:
        applied, carry, cursor = [], [], 0
        for span in pending:
            if span[0] < cursor:
                carry.append(span)
            else:
                applied.append(span)
                cursor = span[1]
        variant = _splice(normalized, applied)
        # A dequoted verb after a separator (`ls; "reboot"`) reaches _CMDPOS only once its start is marked; not inside
        # a quoted-delimiter heredoc fed to a non-shell (that body is literal data).
        yield from filter(fresh, (variant, _mark_command_starts(variant, skip=literal_heredoc_bodies(variant))))
        pending = carry
