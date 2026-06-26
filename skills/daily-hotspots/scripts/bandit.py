#!/usr/bin/env python3
"""Deterministic Thompson-sampling track bandit — exploration-exploitation balance (R6).

ARCHITECTURE §8.3 gives each track a STATIC `weight` — pure exploitation: a high-weight track
(ai-agents=1.3) always dominates the feed, an under-explored track that might be quietly producing
good opportunities never gets a turn, and a track that was over-weighted but has gone cold keeps
topping it. There is no exploration-exploitation balance and the preference never adapts to realized
outcomes. ROADMAP R6 = a multi-armed bandit (Thompson sampling) over tracks: a Beta-Bernoulli
posterior per track learned from reward (did opportunities in this track survive the gate / get
pushed?), drawn each run to produce a BOUNDED exploration-adjusted track weight that feeds the
existing `score_opportunity(track_weight=...)` seam (which already folds it in at HALF strength,
clamped) — so the bandit nudges ranking toward promising-but-under-sampled tracks without ever
dominating the evidence-driven score.

Determinism is non-negotiable (the whole suite byte-compares outputs): every draw is seeded
(`random.Random(per-arm seed)`), so the same (posterior, seed) is byte-identical and replay-safe —
a deterministic Thompson sampler, not wall-clock randomness. Everything here is a PURE function (no
clock, no network, no mutation of inputs); wiring the per-run seed + reward feedback into run.py is
the orchestration seam, deliberately kept OUT of this deterministic boundary (like
`catch_up_digests` in R5).
"""
from __future__ import annotations

import hashlib
import json
import random
import sys

from lib import load_config

_DIMS_FLOOR = 1e-9  # Beta params must stay > 0 for betavariate


def _bandit_cfg(cfg: dict | None) -> dict:
    cfg = cfg or load_config()
    b = (cfg.get("scoring", {}) or {}).get("bandit", {}) or {}
    return {
        "prior_alpha": float(b.get("prior_alpha", 1.0)),
        "prior_beta": float(b.get("prior_beta", 1.0)),
        "explore_weight_lo": float(b.get("explore_weight_lo", 0.5)),
        "explore_weight_hi": float(b.get("explore_weight_hi", 1.5)),
        "reward_pushed": float(b.get("reward_pushed", 1.0)),
        "reward_archived": float(b.get("reward_archived", 0.6)),
        "reward_blocked": float(b.get("reward_blocked", 0.0)),
    }


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def init_arm(cfg: dict | None = None) -> dict:
    """Cold-start arm at the configured prior (Beta(prior_alpha, prior_beta), default uniform 1,1)."""
    bc = _bandit_cfg(cfg)
    return {"alpha": bc["prior_alpha"], "beta": bc["prior_beta"], "n": 0}


def _read_arm(arms: dict, track: str, cfg: dict | None) -> dict:
    """Defensive read of one arm (never mutates `arms`); unseen track => cold-start prior."""
    a = (arms or {}).get(track)
    if not a:
        return init_arm(cfg)
    bc = _bandit_cfg(cfg)
    return {"alpha": float(a.get("alpha", bc["prior_alpha"])),
            "beta": float(a.get("beta", bc["prior_beta"])),
            "n": int(a.get("n", 0))}


def reward_clamp(reward) -> float:
    """Bernoulli-style reward clamped to [0,1] (robust to a bad/oob upstream signal)."""
    try:
        return _clamp(float(reward), 0.0, 1.0)
    except Exception:
        return 0.0


def update_arm(arm: dict, reward, cfg: dict | None = None) -> dict:
    """Beta-Bernoulli posterior update. PURE: returns a NEW arm, never mutates the input
    (replay-safe). reward is clamped to [0,1]; alpha += reward, beta += (1-reward), n += 1."""
    bc = _bandit_cfg(cfg)
    r = reward_clamp(reward)
    a = float((arm or {}).get("alpha", bc["prior_alpha"]))
    b = float((arm or {}).get("beta", bc["prior_beta"]))
    n = int((arm or {}).get("n", 0))
    return {"alpha": a + r, "beta": b + (1.0 - r), "n": n + 1}


def posterior_mean(arm: dict) -> float:
    a = float((arm or {}).get("alpha", 1.0))
    b = float((arm or {}).get("beta", 1.0))
    s = a + b
    return a / s if s > 0 else 0.5


def posterior_variance(arm: dict) -> float:
    """Beta posterior variance = ab / ((a+b)^2 (a+b+1)); shrinks monotonically as evidence (n) grows
    for a fixed mean — the formal 'exploration uncertainty' that Thompson sampling rides on."""
    a = float((arm or {}).get("alpha", 1.0))
    b = float((arm or {}).get("beta", 1.0))
    s = a + b
    if s <= 0:
        return 0.0
    return (a * b) / (s * s * (s + 1.0))


def _arm_seed(seed: int, track: str) -> int:
    """Per-arm deterministic seed, independent of arm ordering (so {A,B} and {B,A} draw identically)."""
    h = hashlib.md5(f"{int(seed)}::{track}".encode("utf-8")).digest()[:8]
    return int.from_bytes(h, "big")


def _draw(arm: dict, seed: int, track: str) -> float:
    rng = random.Random(_arm_seed(seed, track))
    return rng.betavariate(max(_DIMS_FLOOR, float(arm["alpha"])),
                           max(_DIMS_FLOOR, float(arm["beta"])))


def thompson_sample(arms: dict, tracks: list | None = None, seed: int = 0,
                    cfg: dict | None = None) -> dict:
    """Deterministic Thompson draw per track: theta ~ Beta(alpha,beta) under random.Random(per-arm
    seed). Same (arms, tracks, seed) => byte-identical {track: theta in (0,1)} (replay-safe).
    Unknown tracks fall back to the cold-start prior (uniform => wide, naturally explored)."""
    if tracks is None:
        tracks = sorted((arms or {}).keys())
    return {t: _draw(_read_arm(arms, t, cfg), int(seed), t) for t in tracks}


def select_track(arms: dict, tracks: list, seed: int = 0, cfg: dict | None = None) -> str:
    """Pick the arm with the highest Thompson draw this round (argmax theta), deterministic tie-break
    by track id ascending (replay-safe). This is the 'which track do we lean into today' selector;
    a high-mean arm usually wins (exploit) while a wide under-pulled arm sometimes does (explore)."""
    samples = thompson_sample(arms, list(tracks), int(seed), cfg)
    return min(samples.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]


def explore_weight(arms: dict, track: str, seed: int = 0, cfg: dict | None = None) -> float:
    """Bounded exploration-adjusted track weight from one Thompson draw, mapped into config
    [explore_weight_lo, explore_weight_hi] and clamped. This is the value to hand to
    score_opportunity(track_weight=...); score.py then folds it in at half strength + re-clamps, so
    the bandit can never dominate the evidence-driven score. Cold-start (no history) draws from the
    uniform prior and is still in bounds (no NaN, no exception)."""
    bc = _bandit_cfg(cfg)
    lo, hi = bc["explore_weight_lo"], bc["explore_weight_hi"]
    if hi < lo:
        lo, hi = hi, lo
    theta = _draw(_read_arm(arms, track, cfg), int(seed), track)
    return round(_clamp(lo + theta * (hi - lo), lo, hi), 6)


def outcome_reward(card: dict, cfg: dict | None = None) -> float:
    """Map a scored opportunity's realized outcome to a Bernoulli reward in [0,1] (deterministic):
    blocked/excluded => low, pushed => high, archived-only => mid; else a score-derived reward that
    is monotone non-decreasing in final_score. Always clamped to [0,1]."""
    bc = _bandit_cfg(cfg)
    card = card or {}
    if card.get("blocked") or card.get("excluded"):
        return reward_clamp(bc["reward_blocked"])
    if card.get("pushed"):
        return reward_clamp(bc["reward_pushed"])
    if card.get("archived"):
        return reward_clamp(bc["reward_archived"])
    fs = card.get("final_score")
    if fs is not None:
        return reward_clamp(float(fs) / 100.0)
    return reward_clamp(bc["reward_blocked"])


def serialize_arms(arms: dict | None) -> dict:
    """JSON-safe, deterministic snapshot of the per-track posterior for persistence (R6 loop close).
    Sorted keys (replay-safe byte-identical), only {alpha,beta,n} kept, all plain float/int."""
    out = {}
    for track in sorted((arms or {}).keys()):
        a = (arms or {}).get(track) or {}
        out[str(track)] = {"alpha": float(a.get("alpha", 1.0)),
                           "beta": float(a.get("beta", 1.0)),
                           "n": int(a.get("n", 0))}
    return out


def deserialize_arms(obj, cfg: dict | None = None) -> dict:
    """DEFENSIVE load of persisted arm state: stored values are untrusted across runs, so a corrupt
    posterior (negative/zero/NaN alpha or beta, non-numeric, bad shape) is clamped back to a VALID
    Beta arm (params > 0) or the cold-start prior — a bad row can never crash scoring or produce a
    NaN draw. Non-dict input / junk entries are dropped. Keeps only {alpha,beta,n}."""
    bc = _bandit_cfg(cfg)
    if not isinstance(obj, dict):
        return {}
    out = {}
    for track, a in obj.items():
        if not isinstance(track, str) or not isinstance(a, dict):
            continue
        try:
            alpha = float(a.get("alpha", bc["prior_alpha"]))
        except (TypeError, ValueError):
            alpha = bc["prior_alpha"]
        try:
            beta = float(a.get("beta", bc["prior_beta"]))
        except (TypeError, ValueError):
            beta = bc["prior_beta"]
        try:
            n = int(a.get("n", 0))
        except (TypeError, ValueError):
            n = 0
        alpha = alpha if alpha == alpha else bc["prior_alpha"]   # NaN guard
        beta = beta if beta == beta else bc["prior_beta"]
        out[track] = {"alpha": max(_DIMS_FLOOR, alpha),
                      "beta": max(_DIMS_FLOOR, beta),
                      "n": max(0, n)}
    return out


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    arms = data.get("arms", {})
    tracks = data.get("tracks")
    seed = int(data.get("seed", 0))
    out = {
        "thompson": thompson_sample(arms, tracks, seed),
        "selected": select_track(arms, tracks or sorted(arms.keys()), seed) if (tracks or arms) else None,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
