#!/usr/bin/env python3
"""Deterministic verify gate (Acceptance Gate T9 schema + T6 low-quality filter).

LLM proposes, this gate disposes, fail-closed, final veto. A card is BLOCKED (never pushed,
never archived) unless EVERY required field is present and well-formed:

  * track (non-null, from enum)              * final_score in [0,100]
  * 5 score_breakdown dims, each 0-100       * why_now (non-empty)
  * >=2 evidence units, each {url, source, ts}   * action (non-empty)
  * independent_source_count >= min_independent_sources (the >=2 red line)

Missing/short = explicit gap returned, not a silent pass. Used both per-card (validate_card) and
as the batch quality filter (filter_pushable: only >= threshold, never filler).
"""
from __future__ import annotations

import json
import sys

from lib import community_pulse_eligible, load_config

_DIMS = ("track_fit", "timing", "feasibility", "competition", "executability")

# Dual-track routing bucket names (design §7), surfaced as constants so callers never string-match.
COMMUNITY_PULSE = "community_pulse"
BELOW_SOURCES = "below_sources"


def route_below_gate(card: dict, cfg: dict | None = None) -> str:
    """Route a candidate that FAILED the >=2-independent-source red line (design §7).

    Returns ``COMMUNITY_PULSE`` when it is a fresh, track-relevant, community-sourced single-origin
    signal, surfaced as a lightweight rumor (Track 2) rather than lost, else ``BELOW_SOURCES``, an
    honest coverage gap that is reported, never silently dropped. Track 1 (>=2 origins + score gate)
    is decided by gate_batch and is unchanged. The predicate itself lives in lib
    (community_pulse_eligible); this is the named routing seam run.py wires."""
    cfg = cfg or load_config()
    return COMMUNITY_PULSE if community_pulse_eligible(card, cfg) else BELOW_SOURCES


def validate_card(card: dict, cfg: dict | None = None) -> tuple[bool, list[str]]:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    errs = []

    if not card.get("track"):
        errs.append("missing track")
    bd = card.get("score_breakdown") or {}
    for d in _DIMS:
        if d not in bd:
            errs.append(f"missing score_breakdown.{d}")
        else:
            try:
                v = float(bd[d])
                if not (0 <= v <= 100):
                    errs.append(f"score_breakdown.{d} out of [0,100]: {v}")
            except (TypeError, ValueError):
                errs.append(f"score_breakdown.{d} not numeric")

    try:
        fs = float(card.get("final_score"))
        if not (0 <= fs <= 100):
            errs.append(f"final_score out of [0,100]: {fs}")
    except (TypeError, ValueError):
        errs.append("final_score missing/non-numeric")

    ev = card.get("evidence") or []
    valid_ev = [e for e in ev if e.get("url") and e.get("source") and e.get("ts")]
    if len(valid_ev) < 2:
        errs.append(f"need >=2 well-formed evidence{{url,source,ts}}, have {len(valid_ev)}")

    isc = int(card.get("independent_source_count", 0) or 0)
    min_src = int(sc.get("min_independent_sources", 2))
    if isc < min_src:
        errs.append(f"independent_source_count {isc} < {min_src} (red line)")

    if not (card.get("why_now") or "").strip():
        errs.append("missing why_now")
    if not (card.get("action") or "").strip():
        errs.append("missing action")

    return (len(errs) == 0, errs)


def gate_batch(cards: list[dict], cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    passed, blocked = [], []
    for c in cards:
        ok, errs = validate_card(c, cfg)
        if ok:
            passed.append(c)
        else:
            blocked.append({"title": c.get("title", "?"), "errors": errs})

    min_push = float(sc.get("min_score_to_push", 70))
    min_arch = float(sc.get("min_score_to_archive", 55))
    max_push = int(cfg.get("push", {}).get("max_per_day", 5))

    # never filler: only items that clear the score floor are pushable/archivable
    pushable = sorted([c for c in passed if float(c.get("final_score", 0)) >= min_push],
                      key=lambda c: -float(c.get("final_score", 0)))[:max_push]
    archivable = [c for c in passed if float(c.get("final_score", 0)) >= min_arch]
    digest_only = [c for c in archivable if c not in pushable]

    return {
        "passed": passed, "blocked": blocked,
        "pushable": pushable, "archivable": archivable, "digest_only": digest_only,
        "empty_day": len(archivable) == 0,
    }


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    if isinstance(data, list):
        print(json.dumps(gate_batch(data), ensure_ascii=False))
    else:
        ok, errs = validate_card(data)
        print(json.dumps({"ok": ok, "errors": errs}, ensure_ascii=False))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
