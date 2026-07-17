#!/usr/bin/env python3
"""Dual-track routing (source-coverage design sec 7), the SPLIT that decides what a candidate
becomes below the >=2-independent-source red line. Deterministic, stdlib only, clock frozen by
conftest (DAILY_HOTSPOTS_NOW).

Contract asserted here:
  * >=2 independent origins + score >= gate       -> opportunity CARD (Track 1, unchanged)
  * single origin + community + fresh + track-hit  -> community_pulse (Track 2, a lightweight rumor)
  * single origin, anything else                   -> below_sources (a GAP, reported not dropped)
  * a pulse item carries its origin_source attribution + link through (never a leaked score)
  * the pure predicates (lib.*) and the routing seam (verify_gate.route_below_gate) agree
"""
import run as runner
import verify_gate as vg
from lib import (community_pulse_eligible, community_source_set, is_community_signal,
                 is_fresh_for_pulse, load_config)

CFG = load_config()


def _ev(source, url, ts="2026-06-25T11:00:00Z", signal="sig", heat=None, origin=None,
        origin_source=None):
    e = {"source": source, "origin": origin or source, "url": url, "ts": ts, "signal": signal}
    if heat is not None:
        e["heat"] = heat
    if origin_source is not None:
        e["origin_source"] = origin_source
    return e


def _cand(title, evidence, track=None, age_hours=4.0, timing=95):
    """A candidate WITHOUT a preset track goes through the real classifier (so track_matched is a
    genuine keyword hit); pass track= to force the roster-identity path."""
    c = {
        "title": title, "summary": "open source llm agent tooling for builders",
        "evidence": evidence,
        "score_breakdown": {"track_fit": 85, "timing": timing, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": age_hours, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }
    if track is not None:
        c["track"] = track
    return c


def _run(cand):
    return runner.process([cand], CFG, ledger=None, dry_run=True)


# =============================================================== Track 1 (unchanged)

def test_two_independent_origins_is_a_card_not_pulse():
    cand = _cand("MCP agent framework launch",
                 [_ev("v2ex", "https://v2ex.com/t/1", origin_source="v2ex"),
                  _ev("hackernews", "https://news.ycombinator.com/item?id=1")])
    res = _run(cand)
    assert res["built"] == 1                      # cleared the >=2 red line
    assert res["community_pulse"] == []           # a scored card, never a pulse rumor
    assert res["below_sources"] == []


# =============================================================== Track 2 (community pulse)

def test_single_origin_community_routes_to_pulse():
    cand = _cand("MCP agent framework launch",
                 [_ev("v2ex", "https://v2ex.com/t/42", signal="42 replies · programmer",
                      heat=42, origin_source="v2ex")])
    res = _run(cand)
    assert len(res["community_pulse"]) == 1       # surfaced as a rumor, NOT dropped
    assert res["below_sources"] == []             # ...and not counted as a gap
    assert res["built"] == 0 and res["pushed"] == []   # never a scored card


def test_pulse_item_carries_attribution_and_link_but_no_score():
    cand = _cand("Agent tooling rumor",
                 [_ev("linux.do", "https://linux.do/t/123", signal="9 replies", heat=9,
                      origin_source="linux.do")])
    item = _run(cand)["community_pulse"][0]
    assert item["origin_source"] == "linux.do"    # attribution (the yield numerator) carries through
    assert item["url"] == "https://linux.do/t/123"
    assert item["signal"] == "9 replies"
    # a rumor is never dressed as a scored opportunity, no scored dimension leaks into the item
    assert "final_score" not in item and "grade" not in item and "score_breakdown" not in item


# =============================================================== below_sources (the gap list)

def test_single_origin_noncommunity_is_a_gap_not_pulse():
    cand = _cand("MCP agent framework launch",
                 [_ev("hackernews", "https://news.ycombinator.com/item?id=9")])
    res = _run(cand)
    assert res["community_pulse"] == []           # X/HN single-origin is NOT a community rumor
    assert res["below_sources"] and res["below_sources"][0]["isc"] == 1


def test_single_origin_community_but_stale_is_a_gap():
    cand = _cand("MCP agent framework launch",
                 [_ev("v2ex", "https://v2ex.com/t/7", origin_source="v2ex")], age_hours=1000.0)
    res = _run(cand)
    assert res["community_pulse"] == []           # too old to surface as a fresh rumor
    assert res["below_sources"]


def test_single_origin_community_but_no_track_hit_is_a_gap():
    # A community topic with no track keyword (track_matched False) is off-topic noise, not a signal.
    cand = _cand("Weekend offtopic ramble thread",
                 [_ev("v2ex", "https://v2ex.com/t/8", origin_source="v2ex")])
    cand["summary"] = ""
    res = _run(cand)
    assert res["community_pulse"] == []
    assert res["below_sources"]


def test_excluded_community_is_never_a_pulse():
    # An excluded candidate (muted keyword) is diverted before routing, never a rumor.
    cand = _cand("memecoin giveaway airdrop thread",
                 [_ev("v2ex", "https://v2ex.com/t/9", origin_source="v2ex")])
    cand["summary"] = "memecoin pump giveaway airdrop"
    res = _run(cand)
    assert res["community_pulse"] == []
    assert res["excluded"] == 1


# =============================================================== pure predicates + routing seam

def test_community_source_set_default_and_config():
    assert {"v2ex", "linux.do"} <= community_source_set(CFG)
    tuned = {"community_pulse": {"community_sources": ["hackernews"]}}
    assert community_source_set(tuned) == {"hackernews"}


def test_is_community_signal():
    assert is_community_signal([{"origin_source": "v2ex"}], CFG) is True
    assert is_community_signal([{"source": "linux.do"}], CFG) is True
    assert is_community_signal([{"source": "hackernews"}], CFG) is False
    assert is_community_signal([], CFG) is False


def test_is_fresh_for_pulse_window_is_config_tunable():
    assert is_fresh_for_pulse(4.0, CFG) is True
    assert is_fresh_for_pulse(1000.0, CFG) is False
    assert is_fresh_for_pulse(None, CFG) is True           # undated -> treated as fresh (0h)
    tight = {"community_pulse": {"max_age_hours": 10}}
    assert is_fresh_for_pulse(4.0, tight) is True and is_fresh_for_pulse(20.0, tight) is False


def test_community_pulse_eligible_needs_all_four_conditions():
    base = {"track_matched": True, "age_hours": 4.0,
            "evidence": [{"origin_source": "v2ex"}]}
    assert community_pulse_eligible(base, CFG) is True
    assert community_pulse_eligible({**base, "track_matched": False}, CFG) is False
    assert community_pulse_eligible({**base, "age_hours": 1000.0}, CFG) is False
    assert community_pulse_eligible({**base, "evidence": [{"source": "hackernews"}]}, CFG) is False
    assert community_pulse_eligible({**base, "excluded": True}, CFG) is False


def test_route_below_gate_seam_matches_predicate():
    card = {"track_matched": True, "age_hours": 4.0, "evidence": [{"origin_source": "v2ex"}]}
    assert vg.route_below_gate(card, CFG) == vg.COMMUNITY_PULSE
    gap = {"track_matched": True, "age_hours": 4.0, "evidence": [{"source": "hackernews"}]}
    assert vg.route_below_gate(gap, CFG) == vg.BELOW_SOURCES
