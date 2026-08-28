#!/usr/bin/env python3
"""Deterministic score AGGREGATION (Acceptance Gate T2).

The five per-dimension scores (track_fit/timing/feasibility/competition/executability, each
0-100) are PROPOSED upstream by the pinned, temperature-0 LLM judge with anchored 1/3/5 rubric
samples (that step lives in SKILL.md, outside this deterministic boundary). THIS file is the
pure aggregation function:

    FinalScore = (Σ wᵢ·dᵢ) × Confidence(n_sources) × Freshness(age, velocity)
                            × track_weight_norm × lifecycle_weight × crowdedness_mult

SIX factors, not four. The docstring claimed four for a long time while the code applied six, and
the two it omitted (lifecycle down to 0.55, crowdedness down to 0.545) were exactly the ones that
silently killed the demand lane, so the omission was not cosmetic. crowdedness_mult is now pinned at
1.0 and the crowd signal is blended into the competition dimension instead; see score_opportunity.

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
                      cfg: dict | None = None, lifecycle_stage: str | None = None,
                      side: str = "supply", crowdedness: float | None = None) -> dict:
    """`side` splits the two-column model. supply (the default, backward compatible) keeps the
    hotness-first weights, a basic-hotspot lane. demand uses a pain-first weight vector (timing
    down, feasibility/competition/executability up). crowdedness in [0,100] is a demand-only input
    (ignored for supply).

    REVISED 2026-08-27, after the demand lane was found to be arithmetically unable to clear its own
    floor. It had shipped 45 days without archiving a single demand card, and the empty column was
    read as an honest quiet day rather than as a broken lane. Measured on the 2026-08-27 candidates,
    the two sides' RAW scores were comparable (demand 65.6..79.3, supply 65.3..83.4) while the finals
    were not (demand 31.0..50.2, supply 55.0..76.2). The entire gap was two multipliers that only
    demand paid, followed by a floor five points HIGHER than supply's:

      * crowdedness was DOUBLE COUNTED. `competition` already carries 0.30 of the demand weight
        vector, the largest single weight on that side, and crowdedness is definitionally the same
        signal ("how many people already propose exactly this"). It was then applied AGAIN as an
        outside multiplier down to 0.545. A signal declared at 0.30 was in fact charged twice.
      * the freshness FLOOR was written to PROTECT a durable pain from news decay, but demand
        evidence is always weeks old, so demand arrived from BELOW the floor every time and a
        protective max() acted as a flat penalty of roughly 40 percent.

    The fix keeps both signals and charges each once, at its declared weight:
      * crowdedness is folded INTO the competition dimension (`crowdedness_blend`), so a red ocean
        still scores worse, once, inside the weighted space where the config says it lives.
      * demand freshness is NEUTRAL (`demand_freshness_mode`). Recency is not deleted; it is carried
        by the judged `timing` dimension at its declared 0.10 demand weight instead of by a
        mechanical half-life that pain evidence can never satisfy.
    The floors are deliberately NOT lowered. Demand keeps the higher archive bar it was always meant
    to carry; that bar is simply reachable now that the two sides are measured on one scale."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    is_demand = str(side).strip().lower() == "demand"
    wsrc = sc.get("demand_weights") if (is_demand and sc.get("demand_weights")) else sc["weights"]
    w = _norm_weights(wsrc)

    dims = {d: max(0.0, min(100.0, float(breakdown.get(d, 0)))) for d in _DIMS}

    # Crowdedness folded into competition (demand only), charged ONCE at the weight the config
    # declares for it. blend=0 keeps the judged dim alone, blend=1 replaces it with the crowd
    # reading; the default splits them, because the judge and the crowd estimate are independent
    # observations of the same thing and neither deserves to be discarded.
    crowd_mode = str(sc.get("crowdedness_mode", "dimension")).strip().lower()
    competition_adjusted = None
    if is_demand and crowdedness is not None and crowd_mode == "dimension":
        blend = max(0.0, min(1.0, float(sc.get("crowdedness_blend", 0.5))))
        cval = max(0.0, min(100.0, float(crowdedness)))
        competition_adjusted = round((1.0 - blend) * dims["competition"] + blend * (100.0 - cval), 4)
        dims["competition"] = competition_adjusted

    raw = sum(w[d] * dims[d] for d in _DIMS)  # 0..100

    conf = confidence(n_sources, sc.get("min_independent_sources", 2))
    fr = freshness(age_h, sc.get("freshness_half_life_h", 72),
                   sc.get("freshness_gravity", 1.8))

    # Velocity adjusts the freshness decay (deterministic, bounded). velocity is a normalized
    # rate in roughly [-1,1]: a still-heating trend (>0) resists decay (anti-pattern: killing a
    # real trend), and a COOLING trend (<0) is penalized, a window that is actively closing is
    # worth less than a flat one (R4; HEAD clamped this to max(0,v) and ignored cooling).
    if velocity is not None:
        fr = round(min(1.0, max(0.0, fr * (1.0 + 0.15 * float(velocity)))), 6)

    # Demand durability: a real unmet pain does not expire on a news half-life, so demand freshness
    # is floored (recency stops burying a durable, still-unsolved need). Supply keeps full decay.
    if is_demand:
        mode = str(sc.get("demand_freshness_mode", "neutral")).strip().lower()
        if mode == "floor":
            # Legacy behavior, kept only so a retune can be compared against it. Demand evidence is
            # older than the half-life essentially always, so this floors from below and is a flat
            # penalty rather than the protection it reads as.
            fr = round(max(float(sc.get("demand_freshness_floor", 0.6)), fr), 6)
        else:
            # NEUTRAL: a durable unmet pain does not expire on a news half-life. Recency for demand
            # is the judged `timing` dimension at its declared 0.10 weight, not a mechanical decay.
            fr = 1.0

    # Lifecycle window-closed downweight (R4): emerging=1.0 .. fading collapses the score, so a
    # closed-window opportunity stops topping the feed (ARCHITECTURE §3.2/§6.3). Unknown/absent
    # stage is neutral (1.0). Multiplier is config-tunable and clamped to a sane floor; it only
    # touches the freshness/timing axis (confidence stays the independent-source signal).
    lw = sc.get("lifecycle_weights", {}) or {}
    sw = float(lw.get((lifecycle_stage or "").strip().lower(), 1.0))
    sw = max(0.3, min(1.0, sw))

    # track weight folded in at HALF strength + clamped, so a watchlist preference nudges
    # ranking without ever dominating the evidence-driven score (e.g. 1.3 -> effective 1.15).
    tw_clamped = max(0.5, min(1.5, float(track_weight)))
    tw = 1.0 + (tw_clamped - 1.0) * 0.5

    # Under the shipped default (crowdedness_mode == "dimension") crowdedness is NOT an outside
    # multiplier any more: it was blended into the competition dimension above, so it is charged
    # exactly once, at the weight the config declares for it. cw stays 1.0 and the key is retained so
    # downstream readers and archived records keep a stable shape.
    # "legacy_multiplier" replays the old double-counting product and exists ONLY so the calibration
    # test can measure new against old on real history. It is not a supported production setting.
    cw = 1.0
    if is_demand and crowdedness is not None and crowd_mode == "legacy_multiplier":
        pen = float(sc.get("crowdedness_penalty", 0.7))
        cval = max(0.0, min(100.0, float(crowdedness)))
        cw = round(max(0.1, 1.0 - pen * cval / 100.0), 6)

    final = raw * conf * fr * tw * sw * cw
    final = round(max(0.0, min(100.0, final)), 4)

    return {
        "score_breakdown": {d: round(dims[d], 2) for d in _DIMS},
        "weights": w,
        "raw_score": round(raw, 4),
        "confidence": conf,
        "freshness": fr,
        "track_weight": tw,
        "lifecycle_stage": (lifecycle_stage or "").strip().lower() or None,
        "lifecycle_weight": sw,
        "side": "demand" if is_demand else "supply",
        "crowdedness": None if crowdedness is None else round(max(0.0, min(100.0, float(crowdedness))), 2),
        "crowdedness_mult": cw,
        "competition_adjusted": competition_adjusted,
        "final_score": final,
        "grade": grade(final),
    }


# --------------------------------------------------------------------------- weight-retuning gate
# ARCHITECTURE §3.3/§8.3: scoring.weights is a live tuning surface that "can be iterated under a
# self-evolve A/B regression gate." Because score_opportunity is a pure function of the PERSISTED
# breakdown, a whole golden set can be re-ranked under any weight vector WITHOUT re-evaluating, so
# the gate is fully deterministic: LLM proposes new weights, this code disposes (auto_pass /
# needs_review / block) by measuring how far the ranking moved. R2.


def _final_map(items: list, weights: dict | None, cfg: dict) -> dict:
    """{id -> final_score} for every item under `weights` (None = use cfg's current weights).
    Reuses the persisted score_breakdown; never mutates the caller's cfg."""
    use = cfg
    if weights is not None:
        use = json.loads(json.dumps(cfg))
        use["scoring"]["weights"] = weights
    out = {}
    for it in items:
        out[it["id"]] = score_opportunity(
            it.get("score_breakdown", {}),
            int(it.get("n_sources", it.get("independent_source_count", 2))),
            float(it.get("age_h", it.get("age_hours", 0.0))),
            it.get("velocity"),
            float(it.get("track_weight", 1.0)),
            use,
            lifecycle_stage=it.get("lifecycle_stage"),
        )["final_score"]
    return out


def rerank(items: list, weights: dict | None = None, cfg: dict | None = None) -> list:
    """Rank opportunity ids by final_score desc under `weights`, deterministic tie-break by id asc
    (replay-safe). Pure: re-ranks historical opportunities from their persisted breakdown."""
    cfg = cfg or load_config()
    fm = _final_map(items, weights, cfg)
    return [i for i, _ in sorted(fm.items(), key=lambda kv: (-kv[1], str(kv[0])))]


def _kendall_tau_distance(order_a: list, order_b: list) -> float:
    """Normalized Kendall tau distance in [0,1]: fraction of discordant pairs. 0 = identical order,
    1 = full reversal."""
    pos = {x: i for i, x in enumerate(order_b)}
    seq = [pos[x] for x in order_a if x in pos]
    n = len(seq)
    if n < 2:
        return 0.0
    disc = sum(1 for i in range(n) for j in range(i + 1, n) if seq[i] > seq[j])
    return round(disc / (n * (n - 1) / 2.0), 6)


def rank_drift(items: list, weights_a: dict | None, weights_b: dict | None,
               cfg: dict | None = None, top_n: int | None = None) -> dict:
    """Deterministic perturbation between two weight vectors over a golden set: Kendall tau distance,
    max rank displacement, push-floor membership churn, and top-N set churn. All bounded [0,1]."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    fa, fb = _final_map(items, weights_a, cfg), _final_map(items, weights_b, cfg)
    oa = [i for i, _ in sorted(fa.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    ob = [i for i, _ in sorted(fb.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    tau = _kendall_tau_distance(oa, ob)
    pos_b = {x: i for i, x in enumerate(ob)}
    max_shift = max((abs(i - pos_b[x]) for i, x in enumerate(oa)), default=0)
    floor = sc.get("min_score_to_push", 70)
    push_a = {i for i, v in fa.items() if v >= floor}
    push_b = {i for i, v in fb.items() if v >= floor}
    churned = sorted(push_a ^ push_b)
    denom = len(items) or 1
    n = top_n or max(1, min(len(items), len(items) // 2))
    top_left = set(oa[:n]) - set(ob[:n])  # how many of the old top-N dropped out (bounded [0,1])
    return {
        "kendall_tau": tau,
        "max_rank_shift": max_shift,
        "push_floor_churn_frac": round(len(churned) / denom, 6),
        "push_floor_churned": churned,
        "top_n": n,
        "top_n_churn_frac": round(len(top_left) / float(n), 6),
    }


def weight_regression_gate(items: list, old_weights: dict | None, new_weights: dict | None,
                           cfg: dict | None = None) -> dict:
    """Deterministic release verdict for a proposed weight retune (LLM proposes, code disposes):
      * auto_pass, rank drift & push-floor churn both within budget
      * needs_review, over budget but not catastrophic (surfaces to human, never silent)
      * block, catastrophic reorder or churn
    Budget (`scoring.weight_regression`) is fully config-tunable; tightening it is never more
    permissive than loosening it (monotone)."""
    cfg = cfg or load_config()
    b = cfg["scoring"].get("weight_regression", {}) or {}
    max_tau = float(b.get("max_tau", 0.25))
    max_churn = float(b.get("max_push_churn_frac", 0.20))
    cat_tau = float(b.get("catastrophic_tau", 0.6))
    cat_churn = float(b.get("catastrophic_churn_frac", 0.5))
    d = rank_drift(items, old_weights, new_weights, cfg)
    tau, churn = d["kendall_tau"], d["push_floor_churn_frac"]
    reasons: list[str] = []
    if tau >= cat_tau:
        reasons.append(f"catastrophic rank reversal: kendall_tau {tau} >= {cat_tau}")
    if churn >= cat_churn:
        reasons.append(f"catastrophic push-floor churn: {churn} >= {cat_churn}")
    if reasons:
        decision = "block"
    else:
        over: list[str] = []
        if tau > max_tau:
            over.append(f"rank drift over budget: kendall_tau {tau} > {max_tau}")
        if churn > max_churn:
            over.append(f"push-floor churn over budget: {churn} > {max_churn}")
        if over:
            decision, reasons = "needs_review", over
        else:
            decision = "auto_pass"
    return {
        "decision": decision,
        "reasons": reasons,
        "metrics": d,
        "budget": {"max_tau": max_tau, "max_push_churn_frac": max_churn,
                   "catastrophic_tau": cat_tau, "catastrophic_churn_frac": cat_churn},
    }


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    out = score_opportunity(
        data.get("score_breakdown", {}),
        int(data.get("independent_source_count", data.get("n_sources", 0))),
        float(data.get("age_hours", 0.0)),
        data.get("velocity"),
        float(data.get("track_weight", 1.0)),
        lifecycle_stage=data.get("lifecycle_stage"),
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
