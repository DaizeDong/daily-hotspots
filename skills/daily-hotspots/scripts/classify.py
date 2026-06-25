#!/usr/bin/env python3
"""Deterministic two-axis classifier (Acceptance Gate T1).

Axis 1 = track (single, from the config enum) — chosen by keyword hit count, ties broken by
config order then track weight, so the SAME input always yields the SAME label (byte-identical).
Axis 2 = machine_type (multi) + focus_tags — keyword rules over the config enums.

NO free-form LLM category invention (anti-pattern #4): the enum is frozen in config; a new
category requires a schema_version bump. This keeps cross-day ranking comparable.
"""
from __future__ import annotations

import json
import sys

from lib import load_config, slug

# machine-type signal keywords (frozen rules; enum lives in config.machine_types)
_TYPE_RULES = {
    "tool-saas": ["saas", "tool", "platform", "dashboard", "api", "sdk", "app"],
    "marketplace": ["marketplace", "market", "directory", "aggregator", "two-sided"],
    "media": ["newsletter", "content", "media", "blog", "video", "podcast", "creator"],
    "service": ["agency", "service", "consult", "done-for-you", "managed"],
    "hardware": ["hardware", "device", "sensor", "robot", "wearable", "chip"],
    "arbitrage": ["arbitrage", "resell", "spread", "underpriced", "mispriced", "broker"],
    "oss-monetization": ["open source", "open-source", "oss", "self-host", "license",
                         "managed hosting"],
}


def _count_hits(haystack: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw and kw.lower() in haystack)


def classify(title: str, text: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    hay = ((title or "") + " \n " + (text or "")).lower()

    # ---- exclude mute (hard) ----
    for bad in cfg.get("exclude", []):
        if bad and bad.lower() in hay:
            return {"track": None, "excluded": True, "exclude_reason": bad,
                    "machine_type": [], "focus_tags": []}

    # ---- axis 1: track (single) ----
    tracks = [t for t in cfg.get("tracks", []) if t.get("enabled", True)]
    best = None
    best_key = None  # (hits, weight, -order) maximize
    for order, t in enumerate(tracks):
        hits = _count_hits(hay, t.get("keywords", []))
        key = (hits, float(t.get("weight", 1.0)), -order)
        if hits > 0 and (best_key is None or key > best_key):
            best_key, best = key, t
    track = best["id"] if best else (tracks[0]["id"] if tracks else "unclassified")
    track_matched = best is not None

    # ---- axis 2: machine_type (multi) ----
    allowed_types = set(cfg.get("machine_types", []))
    mtypes = [name for name, kws in _TYPE_RULES.items()
              if name in allowed_types and _count_hits(hay, kws) > 0]
    if not mtypes:
        mtypes = ["tool-saas"]  # safe default; deterministic

    # ---- focus tags ----
    focus = [ft for ft in cfg.get("focus_topics", [])
             if any(tok in hay for tok in ft.lower().split() if len(tok) > 3)]

    return {
        "track": track,
        "track_matched": track_matched,
        "excluded": False,
        "machine_type": sorted(mtypes),
        "focus_tags": sorted(focus),
    }


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    out = classify(data.get("title", ""), data.get("text", ""))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
