#!/usr/bin/env python3
"""Deterministic score AGGREGATION (Acceptance Gate T2).

The five per-dimension scores (track_fit/timing/feasibility/competition/executability, each
0-100) are PROPOSED upstream by the pinned, temperature-0 LLM judge with anchored 1/3/5 rubric
samples (that step lives in SKILL.md, outside this deterministic boundary). THIS file is the
pure aggregation function:

    FinalScore = (Σ wᵢ·dᵢ) × Confidence(n_sources) × Freshness(age) × track_weight_norm

It is a pure function of (breakdown, n_sources, age_hours, velocity, config) → byte-identical
across runs, and carries two monotonic invariants the gate asserts:
  * more independent sources  ⇒  confidence non-decreasing  ⇒  final non-decreasing (others fixed)
  * staler (larger age)       ⇒  freshness non-increasing   ⇒  final non-increasing (others fixed)

Effort is NOT a denominator (anti-pattern: small-divisor score explosions). Executability is a
positive dimension instead.
"""
from __future__ import annotations

import json
import sys

from lib import confidence, freshness, load_config

_DIMS = ("track_fit", "timing", "feasibility", "competition", "executability")


def _norm_weights(weights: dict) -> dict:
    w = {d: float(weights.get(d, 0.0)) for d in _DIMS}
    total = sum(w.values()) or 1.0
    return {d: w[d] / total for d in _DIMS}


def grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 55:
        return "C+"
    if score >= 45:
        return "C"
    return "D"


def score_opportunity(breakdown: dict, n_sources: int, age_h: float,
                      velocity: float | None = None, track_weight: float = 1.0,
                      cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    w = _norm_weights(sc["weights"])

    dims = {d: max(0.0, min(100.0, float(breakdown.get(d, 0)))) for d in _DIMS}
    raw = sum(w[d] * dims[d] for d in _DIMS)  # 0..100

    conf = confidence(n_sources, sc.get("min_independent_sources", 2))
    fr = freshness(age_h, sc.get("freshness_half_life_h", 72),
                   sc.get("freshness_gravity", 1.8))

    # Velocity boost: a still-heating trend resists the freshness decay (avoid killing real
    # trends). Bounded, deterministic. velocity is a normalized rate in roughly [-1,1].
    if velocity is not None:
        fr = round(min(1.0, fr * (1.0 + 0.15 * max(0.0, float(velocity)))), 6)

    # track weight folded in at HALF strength + clamped, so a watchlist preference nudges
    # ranking without ever dominating the evidence-driven score (e.g. 1.3 -> effective 1.15).
    tw_clamped = max(0.5, min(1.5, float(track_weight)))
    tw = 1.0 + (tw_clamped - 1.0) * 0.5
    final = raw * conf * fr * tw
    final = round(max(0.0, min(100.0, final)), 4)

    return {
        "score_breakdown": {d: round(dims[d], 2) for d in _DIMS},
        "weights": w,
        "raw_score": round(raw, 4),
        "confidence": conf,
        "freshness": fr,
        "track_weight": tw,
        "final_score": final,
        "grade": grade(final),
    }


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    out = score_opportunity(
        data.get("score_breakdown", {}),
        int(data.get("independent_source_count", data.get("n_sources", 0))),
        float(data.get("age_hours", 0.0)),
        data.get("velocity"),
        float(data.get("track_weight", 1.0)),
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
