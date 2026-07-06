#!/usr/bin/env python3
"""Regression guard for run.py wiring fixes (v0.1.2).

R4: the lifecycle downweight must actually reach the LIVE scoring path (run.build_card had been
calling score_opportunity without lifecycle_stage, so sw was always 1.0 in production).
R5: the catch-up backfill entry (`--catch-up`) must be reachable and must not block on stdin.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import lib  # noqa: E402
import run  # noqa: E402


def _cfg():
    cfg = lib.load_config()
    cfg["scoring"]["lifecycle_weights"] = {"fading": 0.3, "emerging": 1.0}
    return cfg


def _cand(stage):
    return dict(
        title="AI agent infra tool", summary="new dev tool for agents",
        evidence=[{"source": "hn", "origin": "hn"}, {"source": "ph", "origin": "producthunt"}],
        score_breakdown={"market": 80, "timing": 80, "moat": 70, "feasibility": 75,
                         "originality": 70, "evidence_strength": 70},
        age_hours=5.0, velocity=0.2, lifecycle_stage=stage,
    )


def test_r4_lifecycle_downweight_reaches_live_scoring():
    cfg = _cfg()
    emerging = run.build_card(_cand("emerging"), cfg, "r")["final_score"]
    fading = run.build_card(_cand("fading"), cfg, "r")["final_score"]
    # With the R4 wiring, a closed-window (fading) opportunity scores strictly lower than an
    # emerging one; before the fix both were identical (sw==1.0 in the live path).
    assert fading < emerging, (fading, emerging)


def test_catch_up_flag_is_reachable_and_non_blocking(monkeypatch, capsys):
    # --catch-up must not read stdin (would block) and must exit cleanly even without a ledger.
    monkeypatch.setattr(sys, "argv", ["run.py", "--catch-up", "--no-ledger"])
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 1 and "catch_up" in out  # no ledger -> reported, not a crash/hang
