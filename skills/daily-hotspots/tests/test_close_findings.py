#!/usr/bin/env python3
"""Regression guards for the v0.2.0 source-coverage CLOSE pass (completeness findings).

Each test FAILS on the pre-fix code and pins one verified defect:

  * F1  run.collect_roster / collect_sources crashed (AttributeError) when roster_responses /
        community arrived as a NON-dict sub-field of an otherwise-valid --sources payload.
  * F2  a preset-track (roster identity) candidate bypassed the exclude content gate entirely.
  * F3  a malformed candidate / --sources JSON crashed with an unhandled JSONDecodeError instead of
        a structured rc=1 (day held for retry).
  * F4  yield._read_jsonl returned ZERO rows on a single encoding-corrupt byte anywhere in the file.
  * F6  roster.plan_pulls had no per-run size cap (unbounded daily twitterapi fan-out).
  * F8  the weekly idempotent schedule-reminder item daily-hotspots:yield:<week> was never wired.
  * F9  init_config.py never seeded roster.json (a clean install shipped the KOL roster lane dark).

Stdlib only, no network, no live MCP / ledger; clock frozen by conftest.
"""
import importlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

import run as R
import roster as RT
from lib import load_config

Y = importlib.import_module("yield")


# ============================================================ F1 — non-dict sub-fields don't crash
def test_collect_roster_tolerates_non_dict_responses():
    roster = {"schema_version": 1, "entries": [
        {"handle": "karpathy", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-07-13T00:00:00Z", "provenance": "seed"}]}
    # responses as a non-empty LIST (has no .items()) — pre-fix: AttributeError at run.py:217.
    out = R.collect_roster(roster, ["not", "a", "dict"], cfg=load_config())
    assert out == {"signals": [], "pulls": []}
    # also a bare string / number degrade to no observations, never crash.
    assert R.collect_roster(roster, "oops", cfg=load_config())["signals"] == []
    assert R.collect_roster(roster, 42, cfg=load_config())["pulls"] == []


def test_collect_sources_tolerates_non_dict_community_and_roster_responses():
    # community as a str, roster_responses as a list — the whole --sources pass must still complete.
    out = R.collect_sources(roster=None, roster_responses=["x"], community="oops", cfg=load_config())
    assert out["signals"] == [] and out["pulls"] == []
    assert "run_id" in out


# ============================================================ F2 — preset track ≠ exclude bypass
def _preset_cand(title, track="ai-agents", origins=("hackernews", "product-hunt")):
    return {
        "title": title, "summary": "hot new drop", "track": track,
        "entities": ["thing"],
        "evidence": [{"source": s, "origin": s + ".com", "url": "http://x/" + s,
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in origins],
        "score_breakdown": {"track_fit": 85, "timing": 95, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "now", "contrarian_insight": "x really y", "action": "ship",
    }


def test_preset_track_candidate_still_hits_exclude_gate():
    cfg = load_config()
    # a preset-track candidate whose content matches the exclude list must be muted, not built.
    excl = R.build_card(_preset_cand("New memecoin airdrop giveaway pump"), cfg, "run-1")
    assert excl.get("_excluded") is True
    # precision: a CLEAN preset-track candidate is unaffected (still builds a real card).
    ok = R.build_card(_preset_cand("New multi-agent orchestration framework"), cfg, "run-1")
    assert ok is not None and not ok.get("_excluded") and ok["track"] == "ai-agents"


def test_preset_track_excluded_content_never_pushed_or_archived():
    # even with >=2 independent origins (would otherwise clear the red line), excluded content with a
    # preset track must be dropped — not scored / pushed / archived.
    cand = _preset_cand("giveaway airdrop memecoin pump", origins=("hackernews", "product-hunt"))
    res = R.process([cand], load_config(), ledger=None, dry_run=True)
    assert res["excluded"] == 1
    assert res["built"] == 0 and res["pushed"] == [] and res["archived"] == []


# ============================================================ F3 — malformed JSON -> structured rc=1
def test_malformed_candidate_json_returns_structured_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("Here are today's candidates: [ {..} ]"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--no-ledger", "--dry-run"])
    rc = R.main()
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "malformed candidate JSON"
    assert out["watermark_advanced"] is False


def test_malformed_sources_payload_returns_structured_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not: json,}"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--sources", "-", "--no-ledger"])
    rc = R.main()
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "malformed sources payload"
    assert out["pulls_written"] == 0


# ============================================================ F4 — one corrupt byte ≠ zero rows
def test_read_jsonl_recovers_intact_rows_around_a_corrupt_byte(tmp_path):
    p = tmp_path / "opportunities.jsonl"
    # a 3-line file whose MIDDLE line carries an invalid UTF-8 byte (0xFF) — a plain read_text raises
    # UnicodeDecodeError before any line parses and the whole file returns []. Tolerant decode must
    # recover the two intact rows and skip only the corrupt one.
    p.write_bytes(b'{"a": 1}\n' + b'\xff\xfe garbage not json\n' + b'{"a": 3}\n')
    rows = Y._read_jsonl(p)
    assert [r.get("a") for r in rows] == [1, 3]


# ============================================================ F6 — plan_pulls per-run cap
def _roster_n(n):
    return {"schema_version": 1, "entries": [
        {"handle": f"h{i}", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-07-13T00:00:00Z", "provenance": "seed"} for i in range(n)]}


def test_plan_pulls_honors_max_handles_per_run_cap():
    roster = _roster_n(5)
    # no knob -> no cap (byte-identical default): all 5 handles.
    assert len(RT.plan_pulls(roster, {})) == 5
    # cap=2 -> first 2 in roster order (deterministic, seeds first).
    cfg = {"sources": {"twitterapi": {"max_handles_per_run": 2}}}
    plan = RT.plan_pulls(roster, cfg)
    assert [t["handle"] for t in plan] == ["h0", "h1"]
    # garbled / non-positive knob -> no cap (never crash, never mystery-truncate).
    for bad in ("nan", 0, -3, True, None, [1]):
        assert len(RT.plan_pulls(roster, {"sources": {"twitterapi": {"max_handles_per_run": bad}}})) == 5


# ============================================================ F8 — weekly idempotent ledger item
class _KeyCapturingLedger:
    def __init__(self):
        self.calls = []

    def _run(self, verb, args):
        key = args[args.index("--idempotency-key") + 1]
        self.calls.append((verb, key))
        return {"item": {"id": "id-" + key}}


def test_register_yield_item_is_idempotent_iso_week_key():
    now = R.parse_ts("2026-06-25T12:00:00Z")
    week = Y.yield_week_key(now)
    assert week == "2026-W26"                              # 2026-06-25 is ISO week 26
    lg = _KeyCapturingLedger()
    Y.register_yield_item(lg, now=now)
    Y.register_yield_item(lg, now=now)                     # re-run same week -> same key (UPSERT)
    keys = {k for _, k in lg.calls}
    assert keys == {"daily-hotspots:yield:2026-W26"}
    assert len(lg.calls) == 2                              # both UPSERT the SAME idempotency key


# ============================================================ F9 — installer seeds roster.json
def _load_init_config():
    path = Path(__file__).resolve().parents[3] / "scripts" / "init_config.py"
    spec = importlib.util.spec_from_file_location("init_config_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_init_config_seeds_a_valid_roster(tmp_path, monkeypatch, capsys):
    ic = _load_init_config()
    monkeypatch.setattr(sys, "argv", ["init_config.py", "--out", str(tmp_path)])
    assert ic.main() == 0
    capsys.readouterr()
    rj = tmp_path / "roster.json"
    assert rj.is_file(), "a clean install must ship roster.json SEEDED, not dark"
    data = json.loads(rj.read_text(encoding="utf-8"))
    ok, errs = RT.validate_roster(data)
    assert ok, f"seeded roster must be schema-valid: {errs[:2]}"
    handles = [e["handle"] for e in data["entries"]]
    assert "karpathy" in handles and len(handles) == 15
    assert all(e["provenance"] == "seed" for e in data["entries"])
    # deterministic: a re-run SKIPs an existing roster (never clobbers the user's curation).
    monkeypatch.setattr(sys, "argv", ["init_config.py", "--out", str(tmp_path)])
    assert ic.main() == 0
    assert "SKIP (exists)" in capsys.readouterr().out
