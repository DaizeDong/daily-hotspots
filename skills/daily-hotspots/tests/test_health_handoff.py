#!/usr/bin/env python3
"""The clean-versus-unchecked rule has to hold on the SEAM, not only inside each module.

The fail-open detector was built and adversarially verified: fed brightdata's two real measured empty
payloads, None, an empty string, an empty dict, an empty list, whitespace, a 160 byte bot
interstitial, a raising fetcher and a tavily-style error envelope, it never once returned ok. That
module is sound.

The hole was one layer up, on the handoff. `run.normalize_source_health` maps a probe report into the
coverage contract, and its fallback for an unrecognized state had no test. Mutating

    if state not in counts: state = "unknown"      ->      state = "ok"

turned a report of three sources in state "error" into {"ok": 3, "unknown": 0} and ZERO of 1032 tests
went red. A green fleet, rendered from a report that said everything was broken, because one word on
a seam nobody tested. That is precisely the failure this whole round exists to prevent, which is why
it belongs in its own file rather than appended to a parser test.

The rule these pin: an unrecognized, missing or malformed state is UNKNOWN. Never ok. "We do not
recognize what the probe said" and "the probe said it is fine" are different facts.
"""
from __future__ import annotations

import pytest

import run as R


def _report(*states):
    return {"results": [{"name": "src%d" % i, "state": s} for i, s in enumerate(states)]}


# --------------------------------------------------------------------------- the seam
@pytest.mark.parametrize("bogus", [
    "error",          # the shape that actually caught this: a plausible word the contract omits
    "ERROR",
    "failed",
    "healthy",        # a word that MEANS ok but is not the contract's word, and must not be honored
    "green",
    "",
    "   ",
    "unknown_state",
    "fail_open",      # near miss for fail_open_suspected
    "OK ",            # trailing space is normalized by strip, so this one IS ok, see the test below
])
def test_an_unrecognized_state_becomes_unknown_and_never_ok(bogus):
    got = R.normalize_source_health(_report(bogus, bogus, bogus))
    if bogus.strip().lower() in ("ok",):
        pytest.skip("this value normalizes to a real state and is covered separately")
    assert got["ok"] == 0, f"state {bogus!r} was counted as ok"
    assert got["unknown"] == 3, f"state {bogus!r} was not counted as unknown: {got}"


def test_a_report_of_three_broken_sources_never_renders_as_a_healthy_fleet():
    """The exact reproduction. Three sources reporting `error` must not summarize as ok."""
    got = R.normalize_source_health(_report("error", "error", "error"))
    assert got["ok"] == 0
    assert got["unknown"] == 3
    assert sum(got[k] for k in ("ok", "degraded", "down", "fail_open_suspected", "unknown")) == 3


def test_a_non_dict_result_row_is_counted_as_unknown_not_dropped():
    """A dropped row would shrink the denominator, so a fleet of garbage would summarize as a small
    clean fleet rather than as an unreadable one."""
    health = {"results": [{"name": "a", "state": "ok"}, "not-a-dict", None, 42]}
    got = R.normalize_source_health(health)
    assert got["ok"] == 1
    assert got["unknown"] == 3, "malformed rows vanished instead of counting as unknown"


def test_a_missing_state_key_is_unknown():
    got = R.normalize_source_health({"results": [{"name": "a"}, {"name": "b", "state": None}]})
    assert got["ok"] == 0 and got["unknown"] == 2


# --------------------------------------------------------------------------- over-rejection controls
def test_real_states_are_still_counted_as_themselves():
    """A normalizer that answered unknown to everything would be just as broken, and would make the
    alarm meaningless in the other direction."""
    got = R.normalize_source_health(
        _report("ok", "ok", "degraded", "down", "fail_open_suspected", "unknown"))
    assert got["ok"] == 2
    assert got["degraded"] == 1
    assert got["down"] == 1
    assert got["fail_open_suspected"] == 1
    assert got["unknown"] == 1


def test_state_matching_tolerates_case_and_surrounding_space():
    got = R.normalize_source_health(_report("  OK  ", "Down", "FAIL_OPEN_SUSPECTED"))
    assert got["ok"] == 1 and got["down"] == 1 and got["fail_open_suspected"] == 1
    assert got["unknown"] == 0


def test_the_broken_sources_are_named_so_the_alarm_can_say_which():
    got = R.normalize_source_health({"results": [
        {"name": "arctic-shift", "state": "down"},
        {"name": "brightdata", "state": "fail_open_suspected"},
        {"name": "themuse", "state": "ok"}]})
    assert got["names_down"] == ["arctic-shift"]
    assert got["names_fail_open"] == ["brightdata"]


# --------------------------------------------------------------------------- unmeasured stays unmeasured
@pytest.mark.parametrize("bad", [None, "", [], 0, "probe failed", 42])
def test_a_malformed_report_is_unmeasured_rather_than_a_clean_bill_of_health(bad):
    """READER seam: it may degrade, but degrading to None means the coverage line prints the
    unmeasured marker. Degrading to a zeroed dict would print a green tick for a probe that never ran.
    """
    assert R.normalize_source_health(bad) is None


def test_an_empty_results_list_is_not_a_clean_fleet():
    """Zero sources probed is the definition of unchecked. It must not summarize as all-ok."""
    got = R.normalize_source_health({"results": []})
    if got is not None:
        assert got["ok"] == 0, "probing nothing reported an ok source"
        assert sum(got[k] for k in ("ok", "degraded", "down", "fail_open_suspected",
                                    "unknown")) == 0
