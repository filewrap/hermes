"""Conservative, deterministic suggestions for Telegram message reactions.

This local heuristic uses no network, inference, or persistent state. It ignores
quoted/code text, gives explicit suggestions priority over incidental keywords,
and returns ``None`` when intent is not clear.
"""
from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
# Telegram's Bot API ReactionTypeEmoji documentation lists these standard
# reactions. Chat administrators may still restrict which subset is enabled.
TELEGRAM_STANDARD_REACTION_EMOJI = frozenset({
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬",
    "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴",
    "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔",
    "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻",
    "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅",
    "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊",
    "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
})

_EXPLICIT_REACTION_REQUEST = re.compile(
    r"\s*(?:please\s+)?react\s+to\s+this\s+message(?:\s+(?P<emoji>\S+))?\s*[.!]?\s*",
    re.IGNORECASE,
)


def explicit_reaction_request(text: str) -> str | None:
    """Return an explicitly requested standard emoji for the exact reaction phrase."""
    if not isinstance(text, str):
        return None
    match = _EXPLICIT_REACTION_REQUEST.fullmatch(text)
    if not match:
        return None
    emoji = (match.group("emoji") or "👍").rstrip(".!")
    if emoji.replace("\ufe0f", "") not in TELEGRAM_STANDARD_REACTION_EMOJI:
        return None
    return emoji


def _meaningful_text(value: str) -> str:
    if re.fullmatch(r'\s*"[^"]*"\s*', value):
        return ""
    value = _CODE_FENCE.sub(" ", value)
    value = _INLINE_CODE.sub(" ", value)
    value = re.sub(r'"[^"]*"', " ", value)
    kept = []
    for line in value.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">") or re.match(r"^(?:On .+wrote:|\[quote\])", stripped, re.IGNORECASE):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).lower().replace("’", "'").replace("‘", "'").split())


def choose_reaction(text: str, *, reply_text: str = "") -> str | None:
    """Choose an emoji for clear intent, or ``None`` when uncertain.

    Reply context only resolves terse acknowledgements when it clearly asks
    whether something worked; a suggestion to try a fix is not a success claim.
    """
    if not isinstance(text, str) or not isinstance(reply_text, str):
        return None
    raw = text.strip()
    if not raw or raw.startswith("/"):
        return None
    body = _meaningful_text(raw)
    if not body:
        return None

    distress = r"\b(?:sorry to hear|so sorry|sad|heartbroken|devastated|grieving|passed away|died|death|awful|terrible|scared|anxious|worried|struggling|help me|lost my job)\b"
    if re.search(distress, body):
        return "😢"

    failure = (
        r"\b(?:it|that|this|the fix|the update|the script|the test)\s+(?:did not|didn't|"
        r"does not|doesn't|has not|hasn't|have not|haven't|never)\s+(?:work|worked|working)\b|"
        r"\b(?:not fixed|not working|still not|won't work|broken|failed|failure|error|"
        r"crash(?:ed|ing)?|can't|cannot|unable to)\b"
    )
    if re.search(failure, body):
        return "😢"
    if re.search(r"\b(?:don't|do not|never)\s+(?:agree|think|believe|appreciate)\b|\bnot thanks\b", body):
        return None

    suggestion = re.search(
        r"\b(?:suggest(?:ion)?s?|recommend(?:ation)?s?|any ideas|what should i|"
        r"how about|could we|we should|you should|i suggest|i recommend)\b", body
    )
    if suggestion:
        return "👀"

    # A question about success is not itself a success report.
    if ("?" in body or re.match(
        r"^(?:what|why|when|where|who|how|can|could|would|should|are|do|does|did)\b", body
    )):
        return "🤔"

    success = re.search(
        r"\b(?:it|that|this|the fix|the update|the script|the test)\s+(?:finally\s+)?(?:worked|works|is working|worked perfectly)\b|"
        r"\b(?:fixed it|got it working|it's working|it is working)\b", body
    )
    if success:
        before = body[max(0, success.start() - 24):success.start()]
        if re.search(r"\b(?:not|never|didn't|doesn't|hasn't|hadn't|no longer)\s*$", before):
            return None
        return "🎉"

    if re.search(r"\b(?:thank you|thanks|thx|appreciate|grateful)\b", body):
        return "🙏"
    if re.search(r"\b(?:haha+|lol|lmao|rofl|that's funny|that is funny|hilarious)\b", body):
        return "🤣"
    if re.search(r"\b(?:agree|exactly|absolutely|you're right|you are right|same here|sounds good|makes sense)\b", body):
        return "👍"

    if body in {"yes", "yep", "it worked", "works now", "all good"}:
        context = _meaningful_text(reply_text)
        if re.search(failure, context):
            return "😢"
        if re.search(r"\b(?:did it work|does it work|is it working|did that work|test result|confirm the test)\b", context):
            return "🎉"
        if re.search(r"\b(?:try this fix|try the fix|suggest|recommend)\b", context):
            return "👍" if body in {"yes", "yep"} else None

    if "?" in body or re.match(r"^(?:what|why|when|where|who|how|can|could|would|should|are|do|does|did)\b", body) or re.match(r"^is\s+(?:this|that|there|it|the)\b", body):
        return "🤔"
    return None
