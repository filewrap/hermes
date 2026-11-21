"""Stage 1 load gate for the web-designer add-on (audit 6.1).

Load only when the request is a website/webapp (HTML/CSS/JS/TS) AND the
user is admitted. Anything else is declined with a one-line redirect —
never silently loaded — and an ambiguous scope gets one clarifying
question instead of a load. One active run per user/thread.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import store
from .types import RunRecord

# Strong web signals: the request names a website/webapp outright.
_STRONG_WEB = (
    "website", "web site", "web-site", "webapp", "web app", "web-app",
    "landing page", "portfolio", "homepage", "home page", "web page",
    "webpage", "web design", "build a site", "build me a site",
    "make a site", "make me a site", "design a site", "create a site",
    "my site", "our site", "company site", "business site",
    "static site", "site for",
)

# Weaker signals that only count as web when paired with a build/design
# verb or a web-technology mention.
_WEAK_WEB = ("site", "page", "web", "html", "css", "javascript", "js",
             "typescript", "ts", "react", "tailwind")
_BUILD_VERBS = ("build", "make", "create", "design", "redesign", "rebuild",
                "code", "develop", "launch")
_TECH_SIGNALS = ("html", "css", "javascript", "typescript", "react",
                 "tailwind", "vue", "svelte", "next.js", "astro")

# Out-of-scope requests: declined with a redirect, never loaded.
_MOBILE = ("mobile app", "ios app", "android app", "iphone app",
           "react native", "flutter app", "swift app", "kotlin app",
           "native app", "app store")
_BRANDING_PRINT = ("logo", "branding", "business card", "poster", "flyer",
                   "print", "brochure", "letterhead", "brand identity")
_GENERAL_CODING = (
    "python script", "write a script", "fix my code", "debug my code",
    "write a function", "sql query", "bash script", "shell script",
    "cli tool", "command line", "discord bot", "telegram bot",
)


def _contains(text: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text for p in phrases)


def _word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _classify(text: str) -> tuple[str, str]:
    """(decision, reason) where decision is load / out_of_scope / ambiguous."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return "ambiguous", "ambiguous"
    if _contains(lowered, _MOBILE):
        return "out_of_scope", "mobile app"
    if _contains(lowered, _BRANDING_PRINT):
        return "out_of_scope", "branding/print"
    if _contains(lowered, _STRONG_WEB):
        return "load", "website request"
    if _contains(lowered, _GENERAL_CODING):
        return "out_of_scope", "general coding"
    weak = _contains(lowered, _WEAK_WEB)
    verb = _contains(lowered, _BUILD_VERBS)
    tech = _contains(lowered, _TECH_SIGNALS)
    if weak and (verb or tech):
        return "load", "website request"
    if _word(lowered, "app") and not weak:
        return "ambiguous", "ambiguous"
    if weak and not verb and not tech:
        return "ambiguous", "ambiguous"
    return "ambiguous", "ambiguous"


_REDIRECTS = {
    "mobile app": (
        "I only build websites and webapps — a mobile app is out of scope; "
        "ask for a website instead, or brief the mobile app separately."
    ),
    "branding/print": (
        "I only build websites and webapps — branding and print work is out "
        "of scope; ask for a website instead."
    ),
    "general coding": (
        "I only build websites and webapps — general coding is out of scope; "
        "ask for a website instead."
    ),
    "not_admitted": (
        "Web designing is available to admitted users only — ask the owner "
        "for access first."
    ),
    "ambiguous": (
        "Is this a website or webapp you want built (HTML/CSS/JS/TS), or "
        "something else — a mobile app, branding, or general coding?"
    ),
}


def _decline(reason: str, redirect: str) -> dict[str, Any]:
    return {"load": False, "reason": reason, "redirect": redirect, "run": None}


def decide_load(
    text: str,
    *,
    admitted: bool,
    home: str,
    thread_id: str,
    user_id: str,
    chat_id: str,
) -> dict[str, Any]:
    """Stage 1 decision. On load the run is created, started, and stage
    ``load_gate`` is checkpointed; the record then points at ``intake``."""
    if not admitted:
        return _decline("not_admitted", _REDIRECTS["not_admitted"])
    decision, reason = _classify(text)
    if decision == "out_of_scope":
        return _decline("out_of_scope", _REDIRECTS[reason])
    if decision == "ambiguous":
        return _decline("ambiguous", _REDIRECTS["ambiguous"])
    active = store.active_run_for(home, thread_id)
    if active is not None:
        return _decline(
            "active_run",
            f"A web-design run ({active.run_id}) is already active on this "
            "thread — finish or stop it before starting another.",
        )
    try:
        record = store.create_run(
            home=home, thread_id=thread_id, user_id=user_id, chat_id=chat_id,
        )
    except OSError as exc:
        return _decline(
            "workspace_error",
            f"Could not create the web-design workspace: {exc}",
        )
    store.start_run(record)
    store.complete_stage(
        record, "load_gate",
        data={"decision": "load", "reason": reason, "request": (text or "")[:500]},
    )
    record.stage = "intake"
    store.save_run(record)
    return {"load": True, "reason": reason, "redirect": None, "run": record}


__all__ = ["decide_load"]
