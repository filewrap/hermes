#!/usr/bin/env python3
"""Summarize Hermes model usage windows by provider.

Reads ``hermes usage --json`` documents (one per provider) from ``--input``
files or stdin, or runs ``hermes usage --json`` itself, and prints a compact
summary: most-used window first, with reset times.

Defaults to current mode (the most-used window per provider); ``--mode all``
lists every window. Stdlib only; no live network beyond what ``hermes usage``
itself does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def run_hermes_usage(provider: str | None) -> dict[str, Any]:
    cmd = ["hermes", "usage", "--json"]
    if provider:
        cmd += ["--provider", provider]
    try:
        output = subprocess.check_output(cmd, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError("hermes not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"hermes usage failed (exit {exc.returncode}).") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("hermes usage timed out.") from exc
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse hermes usage JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Expected a hermes usage JSON document (object).")
    return payload


def load_documents(input_paths: list[str] | None, provider: str | None) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    if input_paths:
        for path in input_paths:
            if path == "-":
                raw = sys.stdin.read()
            else:
                with open(path, encoding="utf-8") as handle:
                    raw = handle.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Failed to parse {path!r} as JSON: {exc}") from exc
            if isinstance(data, dict):
                documents.append(data)
            elif isinstance(data, list):
                documents.extend(d for d in data if isinstance(d, dict))
            else:
                raise RuntimeError(f"Unsupported JSON shape in {path!r}.")
    else:
        documents.append(run_hermes_usage(provider))
    if not documents:
        raise RuntimeError("No usage documents to summarize.")
    return documents


def window_lines(doc: dict[str, Any]) -> list[str]:
    provider = str(doc.get("provider") or "unknown")
    plan = doc.get("plan")
    header = f"{provider} ({plan})" if plan else provider
    fetched = doc.get("fetched_at")
    lines = [header + (f" — fetched {fetched}" if fetched else "")]
    windows = [w for w in doc.get("windows") or [] if isinstance(w, dict)]
    windows.sort(key=lambda w: w.get("used_percent") if isinstance(w.get("used_percent"), (int, float)) else -1, reverse=True)
    for window in windows:
        label = str(window.get("label") or "window")
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            base = f"  {label}: unavailable"
        else:
            base = f"  {label}: {used:.0f}% used ({max(0, round(100 - used))}% remaining)"
        if window.get("resets_at"):
            base += f" • resets {window['resets_at']}"
        elif window.get("detail"):
            base += f" • {window['detail']}"
        lines.append(base)
    for detail in doc.get("details") or []:
        lines.append(f"  {detail}")
    if doc.get("unavailable_reason"):
        lines.append(f"  Unavailable: {doc['unavailable_reason']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize hermes usage --json documents by provider.")
    parser.add_argument("--provider", default=None, help="Provider to query when fetching live (default: configured provider)")
    parser.add_argument("--mode", choices=["current", "all"], default="current",
                        help="current: most-used window per provider; all: every window")
    parser.add_argument("--input", dest="inputs", action="append", default=None,
                        help="JSON file with a hermes usage --json document (repeatable); '-' reads stdin")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    try:
        documents = load_documents(args.inputs, args.provider)
    except RuntimeError as exc:
        eprint(f"model_usage: {exc}")
        return 1
    if args.format == "json":
        if args.mode == "current":
            trimmed = []
            for doc in documents:
                doc = dict(doc)
                windows = sorted(
                    (w for w in doc.get("windows") or [] if isinstance(w, dict)),
                    key=lambda w: w.get("used_percent") if isinstance(w.get("used_percent"), (int, float)) else -1,
                    reverse=True,
                )
                doc["windows"] = windows[:1]
                trimmed.append(doc)
            documents = trimmed
        print(json.dumps(documents if len(documents) > 1 else documents[0], indent=2))
        return 0
    out: list[str] = []
    for doc in documents:
        lines = window_lines(doc)
        if args.mode == "current" and len(lines) > 2:
            lines = lines[:2]
        out.extend(lines)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
