"""R2 headroom: weight-retuning regression gate (Acceptance Gate T2 extension).

ARCHITECTURE §3.3 / §8.3 make `scoring.weights` a live, git-diffable tuning surface and explicitly
promise it can be "iterated under a self-evolve A/B regression gate" — re-weighting must re-RANK the
golden set in a *bounded, explainable* way, never silently scramble the feed. §3.3 also pins a
golden-set drift monitor (">1 grade drift => pause"). HEAD has the re-scoring math (`score_opportunity`
is a pure function of the persisted breakdown, so re-ranking without re-eval already works) but has NO
regression gate: nothing measures how much a weight change perturbs the ranking, and nothing decides
auto_pass / needs_review / block. A reckless weight edit can therefore ship a fully reordered feed with
zero guardrail (anti-pattern §10.4 LLM-vibes scoring with no deterministic gate; the skill creed is
"LLM proposes weights, a deterministic gate disposes").

These assert the *capability* (a deterministic rank-drift metric + a config-tunable release gate),
not any particular tolerance table. They are the canonical "LLM proposes, code adjudicates" shape: a
proposer suggests new weights, this gate — pure code — rules whether the retune is safe to land.

Imports are lazy (inside each test) so on a baseline lacking the new symbols each case xfails
individually rather than erroring at collection; the fix flips each XFAIL -> XPASS.
"""
import copy

import pytest

from lib import load_config

# R2 headroom: these assert a capability HEAD lacks (the weight-regression gate). Module-level xfail
# keeps the baseline green; the fix flips each to XPASS, after which the marker is removed so they
# stand as permanent regression guards.
pytestmark = pytest.mark.xfail(strict=False,
                               reason="R2 headroom: weight-retuning regression gate not yet built")

CFG = load_config()
PUSH = CFG["scoring"]["min_score_to_push"]

# A small golden set: each item carries only the PERSISTED score_breakdown (+ context), so it can be
# re-ranked under any weight vector without re-evaluating — exactly the §3.3 design. Items are crafted
# so different dimensions dominate different items, making reweighting actually move the ranking.
GOLDEN = [
    {"id": "op-timing",  "score_breakdown": {"track_fit": 50, "timing": 95, "feasibility": 50,
                                             "competition": 50, "executability": 50}},
    {"id": "op-track",   "score_breakdown": {"track_fit": 95, "timing": 50, "feasibility": 50,
                                             "competition": 50, "executability": 50}},
    {"id": "op-feas",    "score_breakdown": {"track_fit": 50, "timing": 50, "feasibility": 95,
                                             "competition": 50, "executability": 50}},
    {"id": "op-compete", "score_breakdown": {"track_fit": 50, "timing": 50, "feasibility": 50,
                                             "competition": 95, "executability": 50}},
    {"id": "op-exec",    "score_breakdown": {"track_fit": 50, "timing": 50, "feasibility": 50,
                                             "competition": 50, "executability": 95}},
    {"id": "op-flat",    "score_breakdown": {"track_fit": 62, "timing": 62, "feasibility": 62,
                                             "competition": 62, "executability": 62}},
]
for _it in GOLDEN:           # uniform fresh, 2-source context so weights drive the ranking
    _it.update(n_sources=2, age_h=4.0, track_weight=1.0)

BASE_W = dict(CFG["scoring"]["weights"])  # the current production weight vector


def _rank(): from score import rerank; return rerank
def _drift(): from score import rank_drift; return rank_drift
def _gate(): from score import weight_regression_gate; return weight_regression_gate


# --------------------------------------------------------------------- capability + determinism
def test_capability_exists():
    from score import rerank, rank_drift, weight_regression_gate
    assert callable(rerank) and callable(rank_drift) and callable(weight_regression_gate)


def test_rerank_deterministic():
    rerank = _rank()
    a = rerank(GOLDEN, BASE_W, CFG)
    b = rerank(GOLDEN, BASE_W, CFG)
    c = rerank(GOLDEN, BASE_W, CFG)
    assert a == b == c
    assert set(a) == {it["id"] for it in GOLDEN}  # total, no drops/dupes


def test_rerank_tiebreak_by_id():
    # identical breakdowns => identical final => deterministic id-ascending tie-break (replay-safe).
    rerank = _rank()
    same = [{"id": f"op-{c}", "score_breakdown": {d: 60 for d in
            ("track_fit", "timing", "feasibility", "competition", "executability")},
            "n_sources": 2, "age_h": 4.0} for c in ("z", "a", "m")]
    assert rerank(same, BASE_W, CFG) == ["op-a", "op-m", "op-z"]


# --------------------------------------------------------------------- drift metric
def test_noop_zero_drift_autopass():
    drift, gate = _drift(), _gate()
    d = drift(GOLDEN, BASE_W, BASE_W, CFG)
    assert d["kendall_tau"] == 0.0
    assert d["push_floor_churn_frac"] == 0.0
    assert gate(GOLDEN, BASE_W, BASE_W, CFG)["decision"] == "auto_pass"


def test_metrics_bounded():
    drift = _drift()
    rev = {"track_fit": 0.05, "timing": 0.05, "feasibility": 0.05,
           "competition": 0.05, "executability": 0.80}
    for wb in (BASE_W, rev, {"track_fit": 1, "timing": 0, "feasibility": 0,
                             "competition": 0, "executability": 0}):
        d = drift(GOLDEN, BASE_W, wb, CFG)
        assert 0.0 <= d["kendall_tau"] <= 1.0
        assert 0.0 <= d["push_floor_churn_frac"] <= 1.0
        assert 0.0 <= d["top_n_churn_frac"] <= 1.0


def test_full_reversal_high_tau():
    # weights that invert the dominant ordering must register near-maximal kendall tau distance.
    drift = _drift()
    base = {"track_fit": 0.8, "timing": 0.05, "feasibility": 0.05,
            "competition": 0.05, "executability": 0.05}
    inv = {"track_fit": 0.05, "timing": 0.05, "feasibility": 0.05,
           "competition": 0.05, "executability": 0.8}
    d_same = drift(GOLDEN, base, base, CFG)
    d_big = drift(GOLDEN, base, inv, CFG)
    assert d_big["kendall_tau"] > d_same["kendall_tau"]
    assert d_big["kendall_tau"] >= 0.4  # a real reorder, not noise


# --------------------------------------------------------------------- gate decisions
def test_mild_reweight_within_budget_autopass():
    gate = _gate()
    mild = dict(BASE_W)
    mild["timing"] = round(mild["timing"] + 0.02, 4)
    mild["competition"] = round(mild["competition"] - 0.02, 4)
    v = gate(GOLDEN, BASE_W, mild, CFG)
    assert v["decision"] == "auto_pass", v


def test_catastrophic_reweight_not_autopass():
    # a violent retune (all weight onto one dim) must NOT silently auto-pass.
    gate = _gate()
    violent = {"track_fit": 0.02, "timing": 0.02, "feasibility": 0.02,
               "competition": 0.02, "executability": 0.92}
    v = gate(GOLDEN, BASE_W, violent, CFG)
    assert v["decision"] in {"needs_review", "block"}, v


def test_full_reversal_blocks():
    # a genuinely anti-correlated set: track_fit rises as executability falls, so weighting one vs
    # the other reverses the order exactly (kendall_tau -> 1.0) and the gate must BLOCK.
    gate = _gate()
    anti = [{"id": f"op-{i}", "n_sources": 2, "age_h": 4.0,
             "score_breakdown": {"track_fit": 90 - 20 * i, "executability": 10 + 20 * i,
                                 "timing": 50, "feasibility": 50, "competition": 50}}
            for i in range(5)]
    base = {"track_fit": 0.84, "timing": 0.04, "feasibility": 0.04,
            "competition": 0.04, "executability": 0.04}
    inv = {"track_fit": 0.04, "timing": 0.04, "feasibility": 0.04,
           "competition": 0.04, "executability": 0.84}
    assert gate(anti, base, inv, CFG)["decision"] == "block"


def test_non_autopass_carries_reasons():
    # human-review semantics: a non-auto_pass verdict must never be silent — it states why.
    gate = _gate()
    violent = {"track_fit": 0.02, "timing": 0.02, "feasibility": 0.02,
               "competition": 0.02, "executability": 0.92}
    v = gate(GOLDEN, BASE_W, violent, CFG)
    assert v["decision"] != "auto_pass"
    assert isinstance(v["reasons"], list) and len(v["reasons"]) >= 1


def test_gate_monotone_in_tolerance():
    # tightening the budget must never be MORE permissive than a looser budget (auto_pass <
    # needs_review < block).
    gate = _gate()
    rank = {"auto_pass": 0, "needs_review": 1, "block": 2}
    cand = {"track_fit": 0.10, "timing": 0.40, "feasibility": 0.10,
            "competition": 0.10, "executability": 0.30}
    loose = copy.deepcopy(CFG); loose["scoring"]["weight_regression"] = {
        "max_tau": 0.9, "max_push_churn_frac": 0.9, "catastrophic_tau": 0.99,
        "catastrophic_churn_frac": 0.99}
    tight = copy.deepcopy(CFG); tight["scoring"]["weight_regression"] = {
        "max_tau": 0.01, "max_push_churn_frac": 0.01, "catastrophic_tau": 0.5,
        "catastrophic_churn_frac": 0.5}
    dl = gate(GOLDEN, BASE_W, cand, loose)["decision"]
    dt = gate(GOLDEN, BASE_W, cand, tight)["decision"]
    assert rank[dt] >= rank[dl]


def test_gate_config_tunable():
    # the budget is a real config surface: a strict budget flips an otherwise-passing retune.
    gate = _gate()
    cand = {"track_fit": 0.10, "timing": 0.40, "feasibility": 0.10,
            "competition": 0.10, "executability": 0.30}
    loose = copy.deepcopy(CFG); loose["scoring"]["weight_regression"] = {
        "max_tau": 0.9, "max_push_churn_frac": 0.9, "catastrophic_tau": 0.99,
        "catastrophic_churn_frac": 0.99}
    strict = copy.deepcopy(CFG); strict["scoring"]["weight_regression"] = {
        "max_tau": 0.001, "max_push_churn_frac": 0.001, "catastrophic_tau": 0.99,
        "catastrophic_churn_frac": 0.99}
    assert gate(GOLDEN, BASE_W, cand, loose)["decision"] == "auto_pass"
    assert gate(GOLDEN, BASE_W, cand, strict)["decision"] == "needs_review"


def test_directional_sanity_timing():
    # raising the TIMING weight must move a timing-dominant item UP (rank index not worse), proving
    # the reweight is coherent, not random churn.
    rerank = _rank()
    low = {"track_fit": 0.30, "timing": 0.05, "feasibility": 0.25,
           "competition": 0.15, "executability": 0.25}
    high = {"track_fit": 0.15, "timing": 0.60, "feasibility": 0.10,
            "competition": 0.05, "executability": 0.10}
    r_low = rerank(GOLDEN, low, CFG).index("op-timing")
    r_high = rerank(GOLDEN, high, CFG).index("op-timing")
    assert r_high <= r_low  # lower index = better rank
