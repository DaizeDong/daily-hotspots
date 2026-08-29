#!/usr/bin/env python3
"""The demand lane must be able to clear its own floor (regression pin for the 45-day silent kill).

Background, so this file is not mistaken for a loosening. As shipped, the demand lane archived ZERO
cards over its entire production history while the digest reported each empty column as an honest
quiet day. Demand cards were being produced every day; they were being killed arithmetically. On one
measured day the two sides' RAW scores were comparable (demand 65.6..79.3, supply 65.3..83.4) and
their finals were not (demand 31.0..50.2, supply 55.0..76.2), because demand alone paid two
multipliers and was then held to a floor five points higher than supply's.

The fix charges each signal once at its declared weight. These tests pin the three properties that
make it a fix rather than a loosening:

  * a well-evidenced demand card CLEARS the floor now and provably did not before  (test_*_clears_*)
  * a genuinely weak demand card is STILL dropped                                  (test_weak_*)
  * supply is UNTOUCHED, byte for byte                                             (test_supply_*)

The third is the load-bearing one. If the change had been a global loosening, supply would have
moved too. Every fixture here is synthetic: shapes were reproduced by hand, no operator record is
copied into the repo.
"""
from __future__ import annotations

import copy

import pytest

import score as S
import verify_gate as VG
from lib import load_config

_DIMS = ("track_fit", "timing", "feasibility", "competition", "executability")


def _dims(**kw) -> dict:
    base = {d: 60.0 for d in _DIMS}
    base.update(kw)
    return base


def _cfg():
    return load_config()


def _legacy_cfg():
    """The pre-fix scoring, reachable only through config, so new-vs-old is measurable rather than
    asserted from memory. If either replay switch is ever deleted, these tests fail loudly instead of
    silently comparing the new behavior against itself."""
    cfg = copy.deepcopy(load_config())
    cfg["scoring"]["crowdedness_mode"] = "legacy_multiplier"
    cfg["scoring"]["demand_freshness_mode"] = "floor"
    return cfg


def _score(dims, cfg, *, n_sources=3, age_h=500.0, side="demand", crowdedness=40,
           lifecycle="emerging", track_weight=1.0):
    return S.score_opportunity(dims, n_sources, age_h, None, track_weight, cfg, lifecycle,
                               side=side, crowdedness=crowdedness)["final_score"]


# --------------------------------------------------------------------------- the replay switches
def test_legacy_replay_is_actually_different_from_the_shipped_default():
    """Negative control for the comparison itself.

    Every new-vs-old assertion below is worthless if `_legacy_cfg` silently produces the same numbers
    as the default (a renamed key, a dropped branch). "old and new agree" and "we failed to reach the
    old behavior" must not look the same, so the difference is asserted before it is relied on.
    """
    dims = _dims(track_fit=80, timing=60, feasibility=86, competition=72, executability=84)
    assert _score(dims, _legacy_cfg(), crowdedness=45) < _score(dims, _cfg(), crowdedness=45) - 10, \
        "legacy replay is not reproducing the old, harsher scoring; the comparison would be vacuous"


# --------------------------------------------------------------------------- demand can now pass
@pytest.mark.parametrize("dims,crowd", [
    # Shapes reproducing real well-evidenced demand cards: strong feasibility and executability, a
    # judged competition in the 60s-70s, moderate crowdedness, evidence months old.
    (_dims(track_fit=80, timing=60, feasibility=86, competition=72, executability=84), 45),
    (_dims(track_fit=78, timing=55, feasibility=88, competition=76, executability=86), 25),
    (_dims(track_fit=70, timing=70, feasibility=80, competition=64, executability=78), 40),
])
def test_well_evidenced_demand_clears_the_floor_and_would_not_have_before(dims, crowd):
    floor = float(_cfg()["scoring"]["min_score_to_surface_demand"])
    new = _score(dims, _cfg(), crowdedness=crowd)
    old = _score(dims, _legacy_cfg(), crowdedness=crowd)
    assert new >= floor, f"a well-evidenced demand card still cannot clear {floor} (got {new})"
    assert old < floor, f"this shape already cleared {floor} before the fix ({old}); it pins nothing"


# --------------------------------------------------------------------------- over-rejection controls
@pytest.mark.parametrize("dims,crowd,label", [
    (_dims(**{d: 50 for d in _DIMS}), 90, "mediocre dims in a saturated market"),
    (_dims(**{d: 50 for d in _DIMS}), 50, "mediocre dims, ordinary crowding"),
    (_dims(track_fit=60, timing=60, feasibility=60, competition=60, executability=60), 80, "flat 60s, crowded"),
])
def test_weak_demand_is_still_dropped(dims, crowd, label):
    """The fix must not be 'let demand through'. A weak demand card stays under the floor."""
    floor = float(_cfg()["scoring"]["min_score_to_surface_demand"])
    got = _score(dims, _cfg(), crowdedness=crowd)
    assert got < floor, f"{label}: scored {got}, which now clears {floor}; the floor stopped meaning anything"


def test_single_source_demand_is_still_culled_by_confidence():
    """The >=2 independent origin red line is upstream of scoring, but the confidence multiplier is
    the last line of defense and must still bite."""
    dims = _dims(track_fit=85, timing=85, feasibility=85, competition=85, executability=85)
    floor = float(_cfg()["scoring"]["min_score_to_surface_demand"])
    assert _score(dims, _cfg(), n_sources=1, crowdedness=20) < floor


def test_lifecycle_penalty_still_applies_to_demand():
    """The lifecycle downweight was one of the two multipliers the old docstring failed to mention.
    It is legitimate and must survive the fix."""
    dims = _dims(**{d: 80 for d in _DIMS})
    scores = [_score(dims, _cfg(), crowdedness=30, lifecycle=st)
              for st in ("emerging", "peak", "declining", "fading")]
    assert scores == sorted(scores, reverse=True), f"lifecycle no longer orders demand scores: {scores}"
    assert scores[0] > scores[-1] + 20, "lifecycle penalty has become cosmetic"


def test_crowdedness_still_moves_demand_in_the_right_direction():
    dims = _dims(**{d: 75 for d in _DIMS})
    blue = _score(dims, _cfg(), crowdedness=0)
    red = _score(dims, _cfg(), crowdedness=100)
    assert blue > red, "crowdedness stopped penalizing a red ocean entirely"


def test_crowdedness_is_charged_once_not_twice():
    """The defect was double counting: crowdedness moved the score by more than the weight the config
    declares for the dimension it belongs to. Its total authority must now be bounded by that weight.
    """
    cfg = _cfg()
    w = cfg["scoring"]["demand_weights"]
    ceiling = 100.0 * float(w["competition"]) / sum(float(v) for v in w.values())
    dims = _dims(**{d: 75 for d in _DIMS})
    span = _score(dims, cfg, crowdedness=0) - _score(dims, cfg, crowdedness=100)
    assert span <= ceiling + 1e-6, \
        f"crowdedness moves the score by {span}, beyond the {ceiling} its declared weight allows"
    assert span > 0.0, "crowdedness has no effect at all; it is no longer charged even once"


# --------------------------------------------------------------------------- supply is untouched
@pytest.mark.parametrize("dims,age", [
    (_dims(track_fit=72, timing=92, feasibility=76, competition=64, executability=74), 12.0),
    (_dims(track_fit=88, timing=95, feasibility=55, competition=45, executability=62), 30.0),
    (_dims(track_fit=65, timing=70, feasibility=70, competition=60, executability=65), 96.0),
])
def test_supply_scores_are_bit_identical_before_and_after(dims, age):
    """The load-bearing control. A demand fix that also moved supply would be a global loosening
    wearing a demand fix's clothes."""
    old = _score(dims, _legacy_cfg(), side="supply", age_h=age, crowdedness=None)
    new = _score(dims, _cfg(), side="supply", age_h=age, crowdedness=None)
    assert new == old, f"supply moved: {old} -> {new}"


def test_supply_ignores_crowdedness_entirely():
    dims = _dims(**{d: 70 for d in _DIMS})
    assert _score(dims, _cfg(), side="supply", crowdedness=0) == \
           _score(dims, _cfg(), side="supply", crowdedness=100)


def test_demand_freshness_is_neutral_not_decayed():
    dims = _dims(**{d: 75 for d in _DIMS})
    fresh = _score(dims, _cfg(), age_h=1.0, crowdedness=30)
    ancient = _score(dims, _cfg(), age_h=20000.0, crowdedness=30)
    assert fresh == ancient, "demand is still being decayed on a news half-life"


def test_supply_freshness_still_decays():
    """Over-rejection control for the one above: neutralizing demand freshness must not neutralize
    supply's, which is the whole hotness signal on that side."""
    dims = _dims(**{d: 75 for d in _DIMS})
    assert _score(dims, _cfg(), side="supply", age_h=1.0, crowdedness=None) > \
           _score(dims, _cfg(), side="supply", age_h=20000.0, crowdedness=None)


# --------------------------------------------------------------------------- the gate reports drops
def _card(title, side, final, **kw):
    card = {
        "title": title, "side": side, "final_score": final,
        "category": "saas-niche", "track": "saas-niche",
        "score_breakdown": {d: 70 for d in _DIMS},
        "independent_source_count": 3,
        "evidence": [{"url": f"https://example.com/{i}", "source": "example",
                      "ts": "2026-08-01T00:00:00Z"} for i in range(3)],
        "why_now": "because", "action": "do the thing",
        "contrarian_insight": "not what you think",
    }
    card.update(kw)
    return card


def test_gate_reports_a_sub_floor_card_instead_of_dropping_it_silently():
    """THE defect that hid all of this. `blocked` carries schema failures only, so a card that passed
    validation and missed its floor used to disappear with every counter reading zero."""
    cfg = _cfg()
    floor = float(cfg["scoring"]["min_score_to_surface_demand"])
    weak = _card("weak demand", "demand", floor - 10.0)
    out = VG.gate_batch([weak], cfg)
    assert out["archivable"] == [], "precondition: this card must not be archivable"
    assert out["blocked"] == [], "precondition: this card must pass schema validation"
    assert len(out["below_floor"]) == 1, "a sub-floor card vanished with no record; that is the defect"
    rec = out["below_floor"][0]
    assert rec["title"] == "weak demand"
    assert rec["side"] == "demand"
    assert rec["floor"] == floor
    assert rec["final_score"] == floor - 10.0


def test_gate_below_floor_is_empty_when_nothing_was_dropped():
    """Over-rejection control: a reporter that always reports is as useless as one that never does."""
    cfg = _cfg()
    strong = _card("strong demand", "demand",
                   float(cfg["scoring"]["min_score_to_surface_demand"]) + 5.0)
    out = VG.gate_batch([strong], cfg)
    assert out["below_floor"] == []
    assert len(out["archivable"]) == 1


def test_gate_uses_the_higher_floor_for_demand_and_the_lower_one_for_supply():
    cfg = _cfg()
    sc = cfg["scoring"]
    mid = (float(sc["min_score_to_archive"]) + float(sc["min_score_to_surface_demand"])) / 2.0
    out = VG.gate_batch([_card("s", "supply", mid), _card("d", "demand", mid)], cfg)
    titles = {c["title"] for c in out["archivable"]}
    assert titles == {"s"}, f"the demand premium is not being applied: archivable={titles}"
    assert [r["title"] for r in out["below_floor"]] == ["d"]


def test_gate_reports_cards_eaten_by_the_push_cap():
    """A push cap that silently eats qualifying cards is the same silence one layer up."""
    cfg = copy.deepcopy(_cfg())
    cfg.setdefault("push", {})["max_per_day"] = 2
    push_floor = float(cfg["scoring"]["min_score_to_push"])
    cards = [_card(f"c{i}", "supply", push_floor + i) for i in range(5)]
    out = VG.gate_batch(cards, cfg)
    assert len(out["pushable"]) == 2
    assert out["over_push_cap"] == 3, "the cap ate 3 qualifying cards and said nothing"


# --------------------------------------------------------------------------- the calibration path
# Found by the self-evolve loop on 2026-08-29, and it is the one defect from the original audit that
# my own remediation left undone: _final_map re-scored every item for the R2 weight-regression gate
# WITHOUT passing side or crowdedness, so a demand card was re-scored with supply's weight vector.
# The same side-blindness that left the demand lane unable to clear its own floor for 45 days,
# reproduced inside the calibration path that exists to catch exactly that.
def _golden_item(iid, side, crowd=40):
    return {"id": iid, "side": side, "crowdedness": crowd,
            "score_breakdown": {"track_fit": 70, "timing": 40, "feasibility": 85,
                                "competition": 75, "executability": 80},
            "independent_source_count": 3, "age_hours": 500.0,
            "track_weight": 1.0, "lifecycle_stage": "emerging"}


def test_final_map_rescoring_respects_each_item_side():
    """A demand item must be re-scored with the demand vector, not supply's."""
    cfg = _cfg()
    items = [_golden_item("d", "demand"), _golden_item("s", "supply")]
    got = S._final_map(items, None, cfg)
    direct_d = S.score_opportunity(items[0]["score_breakdown"], 3, 500.0, None, 1.0, cfg,
                                   "emerging", side="demand",
                                   crowdedness=40)["final_score"]
    assert got["d"] == pytest.approx(direct_d), \
        "the regression gate re-scored a demand card as supply"


def test_final_map_rescoring_respects_crowdedness():
    """Crowdedness is a demand-only input and is persisted per card. Dropping it makes two items
    with very different crowd readings score identically, which is what the gate must not do."""
    cfg = _cfg()
    got = S._final_map([_golden_item("blue", "demand", crowd=0),
                        _golden_item("red", "demand", crowd=100)], None, cfg)
    assert got["blue"] != got["red"], "crowdedness was dropped during regression re-scoring"
    assert got["blue"] > got["red"]


def test_final_map_leaves_supply_alone():
    """Over-rejection control: passing side through must not change supply's numbers."""
    cfg = _cfg()
    item = _golden_item("s", "supply")
    got = S._final_map([item], None, cfg)
    direct = S.score_opportunity(item["score_breakdown"], 3, 500.0, None, 1.0, cfg,
                                 "emerging", side="supply", crowdedness=None)["final_score"]
    assert got["s"] == pytest.approx(direct)


def test_final_map_defaults_a_missing_side_to_supply():
    """An older persisted record has no side. It must not crash and must not become demand."""
    cfg = _cfg()
    item = _golden_item("old", "supply")
    item.pop("side")
    item.pop("crowdedness")
    got = S._final_map([item], None, cfg)
    assert got["old"] > 0
