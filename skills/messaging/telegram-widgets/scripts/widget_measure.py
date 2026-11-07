#!/usr/bin/env python3
"""Measure a Telegram widget like the server does — local only, no network.

Port of ``measure()`` from serejaris/telegram-skills ``tg-widgets``. The
``send()`` half of the source tool is deliberately NOT ported: it POSTed to
the Bot API, which Hermes never uses. Sending a widget is the
``telegram_client`` ``widget_send`` action, called exactly once and never
retried — a timeout does not prove the message was not delivered.

Limits (from the widget guides): raw HTML at most 32,768 bytes; zlib level 9
at most 4,096 bytes. A widget over 3,800 bytes compressed is reported as
``near_limit`` so the author can trim before it tips over.
"""
import argparse
import json
import sys
import zlib

MAX_HTML = 32768
MAX_ZLIB = 4096
TARGET_ZLIB = 3800


def measure(data: bytes) -> dict:
    """Return ``{"html_bytes", "zlib_bytes", "verdict"}`` for raw HTML bytes."""
    packed = len(zlib.compress(data, 9))
    if len(data) > MAX_HTML or packed > MAX_ZLIB:
        verdict = "too_large"
    elif packed > TARGET_ZLIB:
        verdict = "near_limit"
    else:
        verdict = "ok"
    return {"html_bytes": len(data), "zlib_bytes": packed, "verdict": verdict}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="widget HTML file to measure")
    args = parser.parse_args()

    with open(args.file, "rb") as handle:
        data = handle.read()
    result = measure(data)
    print(json.dumps(result))
    return 1 if result["verdict"] == "too_large" else 0


if __name__ == "__main__":
    sys.exit(main())
