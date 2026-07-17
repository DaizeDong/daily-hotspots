"""Two-column model (2026-07): DEMAND (quality, non-consensus) vs SUPPLY (basic hotspots).
Demand scoring de-emphasizes timing, floors freshness (durable pain), penalizes crowdedness, and
clears a higher bar; the digest and headlines render the two columns separately, demand first."""
import digest as dg
import verify_gate as vg
from score import score_opportunity
from lib import load_config

CFG = load_config()
_BD = {"track_fit": 70, "timing": 90, "feasibility": 70, "competition": 70, "executability": 70}


# --------------------------------------------------------------------------- scoring: side split
def test_demand_weights_differ_from_supply():
    # identical inputs, only the side differs -> different weighting -> different raw score.
    sup = score_opportunity(_BD, 3, 10.0, cfg=CFG, side="supply")
    dem = score_opportunity(_BD, 3, 10.0, cfg=CFG, side="demand")
    assert sup["side"] == "supply" and dem["side"] == "demand"
    assert sup["raw_score"] != dem["raw_score"]          # a different weight vector is actually used


def test_demand_deemphasizes_timing():
    # a high-timing / low-competition card: supply loves it (timing is top weight), demand does not.
    hot = {"track_fit": 60, "timing": 100, "feasibility": 60, "competition": 20, "executability": 60}
    sup = score_opportunity(hot, 3, 5.0, cfg=CFG, side="supply")["raw_score"]
    dem = score_opportunity(hot, 3, 5.0, cfg=CFG, side="demand")["raw_score"]
    assert dem < sup                                     # demand does not reward pure hotness


def test_crowdedness_penalizes_demand_only():
    clean = score_opportunity(_BD, 3, 10.0, cfg=CFG, side="demand", crowdedness=0)
    red = score_opportunity(_BD, 3, 10.0, cfg=CFG, side="demand", crowdedness=100)
    assert red["final_score"] < clean["final_score"]     # a red ocean is worth less
    assert red["crowdedness_mult"] < 0.5 <= clean["crowdedness_mult"]
    # crowdedness is a no-op on supply
    s0 = score_opportunity(_BD, 3, 10.0, cfg=CFG, side="supply", crowdedness=100)
    assert s0["crowdedness_mult"] == 1.0


def test_demand_freshness_floor_beats_stale_supply():
    # a 30-day-old signal: supply freshness collapses, demand is floored (durable pain survives).
    old = 24 * 30.0
    sup = score_opportunity(_BD, 3, old, cfg=CFG, side="supply")["freshness"]
    dem = score_opportunity(_BD, 3, old, cfg=CFG, side="demand")["freshness"]
    assert dem >= CFG["scoring"]["demand_freshness_floor"] > sup


# --------------------------------------------------------------------------- gate: higher demand bar
def _card(score, side, title):
    return {"canonical_key": f"k|{title}", "title": title, "summary": f"summary of {title}",
            "track": "saas-niche", "final_score": score, "grade": "C",
            "side": side, "why_now": "w", "action": "build it", "independent_source_count": 2,
            "score_breakdown": {d: 60 for d in ("track_fit", "timing", "feasibility",
                                                "competition", "executability")},
            "evidence": [{"source": "reddit", "origin_type": "internal", "url": "u1",
                          "ts": "2026-07-16T10:00:00Z"},
                         {"source": "g2", "origin_type": "external", "url": "u2",
                          "ts": "2026-07-16T09:00:00Z"}]}


def test_demand_bar_is_higher_than_supply():
    # score 57: clears supply archive floor (55) but NOT the demand bar (60).
    g = vg.gate_batch([_card(57, "demand", "weakdemand"), _card(57, "supply", "weaksupply")], CFG)
    titles = [c["title"] for c in g["archivable"]]
    assert "weaksupply" in titles and "weakdemand" not in titles


# --------------------------------------------------------------------------- digest: two sections
def _full(score, side, title, track="saas-niche"):
    c = _card(score, side, title)
    c.update({"track": track, "grade": "B"})
    return c


def test_markdown_renders_both_sections_demand_first():
    md = dg.build_markdown([_full(80, "supply", "hot thing"), _full(80, "demand", "real pain")],
                           {"candidates": 2}, "2026-07-16")
    assert "🎯 需求机会" in md and "📈 供给热点" in md
    assert md.index("需求机会") < md.index("供给热点")            # demand column first
    assert md.index("real pain") < md.index("hot thing")         # demand card before supply card


def test_markdown_demand_empty_is_honest_supply_still_shows():
    md = dg.build_markdown([_full(80, "supply", "hot thing")], {"candidates": 1}, "2026-07-16")
    assert "今日无合格需求机会" in md                              # honest empty demand section
    assert "hot thing" in md                                      # supply still rendered


def test_headlines_lead_with_demand_then_supply_tail():
    cards = [_full(85, "demand", "demand win"), _full(70, "supply", "supply hot")]
    out = dg.build_headlines(cards, {"candidates": 2}, date="2026-07-16")
    assert "🎯 **需求机会**" in out and "📈 **供给热点**" in out
    assert out.index("需求机会") < out.index("供给热点")
    assert "**1.【" in out                                        # demand gets the numbered treatment
    assert "· 【" in out                                          # supply is the compact tail
