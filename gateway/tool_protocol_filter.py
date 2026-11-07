"""Remove provider tool-protocol wrappers accidentally emitted as chat text.

Provider tool calls are handled structurally by the agent. If a provider instead
returns their ``<|tool_call...|>`` / ``<|tool_arg...|>`` wrappers as ordinary
assistant content, never display the wrapper or its argument payload. Fenced and
matched inline code are preserved verbatim so explanations and code examples
remain usable.
"""
from __future__ import annotations

import re

_MARKER_RE = re.compile(
    r"<\|tool_(call|arg)(?::([a-z0-9_.-]{1,128}))?\|>", re.IGNORECASE,
)
_CLOSE_ACTIONS = frozenset({"end", "finish", "done", "stop", "close", "complete"})
_MAX_PARTIAL_MARKER_LENGTH = 160


def _is_partial_marker(text: str, start: int = 0) -> bool:
    """Whether a bounded suffix could still become a tool marker."""
    if len(text) - start > _MAX_PARTIAL_MARKER_LENGTH:
        return False
    lower = text[start:].lower()
    for base in ("<|tool_call", "<|tool_arg"):
        if base.startswith(lower):
            return True
        if not lower.startswith(base):
            continue
        rest = lower[len(base):]
        if rest in {"", "|"}:
            return True
        if rest.startswith(":"):
            suffix = rest[1:-1] if rest.endswith("|") else rest[1:]
            return len(suffix) <= 128 and all(
                char.isalnum() or char in "_.-" for char in suffix
            )
    return False


class ToolProtocolOutputFilter:
    """Incrementally strip raw tool blocks while preserving Markdown code.

    ``feed`` returns only newly safe text. An incomplete marker suffix is held
    between calls, so a provider delta split at any character cannot leak a
    partial wrapper. A candidate inline-code span is preserved only after a
    matching-length closer is seen; unmatched backticks are literal text. An
    unclosed tool block is suppressed through end-of-stream.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._blocks: list[str] = []
        self._fence_char: str | None = None
        self._fence_length = 0
        self._inline_candidate_ticks = 0
        self._inline_search_from = 0
        self._line_start = True
        self._line_indent = 0
        self._backslash_run = 0

    @staticmethod
    def _run_length(text: str, start: int, char: str) -> int:
        end = start
        while end < len(text) and text[end] == char:
            end += 1
        return end - start

    @classmethod
    def _find_inline_close(cls, text: str, start: int, tick_count: int) -> int | None:
        """End offset for the next exact-length inline-code delimiter run."""
        i = start
        while i < len(text):
            if text[i] != "`":
                i += 1
                continue
            count = cls._run_length(text, i, "`")
            if count == tick_count:
                return i + count
            i += count
        return None

    @staticmethod
    def _trailing_tick_start(text: str) -> int:
        i = len(text)
        while i and text[i - 1] == "`":
            i -= 1
        return i

    def feed(self, text: str = "", *, final: bool = False) -> str:
        """Consume one delta; pass ``final=True`` to resolve held suffixes."""
        self._pending += str(text or "")
        buf = self._pending
        out: list[str] = []
        i = 0
        size = len(buf)

        def advance_line_state(value: str) -> None:
            for char in value:
                if char == "\\":
                    self._backslash_run += 1
                else:
                    self._backslash_run = 0
                if char == "\n":
                    self._line_start = True
                    self._line_indent = 0
                elif self._line_start and char == " ":
                    self._line_indent += 1
                else:
                    self._line_start = False

        def emit(value: str) -> None:
            out.append(value)
            advance_line_state(value)

        def discard(value: str) -> None:
            advance_line_state(value)

        def marker_action(kind: str, action: str) -> None:
            if action.casefold() in _CLOSE_ACTIONS:
                if kind in self._blocks:
                    # Closing a call also closes any still-open nested argument.
                    index = len(self._blocks) - 1 - self._blocks[::-1].index(kind)
                    del self._blocks[index:]
            else:
                # Unknown suffixes are treated as openers conservatively: an
                # unrecognized raw wrapper must not expose its payload.
                self._blocks.append(kind)

        # A possible inline code span is held until its matching delimiter is
        # found. If none exists at end-of-stream, release the opener as literal
        # text and resume normal protocol scanning for the rest of the content.
        if self._inline_candidate_ticks:
            close_end = self._find_inline_close(
                buf, self._inline_search_from, self._inline_candidate_ticks,
            )
            if close_end is not None:
                emit(buf[:close_end])
                i = close_end
                self._inline_candidate_ticks = 0
                self._inline_search_from = 0
            elif not final:
                trailing_start = self._trailing_tick_start(buf)
                self._inline_search_from = max(self._inline_candidate_ticks, trailing_start)
                self._pending = buf
                return "".join(out)
            else:
                opener_length = self._inline_candidate_ticks
                emit(buf[:opener_length])
                i = opener_length
                self._inline_candidate_ticks = 0
                self._inline_search_from = 0

        while i < size:
            char = buf[i]

            # Inside an ordinary-content tool block, discard payload wholesale;
            # only matching protocol close markers can resume visible output.
            if self._blocks:
                marker = _MARKER_RE.match(buf, i) if char == "<" else None
                if marker is not None:
                    kind, action = marker.group(1).casefold(), marker.group(2) or ""
                    marker_action(kind, action)
                    token = marker.group(0)
                    discard(token)
                    i = marker.end()
                    continue
                if char == "<" and _is_partial_marker(buf, i):
                    if not final:
                        break
                    discard(buf[i:])
                    i = size
                    continue
                discard(char)
                i += 1
                continue

            # A closing fence is active only at line start, with up to three
            # leading spaces and a matching run of delimiters.
            if self._fence_char is not None:
                if (self._line_start and self._line_indent <= 3
                        and char == self._fence_char):
                    count = self._run_length(buf, i, char)
                    if count >= self._fence_length:
                        after = i + count
                        while after < size and buf[after] in " \t\r":
                            after += 1
                        if after == size or buf[after] == "\n":
                            close_end = after + 1 if after < size else after
                            emit(buf[i:close_end])
                            self._fence_char = None
                            self._fence_length = 0
                            i = close_end
                            continue
                emit(char)
                i += 1
                continue

            # Opening fenced code blocks: CommonMark-style runs at line start,
            # optionally indented by at most three spaces. Hold incomplete runs.
            if char in "`~" and self._line_start and self._line_indent <= 3:
                count = self._run_length(buf, i, char)
                if i + count == size and not final and count < 3:
                    break
                if count >= 3:
                    self._fence_char = char
                    self._fence_length = count
                    emit(buf[i:i + count])
                    i += count
                    continue
                if char == "~":
                    emit(buf[i:i + count])
                    i += count
                    continue
                # One/two ticks fall through to matched inline-code detection.

            # A backslash-escaped tick is literal Markdown text, not a delimiter.
            if char == "`" and self._backslash_run % 2:
                emit(char)
                i += 1
                continue

            # Preserve inline code only when a matching-length closer exists.
            if char == "`":
                count = self._run_length(buf, i, char)
                if i + count == size and not final:
                    break
                close_end = self._find_inline_close(buf, i + count, count)
                if close_end is not None:
                    emit(buf[i:close_end])
                    i = close_end
                    continue
                if not final:
                    self._inline_candidate_ticks = count
                    trailing_start = self._trailing_tick_start(buf)
                    self._inline_search_from = max(count, trailing_start - i)
                    break
                # CommonMark treats an unmatched backtick run literally.
                emit(buf[i:i + count])
                i += count
                continue

            # An outside-code tool marker is removed. Open markers suppress
            # their payload; stray end markers do not suppress following prose.
            if char == "<":
                marker = _MARKER_RE.match(buf, i)
                if marker is not None:
                    kind, action = marker.group(1).casefold(), marker.group(2) or ""
                    marker_action(kind, action)
                    token = marker.group(0)
                    discard(token)
                    i = marker.end()
                    continue
                if _is_partial_marker(buf, i):
                    if not final:
                        break
                    discard(buf[i:])
                    i = size
                    continue

            emit(char)
            i += 1

        self._pending = buf[i:]
        if final and self._pending:
            # A dangling protocol-token prefix is not user-facing prose.
            if _is_partial_marker(self._pending) or self._blocks:
                discard(self._pending)
            else:
                emit(self._pending)
            self._pending = ""
        return "".join(out)


def filter_tool_protocol_output(text: str) -> str:
    """Stateless final/egress filter with the same rules as the stream filter."""
    return ToolProtocolOutputFilter().feed(str(text or ""), final=True)
