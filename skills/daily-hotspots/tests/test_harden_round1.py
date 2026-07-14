#!/usr/bin/env python3
"""Harden round 1 regression guards — the yield-engine + config slice (audit HARDEN pass).

One (or a few) test(s) per verified finding whose FIX lives in yield.py / lib.py / verify_config.py;
each FAILS on the pre-fix code. Deterministic: stdlib only, no network, no live MCP, no live config
(every call passes an explicit cfg or a tmp watchlist, so the companion repo is never probed). Clock
frozen by conftest (DAILY_HOTSPOTS_NOW = 2026-06-25T12:00:00Z).

Findings covered here:
  3/7. yield numerator dedups a RESURFACED card by opportunity identity ("once per card", §8) — a
       single resurfacing story no longer triple-counts a handle's contributions or crosses
       propose_add_min_count on its own.
  5/8. the §9 yield guardrails only TIGHTEN — a watchlist.json can't mass-prune the roster
       (floor:1000) or nullify the cold-start guard (min_history_days:0); the doctor
       (verify_config.validate_yield_block) surfaces such loosening loudly.
  6.   the review queue's "recently pruned" is DURABLE — a handle disabled in a PRIOR run stays
       discoverable for un-prune (§9), not just this-report's fresh prunes.

(Findings 1/2/4 — parse_v2ex/parse_rss content-safety and the cross-day community-pulse dedup —
live in run.py / digest.py / dedup.py and are guarded by that slice's own tests.)
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

import roster as RT
from lib import load_config, parse_ts

Y = importlib.import_module("yield")   # 'yield' is a keyword -> import by name

# verify_config.py lives at <repo>/scripts (not the skill scripts dir).
ROOT_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))
import verify_config as vc  # noqa: E402

NOW = parse_ts("2026-06-25T12:00:00Z")
YCFG = Y.yield_cfg({})                  # module defaults (window 30d, floor 0, prune_after_weeks 2)


# =================================================================== Finding 3 & 7: resurface dedup
def _resurfacing_records(opp_id, handle, n, base_day=20):
    """ONE opportunity re-archived n times (RESURFACE): n append-only lines, ONE opportunity_id."""
    out = []
    for i in range(n):
        ts = "2026-06-%02dT09:00:00Z" % (base_day + i)
        out.append({"opportunity_id": opp_id, "canonical_key": "ck-" + opp_id,
                    "first_seen": "2026-06-%02dT09:00:00Z" % base_day, "last_seen": ts,
                    "pushed": True, "track": "dev-tools",
                    "evidence": [{"origin_handle": handle,
                                  "url": "https://x.com/%s/status/1" % handle, "faves": 50}]})
    return out


def test_compute_yield_counts_resurfaced_card_once():
    # A rostered handle whose ONE opportunity resurfaced 3x must not read as 3 contributions.
    records = _resurfacing_records("op-res", "karpathy", 3)
    pulls = [{"run_id": "r%d" % i, "ts": "2026-06-2%dT08:00:00Z" % (2 + i), "handle": "karpathy"}
             for i in range(3)]
    y = Y.compute_yield(records, pulls, NOW, YCFG)
    s = y[Y.okey(Y.KIND_HANDLE, "karpathy")]
    assert s["contributions"] == 1        # once per distinct opportunity, NOT once per archive line
    assert s["pulls"] == 3
    assert s["yield"] == pytest.approx(1 / 3)   # 1 distinct card / 3 pulls, not 3/3


def test_propose_add_dedups_resurfaced_opportunity_below_min_count():
    # A non-roster handle in ONE opportunity that resurfaced twice must NOT cross the default
    # propose_add_min_count (2) on the strength of a single story.
    records = _resurfacing_records("op-res", "newvoice", 2)
    rep = Y.run_yield({"schema_version": 1, "entries": []}, records, [], cfg={}, now=NOW)
    assert "newvoice" not in {c["handle"] for c in rep["propose_add"]}


def test_weekly_observations_dedups_resurface_within_week():
    # The same opportunity re-archived twice in ONE week counts once for that week (no false
    # perpetual-productivity that would let a dead-weight handle evade prune).
    records = [
        {"opportunity_id": "op-x", "first_seen": "2026-06-24T09:00:00Z",
         "last_seen": "2026-06-24T09:00:00Z", "track": "dev-tools",
         "evidence": [{"origin_handle": "hh", "url": "u1"}]},
        {"opportunity_id": "op-x", "first_seen": "2026-06-24T09:00:00Z",
         "last_seen": "2026-06-25T09:00:00Z", "track": "dev-tools",
         "evidence": [{"origin_handle": "hh", "url": "u1"}]},
    ]
    obs = Y.weekly_observations((Y.KIND_HANDLE, "hh"), records, [], NOW, 1)
    assert obs[0][0] == 1        # week-0 contributions deduped to 1 (both lines are one opportunity)


# =================================================================== Finding 5 & 8: yield config clamp
def _load_with_yield(tmp_path, yblk):
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps({"schema_version": 1, "yield": yblk}), encoding="utf-8")
    return load_config(explicit_path=str(p))


def test_yield_floor_cannot_be_loosened_to_mass_prune(tmp_path):
    assert _load_with_yield(tmp_path, {"floor": 1000})["yield"]["floor"] == 0   # capped at default


def test_yield_min_history_days_cannot_nullify_cold_start(tmp_path):
    assert float(_load_with_yield(tmp_path, {"min_history_days": 0})["yield"]["min_history_days"]) >= 7


def test_yield_prune_after_weeks_cannot_be_loosened(tmp_path):
    assert float(_load_with_yield(tmp_path, {"prune_after_weeks": 1})["yield"]["prune_after_weeks"]) >= 2


def test_yield_config_may_still_tighten(tmp_path):
    cfg = _load_with_yield(tmp_path, {"prune_after_weeks": 3, "min_history_days": 14, "floor": -1})
    assert float(cfg["yield"]["prune_after_weeks"]) == 3     # stricter is honored
    assert float(cfg["yield"]["min_history_days"]) == 14
    assert cfg["yield"]["floor"] == -1                       # floor <= default is allowed (disables prune)


def test_clamped_config_disarms_mass_prune_end_to_end(tmp_path):
    # A watchlist that TRIED to gut the roster (floor 1000, prune_after_weeks 1, min_history 0) must,
    # after load_config's clamp, prune NOTHING — a handle with even ONE contribution is spared.
    cfg = _load_with_yield(tmp_path, {"floor": 1000, "prune_after_weeks": 1, "min_history_days": 0})
    roster = {"schema_version": 1, "entries": [
        {"handle": "keepme", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    records = [{"opportunity_id": "op1", "first_seen": "2026-06-10T09:00:00Z",
                "last_seen": "2026-06-24T09:00:00Z", "pushed": True, "track": "ai-agents",
                "evidence": [{"origin_handle": "keepme", "url": "u", "faves": 700}]}]
    pulls = [{"run_id": "r%d" % i, "ts": "2026-06-%02dT08:00:00Z" % d, "handle": "keepme"}
             for i, d in enumerate((17, 24))]
    rep = Y.run_yield(roster, records, pulls, cfg=cfg, now=NOW, apply=True)
    assert rep["prune"] == []                              # the loosened floor was clamped away
    assert RT.find_entry(roster, "keepme")["enabled"] is True


def test_doctor_validate_yield_block_accepts_safe_and_absent():
    assert vc.validate_yield_block(None) == (True, [])
    ok, errs = vc.validate_yield_block({"floor": 0, "prune_after_weeks": 2, "min_history_days": 7})
    assert ok and errs == []


def test_doctor_validate_yield_block_flags_loosening():
    ok, errs = vc.validate_yield_block({"floor": 1000})
    assert not ok and any("floor" in e for e in errs)
    ok2, errs2 = vc.validate_yield_block({"min_history_days": 0})
    assert not ok2 and any("min_history_days" in e for e in errs2)
    assert not vc.validate_yield_block({"prune_after_weeks": 1})[0]


def test_doctor_validate_yield_block_flags_malformed_type():
    ok, errs = vc.validate_yield_block({"floor": "lots"})
    assert not ok and any("floor" in e for e in errs)


def test_doctor_validate_yield_block_allows_tightening():
    ok, errs = vc.validate_yield_block({"prune_after_weeks": 4, "min_history_days": 30, "floor": 0})
    assert ok and errs == []


# =================================================================== Finding 6: durable recently-pruned
def test_recently_pruned_shows_previously_disabled_handle():
    # A handle disabled in a PRIOR run (enabled=false) is skipped by decide_prune (enabled-only), so
    # the review file must still enumerate it under 'recently pruned' for un-prune (§9 durability).
    roster = {"schema_version": 1, "entries": [
        {"handle": "activeone", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"},
        {"handle": "prunedlast", "track": "dev-tools", "tier": 1, "enabled": False,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed", "notes": "auto-pruned 2026-06-18"},
    ]}
    rep = Y.run_yield(roster, [], [], cfg={}, now=NOW)
    assert rep["prune"] == []                              # nothing freshly pruned this run
    assert any(d["handle"] == "prunedlast" for d in rep["disabled"])
    md = Y.render_review_md(rep)
    sect = md.split("## recently pruned")[1].split("\n## ")[0]
    assert "prunedlast" in sect                            # durably discoverable for un-prune
    assert "_none_" not in sect                            # the section is NOT empty
