"""R4 headroom: lifecycle window-closed downweight + cooling-velocity penalty (Acceptance Gate
T2/T6 extension).

ARCHITECTURE §3.2 (timing Why-Now = a *narrow* window) and §6.3 (宁缺毋滥) require that a
peak/declining/fading opportunity — whose window has closed — must NOT keep topping the feed
with the same score as a fresh emerging one. HEAD's `score_opportunity` has no lifecycle-stage
axis at all, and clamps cooling velocity to zero (`max(0.0, velocity)`), so a declining trend is
scored byte-identically to a flat one. These tests assert the *capability* (stage awareness +
cooling penalty), not any particular multiplier table.

Marked xfail(strict=False) so the baseline stays green (A-tier eligibility preserved); the fix
flips them to XPASS = a real, gate-adjudicated net gain, after which the markers are removed and
they become permanent regression guards.
"""
import copy
import json

import pytest

from lib import load_config
from score import score_opportunity

CFG = load_config()
# A strong, fresh, multi-source breakdown so the closed-window collapse is visible above noise.
BD = {"track_fit": 85, "timing": 90, "feasibility": 80, "competition": 70, "executability": 85}


def _final(stage=None, vel=None, n=3, age=4.0, cfg=None):
    return score_opportunity(BD, n, age, vel, 1.0, cfg or CFG, lifecycle_stage=stage)["final_score"]


# ----------------------------------------------------------------- lifecycle stage downweight
@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_declining_below_emerging():
    assert _final("declining") < _final("emerging")


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_fading_below_declining():
    assert _final("fading") < _final("declining")


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_peak_below_emerging():
    assert _final("peak") < _final("emerging")


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_lifecycle_monotone_chain():
    e, p, d, f = _final("emerging"), _final("peak"), _final("declining"), _final("fading")
    assert e >= p >= d >= f
    assert f < e  # the closed-window axis strictly collapses a faded opportunity


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_unknown_stage_is_neutral():
    # stage awareness must never silently penalize an UNLABELED opportunity: None / unknown
    # stage == the no-stage baseline.
    base = score_opportunity(BD, 3, 4.0, None, 1.0, CFG)["final_score"]
    assert _final(None) == base
    assert _final("totally-unknown-stage") == base


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_stage_orthogonal_to_confidence():
    # downweighting a closed window must not corrupt the independent-source confidence axis.
    out = score_opportunity(BD, 3, 4.0, None, 1.0, CFG, lifecycle_stage="declining")
    assert out["confidence"] == 1.0
    assert out["final_score"] < score_opportunity(
        BD, 3, 4.0, None, 1.0, CFG, lifecycle_stage="emerging")["final_score"]


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_closed_window_drops_below_push_floor():
    # 宁缺毋滥 §6.3: an opportunity that clears the push floor while emerging must NOT keep topping
    # the feed once its window has closed (fading).
    floor = CFG["scoring"]["min_score_to_push"]
    assert _final("emerging") >= floor
    assert _final("fading") < floor


@pytest.mark.xfail(strict=False, reason="R4: lifecycle stage not yet a scoring axis")
def test_stage_deterministic():
    outs = [json.dumps(score_opportunity(BD, 3, 4.0, -0.3, 1.0, CFG, lifecycle_stage="declining"),
                       sort_keys=True) for _ in range(8)]
    assert len(set(outs)) == 1


@pytest.mark.xfail(strict=False, reason="R4: lifecycle weights not yet config-tunable")
def test_lifecycle_weights_config_tunable():
    cfg2 = copy.deepcopy(CFG)
    cfg2["scoring"]["lifecycle_weights"] = {
        "emerging": 1.0, "peak": 0.9, "declining": 0.5, "fading": 0.55}
    softer = score_opportunity(BD, 3, 4.0, None, 1.0, CFG,
                               lifecycle_stage="declining")["final_score"]
    harsher = score_opportunity(BD, 3, 4.0, None, 1.0, cfg2,
                                lifecycle_stage="declining")["final_score"]
    assert harsher < softer  # config override (0.5 < default 0.75) lowers the score


# ----------------------------------------------------------------- cooling velocity penalty
@pytest.mark.xfail(strict=False, reason="R4: cooling (negative) velocity clamped to zero")
def test_cooling_velocity_penalizes():
    assert _final(vel=-0.5) < _final(vel=0.0)


@pytest.mark.xfail(strict=False, reason="R4: cooling (negative) velocity clamped to zero")
def test_cooling_velocity_monotone():
    assert _final(vel=-0.8) <= _final(vel=-0.3) <= _final(vel=0.0)
    assert _final(vel=-0.8) < _final(vel=0.0)


@pytest.mark.xfail(strict=False, reason="R4: cooling (negative) velocity clamped to zero")
def test_cooling_velocity_bounded_positive():
    # deep cooling must stay a real (just-faded) signal, never zeroed, but below neutral.
    assert _final(vel=-1.0) > 0.0
    assert _final(vel=-1.0) < _final(vel=0.0)
