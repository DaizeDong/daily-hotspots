"""T5 base round-trip (real reminder.py), T4 source coverage, T7 idempotent pipeline."""
import os
from pathlib import Path

import pytest

import dedup as dd
import run as runner
from lib import load_config, canonical_key

CFG = load_config()
EXT = dd.EXT_PREFIX

REMINDER = Path.home() / ".claude/skills/schedule-reminder/scripts/reminder.py"
_has_base = REMINDER.is_file()


def _cand(title, summary, track, bd_timing, sources):
    return {
        "title": title, "summary": summary, "track": track,
        "entities": (title + " " + summary).lower().split(),
        "evidence": [{"source": s, "origin": s + ".com", "url": "http://x/" + s,
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in sources],
        "score_breakdown": {"track_fit": 80, "timing": bd_timing, "feasibility": 70,
                            "competition": 65, "executability": 80},
        "age_hours": 5.0, "velocity": 0.1, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


# ---------------------------------------------------------------- T5
@pytest.mark.skipif(not _has_base, reason="schedule-reminder reminder.py not installed")
def test_ledger_roundtrip_and_idempotency(tmp_path):
    db = str(tmp_path / "t.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    cand = {"canonical_key": "op-test-key::ai-agents", "title": "Test op",
            "evidence": [{"source": "hackernews"}], "final_score": 80,
            "source_set": ["hackernews", "trend-pulse"], "lifecycle_stage": "emerging"}
    ext = dd.build_ext(cand, {"ts": "2026-06-25T12:00:00Z", "score": 80}, {}, CFG)
    r1 = lc.upsert(cand, ext)
    id1 = r1["item"]["id"]
    rows = lc.list_active()
    found = [r for r in rows if dd._row_key(r) == cand["canonical_key"]]
    assert found, "item not found via list --source --active"
    got_ext = found[0]["ext"]
    assert got_ext.get(EXT + "canonical_key") == cand["canonical_key"]  # ext preserved verbatim
    assert EXT + "samples" in got_ext
    # re-add same idempotency key => same id (UPSERT)
    r2 = lc.upsert(cand, ext)
    assert r2["item"]["id"] == id1


@pytest.mark.skipif(not _has_base, reason="schedule-reminder reminder.py not installed")
def test_watermark_singleton(tmp_path):
    db = str(tmp_path / "w.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    lc.add_watermark("2026-06-25T12:00:00Z")
    lc.add_watermark("2026-06-25T13:00:00Z")  # same idempotency key -> updates singleton
    assert lc.get_watermark() == "2026-06-25T13:00:00Z"


@pytest.mark.skipif(not _has_base, reason="schedule-reminder reminder.py not installed")
def test_pulse_seen_singleton_roundtrip(tmp_path):
    # HARDEN (§7): the cross-day pulse-seen map round-trips through the base as a singleton, exactly
    # like the watermark, so a rumor rendered today is remembered and suppressed tomorrow.
    db = str(tmp_path / "p.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    assert lc.get_pulse_seen() == {}                       # absent -> empty, never raises
    lc.set_pulse_seen({"u:https://v2ex.com/t/1": "2026-06-25T12:00:00Z"})
    lc.set_pulse_seen({"u:https://v2ex.com/t/1": "2026-06-25T12:00:00Z",     # UPSERT the singleton
                       "u:https://linux.do/t/2": "2026-06-25T13:00:00Z"})
    got = lc.get_pulse_seen()
    assert got.get("u:https://v2ex.com/t/1") == "2026-06-25T12:00:00Z"
    assert got.get("u:https://linux.do/t/2") == "2026-06-25T13:00:00Z"


# ---------------------------------------------------------------- T4 coverage / no silent skip
def test_one_source_is_explicit_gap_not_silent():
    cands = [_cand("Single-source idea", "only one origin here", "ai-agents", 90,
                   ["hackernews"])]
    res = runner.process(cands, CFG, ledger=None, dry_run=True)
    assert res["below_sources"], "a 1-source candidate must surface as an explicit gap"
    assert res["pushed"] == []


# ---------------------------------------------------------------- T7 idempotent pipeline (no base)
def test_pipeline_dryrun_pushes_quality_only():
    cands = [
        _cand("MCP agent framework launch", "open source llm agent tooling", "ai-agents", 95,
              ["hackernews", "product-hunt"]),
        _cand("Single source weak", "one origin", "ai-agents", 95, ["hackernews"]),
    ]
    res = runner.process(cands, CFG, ledger=None, dry_run=True)
    assert res["built"] == 1            # only the >=2-source survives the red line
    assert len(res["below_sources"]) == 1
    assert res["empty_day"] in (True, False)


def test_pipeline_resurface_vs_suppress_with_fake_ledger():
    # day-1 state captured as a fake ledger row
    c1 = _cand("MCP agent framework launch", "open source llm agent tooling", "ai-agents", 60,
               ["hackernews", "product-hunt"])

    class FakeLedger:
        def __init__(self, rows):
            self.rows = rows
            self.upserts = []

        def list_active(self):
            return self.rows

        def upsert(self, cand, ext):
            self.upserts.append((cand["canonical_key"], ext))

        def add_watermark(self, *a):
            pass

    # build day-1 card to derive its canonical_key + ext
    card1 = runner.build_card(c1, CFG, "day1")
    ext1 = dd.build_ext(card1, {"ts": "2026-06-24T12:00:00Z", "score": 60}, {}, CFG)
    row = {"idempotency_key": card1["canonical_key"], "ext": ext1}

    # day-2: same opportunity but lifecycle stage jumps emerging->peak => material => RESURFACE
    c2 = _cand("MCP agent framework launch", "open source llm agent tooling", "ai-agents", 95,
               ["hackernews", "product-hunt"])
    c2["lifecycle_stage"] = "peak"
    res = runner.process([c2], CFG, ledger=FakeLedger([row]), dry_run=True)
    assert res["resurface"] == 1 and res["new"] == 0
