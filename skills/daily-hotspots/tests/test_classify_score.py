"""T1 classification consistency + T2 score reproducibility & monotonicity."""
import json

from lib import load_config, canonical_key, freshness, confidence
from classify import classify
from score import score_opportunity

CFG = load_config()


# ---------------------------------------------------------------- T1
def test_classify_deterministic_same_label():
    title = "Show HN: an open-source MCP agent framework for LLM tooling"
    text = "self-host your own agent, replaces a paid API"
    a = classify(title, text, CFG)
    b = classify(title, text, CFG)
    assert a == b
    assert a["track"] == "ai-agents"
    assert not a["excluded"]


def test_classify_exclude_mute():
    out = classify("New memecoin airdrop giveaway", "pump it", CFG)
    assert out["excluded"] is True
    assert out["track"] is None


def test_classify_track_enum_only():
    out = classify("A vertical SaaS billing workflow for compliance", "", CFG)
    assert out["track"] in {t["id"] for t in CFG["tracks"]}


def test_canonical_key_pure_and_orderless():
    k1 = canonical_key(["MinerU", "opendatalab-mineru", "pdf"], "ai-agents")
    k2 = canonical_key(["pdf", "mineru", "mineru"], "ai-agents")
    assert k1 == k2  # alias-folded + sorted + deduped


# ---------------------------------------------------------------- T2
BD = {"track_fit": 80, "timing": 90, "feasibility": 70, "competition": 65, "executability": 80}


def test_score_byte_identical():
    outs = [json.dumps(score_opportunity(BD, 3, 10.0, 0.2, 1.3, CFG), sort_keys=True)
            for _ in range(8)]
    assert len(set(outs)) == 1


def test_score_in_range():
    s = score_opportunity(BD, 3, 10.0, None, 1.0, CFG)
    assert 0 <= s["final_score"] <= 100
    assert s["grade"] in {"A", "B+", "B", "C+", "C", "D"}


def test_score_monotone_in_sources():
    s1 = score_opportunity(BD, 1, 10.0, None, 1.0, CFG)["final_score"]
    s2 = score_opportunity(BD, 2, 10.0, None, 1.0, CFG)["final_score"]
    s3 = score_opportunity(BD, 3, 10.0, None, 1.0, CFG)["final_score"]
    assert s1 <= s2 <= s3
    assert confidence(1) <= confidence(2) <= confidence(3)


def test_score_monotone_in_staleness():
    fresh = score_opportunity(BD, 3, 1.0, None, 1.0, CFG)["final_score"]
    stale = score_opportunity(BD, 3, 240.0, None, 1.0, CFG)["final_score"]
    assert stale <= fresh
    assert freshness(240) <= freshness(1)


def test_golden_scale_reachable():
    # R2 anchor: a strong, fresh, 2-source opportunity MUST be able to clear the push floor (70),
    # and a mediocre stale one must not. Guards against a multiplier stack that crushes everything
    # below threshold (regression: over-punishing freshness made nothing pushable).
    strong = score_opportunity(
        {"track_fit": 85, "timing": 90, "feasibility": 80, "competition": 70, "executability": 85},
        2, 4.0, 0.3, 1.3, CFG)["final_score"]
    weak = score_opportunity(
        {"track_fit": 45, "timing": 40, "feasibility": 45, "competition": 40, "executability": 45},
        2, 200.0, None, 1.0, CFG)["final_score"]
    assert strong >= CFG["scoring"]["min_score_to_push"], strong
    assert weak < CFG["scoring"]["min_score_to_archive"], weak


def test_effort_not_a_denominator():
    # raising executability (a positive dim) must not crash the score upward unboundedly
    lo = score_opportunity({**BD, "executability": 10}, 3, 10.0, None, 1.0, CFG)["final_score"]
    hi = score_opportunity({**BD, "executability": 100}, 3, 10.0, None, 1.0, CFG)["final_score"]
    assert hi >= lo and hi <= 100
