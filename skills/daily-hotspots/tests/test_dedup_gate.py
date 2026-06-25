"""T3 dedup correctness, T6 low-quality filter, T7 cross-day, T9 schema gate."""
from lib import load_config, canonical_key, simhash
import dedup as dd
from verify_gate import validate_card, gate_batch

CFG = load_config()
EXT = dd.EXT_PREFIX


def _row(title, summary, track, score, stage="", sources=None, push_count=0):
    ck = canonical_key([w for w in (title + " " + summary).lower().split()], track)
    ext = {
        EXT + "canonical_key": ck,
        EXT + "simhash": simhash(title + " " + summary),
        EXT + "text": title + " " + summary,
        EXT + "first_seen": "2026-06-24T12:00:00Z",
        EXT + "last_seen": "2026-06-24T12:00:00Z",
        EXT + "last_score": score,
        EXT + "lifecycle_stage": stage,
        EXT + "source_set": sources or ["hackernews", "trend-pulse"],
        EXT + "push_count": push_count,
        EXT + "samples": [],
    }
    return {"idempotency_key": ck, "ext": ext}


def _cand(title, summary, track, score, stage="", sources=None):
    ck = canonical_key([w for w in (title + " " + summary).lower().split()], track)
    ev = [{"source": s, "origin": s + ".com", "url": "http://x", "ts": "2026-06-25T11:00:00Z"}
          for s in (sources or ["hackernews", "trend-pulse"])]
    return {"canonical_key": ck, "title": title, "summary": summary, "track": track,
            "final_score": score, "lifecycle_stage": stage, "evidence": ev,
            "source_set": sources or ["hackernews", "trend-pulse"]}


# ---------------------------------------------------------------- T3
def test_exact_match():
    row = _row("MCP agent framework", "open source tooling", "ai-agents", 70)
    cand = _cand("MCP agent framework", "open source tooling", "ai-agents", 72)
    assert dd.match_existing(cand, [row], CFG) is not None


def test_distinct_no_match():
    row = _row("MCP agent framework", "open source tooling", "ai-agents", 70)
    cand = _cand("DeFi yield aggregator", "stablecoin onchain vault", "fintech-crypto", 70)
    assert dd.match_existing(cand, [row], CFG) is None


def test_near_dup_rewrite_matches():
    row = _row("MinerU PDF extraction open source tool", "parse pdf to markdown",
               "ai-agents", 70, sources=["github", "hackernews"])
    cand = _cand("MinerU open-source PDF extraction tool", "convert pdf into markdown",
                 "ai-agents", 71, sources=["github", "hackernews"])
    assert dd.match_existing(cand, [row], CFG) is not None


# ---------------------------------------------------------------- T7
def test_decide_new():
    cand = _cand("Brand new agent infra idea", "fresh entities only", "ai-agents", 80)
    assert dd.decide(cand, None, CFG)["branch"] == dd.NEW


def test_decide_suppress():
    row = _row("MCP agent framework", "open source tooling", "ai-agents", 70)
    cand = _cand("MCP agent framework", "open source tooling", "ai-agents", 72)  # delta 2
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.SUPPRESS


def test_decide_resurface_on_score_jump():
    row = _row("MCP agent framework", "open source tooling", "ai-agents", 60)
    cand = _cand("MCP agent framework", "open source tooling", "ai-agents", 85)  # +25
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.RESURFACE


def test_decide_resurface_on_new_source_crossing_two():
    row = _row("MCP agent framework", "open source tooling", "ai-agents", 70,
               sources=["hackernews"])
    cand = _cand("MCP agent framework", "open source tooling", "ai-agents", 72,
                 sources=["hackernews", "product-hunt"])
    m = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, m, CFG)["branch"] == dd.RESURFACE


# ---------------------------------------------------------------- T9 schema gate
def _full_card(score=80, n=2):
    return {
        "track": "ai-agents", "final_score": score, "independent_source_count": n,
        "score_breakdown": {"track_fit": 80, "timing": 90, "feasibility": 70,
                            "competition": 65, "executability": 80},
        "evidence": [{"url": "http://a", "source": "hackernews", "ts": "2026-06-25T11:00:00Z"},
                     {"url": "http://b", "source": "trend-pulse", "ts": "2026-06-25T11:00:00Z"}],
        "why_now": "platform shift", "action": "build MVP",
    }


def test_gate_full_card_passes():
    ok, errs = validate_card(_full_card(), CFG)
    assert ok, errs


def test_gate_blocks_missing_evidence():
    c = _full_card()
    c["evidence"] = c["evidence"][:1]
    ok, errs = validate_card(c, CFG)
    assert not ok and any("evidence" in e for e in errs)


def test_gate_blocks_one_source():
    c = _full_card(n=1)
    ok, errs = validate_card(c, CFG)
    assert not ok and any("independent_source_count" in e for e in errs)


def test_gate_blocks_missing_dim():
    c = _full_card()
    del c["score_breakdown"]["timing"]
    ok, errs = validate_card(c, CFG)
    assert not ok and any("timing" in e for e in errs)


# ---------------------------------------------------------------- T6 low-quality filter
def test_batch_no_filler_and_cap():
    high = [_full_card(score=90) for _ in range(8)]
    for i, c in enumerate(high):
        c["title"] = f"high-{i}"
        c["canonical_key"] = f"k{i}"
    low = _full_card(score=30)
    low["title"] = "low"
    g = gate_batch(high + [low], CFG)
    assert len(g["pushable"]) <= CFG["push"]["max_per_day"]
    assert all(c["final_score"] >= CFG["scoring"]["min_score_to_push"] for c in g["pushable"])
    assert "low" not in [c["title"] for c in g["pushable"]]


def test_batch_empty_day_honest():
    low = _full_card(score=20)
    g = gate_batch([low], CFG)
    assert g["empty_day"] is True
    assert g["pushable"] == []
