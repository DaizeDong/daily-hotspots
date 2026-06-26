"""R6 follow-through: the Thompson bandit must actually be WIRED INTO run.py (batch-6 deferred the
orchestration seam). HEAD ships bandit.py as pure functions but run.py scores with the STATIC track
weight and never closes the reward loop. These pin the wiring capability:

  * scoring uses an explore-adjusted (bandit) track weight, bounded, deterministic, opt-in;
  * the reward loop closes — each run emits the next per-track arm learned from realized outcomes;
  * default (no arms) is byte-identical to today; the bandit can never break score bounds.

Capability assertions (not a specific multiplier table). Marked xfail(strict=False) so the green
baseline stays green until the wiring lands.
"""
import pytest

import run as runner
from lib import load_config

CFG = load_config()
# Headroom landed (self-evolve gate ACCEPT e=129.27, +12, 0 regressed): these are now permanent
# regression guards on the bandit->run.py wiring.


def _cand(title="MCP agent framework launch", track="ai-agents", timing=95, sources=None):
    sources = sources or ["hackernews", "product-hunt"]
    return {
        "title": title, "summary": "open source llm agent tooling", "track": track,
        "entities": (title + " open source llm agent").lower().split(),
        "evidence": [{"source": s, "origin": s + ".com", "url": "http://" + s + "/x",
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in sources],
        "score_breakdown": {"track_fit": 85, "timing": timing, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


def _hi_arm():   # well-performing track: theta ~0.98 deterministically
    return {"ai-agents": {"alpha": 50.0, "beta": 1.0, "n": 51}}


def _lo_arm():   # cold / under-performing track: theta ~0.02
    return {"ai-agents": {"alpha": 1.0, "beta": 50.0, "n": 51}}


# 1 — capability surface exists
def test_capability_surface_exists():
    assert hasattr(runner, "effective_track_weight")
    res = runner.process([_cand()], CFG, ledger=None, dry_run=True,
                         bandit_arms=_hi_arm(), bandit_seed=7)
    assert "bandit_arms_next" in res and isinstance(res["bandit_arms_next"], dict)


# 2 — default (no arms) is byte-identical to the static-weight score
def test_no_arms_is_byte_identical_to_static():
    static = runner.build_card(_cand(), CFG, "r")
    none_arms = runner.build_card(_cand(), CFG, "r", arms=None, seed=0)
    assert none_arms["final_score"] == static["final_score"]
    assert none_arms["raw_score"] == static["raw_score"]


# 3 — a well-performing arm lifts its track's score above the static baseline
def test_high_mean_arm_lifts_score():
    static = runner.build_card(_cand(), CFG, "r")["final_score"]
    hi = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=7)["final_score"]
    assert hi > static


# 4 — an under-performing arm dampens its track's score below the static baseline
def test_low_mean_arm_dampens_score():
    static = runner.build_card(_cand(), CFG, "r")["final_score"]
    lo = runner.build_card(_cand(), CFG, "r", arms=_lo_arm(), seed=7)["final_score"]
    assert lo < static


# 5 — the explore-adjusted weight is BOUNDED to [0.5*static, 1.5*static] for any arm/seed
def test_effective_weight_bounded():
    static = runner.effective_track_weight("ai-agents", CFG)
    for seed in range(20):
        for arms in (_hi_arm(), _lo_arm(), {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}):
            w = runner.effective_track_weight("ai-agents", CFG, arms, seed)
            assert 0.5 * static - 1e-6 <= w <= 1.5 * static + 1e-6


# 6 — deterministic / replay-safe: same (arms, seed) => byte-identical score
def test_determinism_replay():
    a = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=13)["final_score"]
    b = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=13)["final_score"]
    assert a == b


# 7 — reward loop closes: a PUSHED card's track arm gains evidence (alpha up, n+1)
def test_reward_loop_pushed_updates_arm():
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    res = runner.process([_cand(timing=98)], CFG, ledger=None, dry_run=True,
                         bandit_arms=arms, bandit_seed=1)
    assert res["pushed"], "fixture must push so there is a positive outcome to learn from"
    nxt = res["bandit_arms_next"]["ai-agents"]
    assert nxt["alpha"] > arms["ai-agents"]["alpha"]
    assert nxt["n"] == arms["ai-agents"]["n"] + 1


# 8 — input arms are never mutated (PURE feedback)
def test_input_arms_not_mutated():
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    snapshot = dict(arms["ai-agents"])
    runner.process([_cand()], CFG, ledger=None, dry_run=True, bandit_arms=arms, bandit_seed=1)
    assert arms["ai-agents"] == snapshot


# 9 — only real outcomes teach the bandit: a run with NO actionable card leaves arms unchanged
def test_no_actionable_no_learning():
    arms = {"ai-agents": {"alpha": 3.0, "beta": 2.0, "n": 5}}
    # single-source candidate is gated out at the red line => not actionable
    weak = _cand(sources=["hackernews"])
    res = runner.process([weak], CFG, ledger=None, dry_run=True, bandit_arms=arms, bandit_seed=1)
    assert res["below_sources"]
    assert res["bandit_arms_next"]["ai-agents"] == arms["ai-agents"]


# 10 — exploration actually varies with the seed (not a constant greedy pick)
def test_exploration_varies_with_seed():
    cold = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    vals = {runner.effective_track_weight("ai-agents", CFG, cold, s) for s in range(30)}
    assert len(vals) > 1, "a cold (uniform) arm must explore a range, not return one constant"


# 11 — boundedness end-to-end: even an extreme arm keeps final_score in [0,100]
def test_score_bounds_hold_under_extreme_arm():
    extreme = {"ai-agents": {"alpha": 1e6, "beta": 1.0, "n": 1}}
    fs = runner.build_card(_cand(), CFG, "r", arms=extreme, seed=3)["final_score"]
    assert 0.0 <= fs <= 100.0


# 12 — flows through the real score_opportunity(track_weight=) seam => monotone in arm quality
def test_monotone_in_arm_quality():
    lo = runner.build_card(_cand(), CFG, "r", arms=_lo_arm(), seed=5)["final_score"]
    hi = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=5)["final_score"]
    assert hi >= lo
