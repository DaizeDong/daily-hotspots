#!/usr/bin/env python3
"""Deterministic two-axis classifier (Acceptance Gate T1).

Axis 1 = track (single, from the config enum), chosen by keyword hit count, ties broken by track
WEIGHT and only then by config order: the key is (hits, weight, -order), as reference/scoring.md
specifies. This line used to state the reverse. A self-improvement round, shown this file but not
reference/scoring.md, correctly spotted the contradiction and changed the CODE to match the prose,
which would have silently re-labelled every tied card.
Axis 2 = machine_type (multi) + focus_tags, keyword rules over the config enums.

NO free-form LLM category invention (anti-pattern #4): the enum is frozen in config; a new
category requires a schema_version bump. This keeps cross-day ranking comparable.
"""
from __future__ import annotations

import json
import re
import sys

from lib import load_config, slug

# The track a candidate gets when NO track keyword matched anywhere. It is a REAL enum member
# (lib.DEFAULT_CONFIG carries it at weight 1.0), not a silent re-label: the old fallback returned
# ``tracks[0]``, which is ai-agents, the track carrying the LARGEST weight (1.3), so an item nothing
# could classify was handed the biggest scoring bonus in the system AND keyed under ``::ai-agents``
# for cross-day dedup. That is why 61 of 166 archived cards read ``track: ai-agents``. An
# unclassifiable item now says so. If a config omits the entry, run._track_weight falls back to 1.0,
# i.e. neutral, never a bonus.
UNCLASSIFIED_TRACK = "unclassified"

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


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]")
_KW_CACHE: dict = {}


def _kw_regex(kw: str):
    """Compile ONE keyword into a token-boundary matcher.

    A bare-substring test is wrong for short ASCII keywords and was actively mis-classifying: ``ci``
    (dev-tools) fired inside *social*, *decision*, *specific*; ``api`` (dev-tools) fired inside
    *rapid*, *capital*, *therapist*; ``app`` fired inside *apple* and *happy*. Every phantom hit
    moves a real hit count and can flip the track a card is filed and cross-day deduped under.

    So an ASCII edge gets a word boundary: a keyword whose FIRST character is an ASCII word char must
    not be preceded by one, and likewise for its LAST character. A CJK keyword (Chinese has no
    spaces, so a boundary test would never match) keeps plain substring semantics, because neither
    edge is an ASCII word char. Interior punctuation stays literal, so multi-word (``open source``)
    and hyphenated (``done-for-you``, ``self-host``) keywords keep matching exactly what they say."""
    pat = re.escape(kw)
    if kw and _ASCII_WORD_RE.match(kw[0]):
        pat = r"(?<![A-Za-z0-9_])" + pat
    if kw and _ASCII_WORD_RE.match(kw[-1]):
        pat = pat + r"(?![A-Za-z0-9_])"
    return re.compile(pat)


def keyword_hit(haystack: str, keyword: str) -> bool:
    """True when ``keyword`` occurs in ``haystack`` on token boundaries (see _kw_regex).

    ``haystack`` is expected already lowercased; the keyword is lowercased here. Shared by the
    track / machine-type rules below AND by run.collect_community_source's keep_keywords /
    drop_keywords lane filter, so the two can never drift apart."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    rx = _KW_CACHE.get(kw)
    if rx is None:
        rx = _KW_CACHE[kw] = _kw_regex(kw)
    return bool(rx.search(haystack or ""))


def _count_hits(haystack: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if keyword_hit(haystack, kw))


def check_excluded(title: str, text: str, cfg: dict | None = None) -> str | None:
    """The hard content mute: the FIRST matching ``exclude`` term (the mute reason), or None.

    Split out of classify() so the roster / preset-track lane (run.build_card, §6) enforces the SAME
    exclude list. A preset track (roster identity) carries the TRACK but is NEVER a license to bypass
    the mute list (memecoin / giveaway airdrop / crypto pump / nsfw / mlm): build_card skips classify()
    when a track is preset, so without this the exclude gate was defeated for the whole X-roster lane
    (excluded content could be scored, pushed, and archived)."""
    cfg = cfg or load_config()
    hay = ((title or "") + " \n " + (text or "")).lower()
    # Deliberately a BARE SUBSTRING test, unlike the keyword rules below. The mute list is a
    # safety veto, so over-matching (``memecoins`` muted by ``memecoin``) is the SAFE direction
    # and a token boundary would LOOSEN it. Keep it greedy.
    for bad in cfg.get("exclude", []):
        if bad and bad.lower() in hay:
            return bad
    return None


def classify(title: str, text: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    hay = ((title or "") + " \n " + (text or "")).lower()

    # ---- exclude mute (hard) ----
    bad = check_excluded(title, text, cfg)
    if bad is not None:
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
    # NO fallback to tracks[0] (see UNCLASSIFIED_TRACK): "nothing matched" is said out loud, never
    # silently filed under whichever track happens to sit first in the config.
    track = best["id"] if best else UNCLASSIFIED_TRACK
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
