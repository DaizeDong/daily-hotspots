#!/usr/bin/env python3
"""Harden round 4 regression guards — the source-coverage self-evolve slice (audit HARDEN round 4).

One (or a few) test(s) per verified finding; each FAILS on the pre-fix code and PASSES after the fix.
Deterministic: stdlib only, no network, no live MCP, no live config (every call passes an explicit
cfg / roster / tmp path, or points DAILY_HOTSPOTS_CONFIG at a temp dir). Clock frozen by conftest
(DAILY_HOTSPOTS_NOW = 2026-06-25T12:00:00Z).

Findings covered here:
  1. Non-finite (NaN/Infinity/1e999) yield-config values survived every clamp and crashed the ENTIRE
     weekly pass at a downstream int(inf)/int(nan); yield._coerce_num now degrades them to the default
     and verify_config.validate_yield_block flags them.
  2. A roster member's quote-tweet manufactured a SECOND independent origin from ONE pull, letting a
     tweet+its-quoted-post clear the >=2-independent-origin red line; count_independent_sources now
     discounts a purely-quote-derived origin whose parent is co-present (anti-echo-chamber).
  3. The §8/§9 self-evolve engine was inert — nothing wrote the pulls-log DENOMINATOR or ran a weekly
     yield pass from any entry point; run.py now exposes --sources (writes the denominator) and --yield
     (the weekly pass), closing the loop end-to-end.
"""
import importlib
import json
import sys
from pathlib import Path

import run as RUN          # skills/.../scripts on sys.path via conftest
import roster as RT        # noqa: F401  (kept for parity / future use)
import lib
from lib import parse_ts

Y = importlib.import_module("yield")   # 'yield' is a keyword -> import by name

# verify_config.py lives at <repo>/scripts (not the skill scripts dir).
ROOT_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))
import verify_config as vc  # noqa: E402

NOW = parse_ts("2026-06-25T12:00:00Z")
INF = float("inf")
NAN = float("nan")


# ============================================================ Finding 1: non-finite yield config
def test_coerce_num_degrades_non_finite_to_default():
    # A genuine non-finite float (JSON NaN/Infinity parse to these) -> the shipped default, never inf/nan.
    assert Y._coerce_num(INF, 30) == 30
    assert Y._coerce_num(-INF, 30) == 30
    assert Y._coerce_num(NAN, 0) == 0
    # A STRING that resolves to non-finite must NOT raise at int(f) (the pre-fix crash) -> default.
    assert Y._coerce_num("inf", 30) == 30
    assert Y._coerce_num("nan", 2) == 2
    assert Y._coerce_num("1e999", 30) == 30
    # An astronomically large int that overflows float() also degrades, not crashes.
    assert Y._coerce_num(10 ** 400, 30) == 30
    # ...while ordinary finite values are unchanged (byte-identical to before).
    assert Y._coerce_num(45, 30) == 45
    assert Y._coerce_num(0.1, 0.1) == 0.1
    assert Y._coerce_num("14", 30) == 14          # numeric string still coerces
    assert Y._coerce_num("garbage", 7) == 7


def test_yield_cfg_neutralizes_non_finite_in_every_knob():
    ycfg = Y.yield_cfg({"yield": {"window_days": INF, "propose_add_min_count": INF,
                                  "prune_after_weeks": NAN, "noisy_pull_min": INF,
                                  "pre_viral_faves_threshold": INF}})
    # each falls back to its finite default; the pass now has a defined, comparison-safe threshold.
    assert ycfg["window_days"] == 30
    assert ycfg["propose_add_min_count"] == 2
    assert ycfg["prune_after_weeks"] == 2
    assert ycfg["noisy_pull_min"] == 10
    assert ycfg["pre_viral_faves_threshold"] == 500


def test_run_yield_survives_infinity_window_days_via_full_load_config(tmp_path):
    # The finding's live repro: watchlist.json {"yield":{"window_days":1e999}} -> load_config keeps
    # window_days=inf (inf >= floor), then compute_yield's int(inf) took the WHOLE pass down with no
    # report / no prune / no propose-add. After the fix window_days coerces to the default and the pass
    # completes.
    wl = tmp_path / "watchlist.json"
    wl.write_text('{"yield": {"window_days": 1e999}}', encoding="utf-8")
    cfg = lib.load_config(str(wl))
    rep = Y.run_yield({"schema_version": 1, "entries": []}, [], [], cfg=cfg, now=NOW)  # must not raise
    assert rep["window_days"] == 30                      # non-finite dropped to default
    assert isinstance(rep["prune"], list) and isinstance(rep["propose_add"], list)


def test_run_yield_survives_infinity_propose_add_min_count(tmp_path):
    # The sibling repro: propose_add_min_count is NOT clamped by lib, so an Infinity reached
    # decide_propose_add's int(inf) -> OverflowError. Now coerced to the default.
    wl = tmp_path / "watchlist.json"
    wl.write_text('{"yield": {"propose_add_min_count": Infinity}}', encoding="utf-8")
    cfg = lib.load_config(str(wl))
    rep = Y.run_yield({"schema_version": 1, "entries": []}, [], [], cfg=cfg, now=NOW)  # must not raise
    assert rep["propose_add"] == []


def test_validate_yield_block_flags_non_finite():
    ok, errs = vc.validate_yield_block({"window_days": INF})
    assert ok is False and any("window_days" in e and "finite" in e.lower() for e in errs)
    ok2, errs2 = vc.validate_yield_block({"propose_add_min_count": NAN})
    assert ok2 is False and any("propose_add_min_count" in e for e in errs2)
    # a finite (if out-of-guardrail) value is still accepted as a NUMBER by the finite check
    ok3, _ = vc.validate_yield_block({"window_days": 45})
    assert ok3 is True


# ============================================================ Finding 2: quote-tweet fake 2nd origin
def _ev(origin, url, **extra):
    d = {"source": "twitterapi", "origin": origin, "url": url}
    d.update(extra)
    return d


def test_quote_derivative_does_not_manufacture_second_independent_origin():
    # A roster member (karpathy) + the non-roster voice it QUOTED (evalmaxxer, via_handle=karpathy)
    # come from ONE pull about the SAME content -> they must NOT count as two independent origins.
    ev = [
        _ev("x.com/karpathy", "https://x.com/karpathy/1", origin_handle="karpathy"),
        _ev("x.com/karpathy", "https://x.com/karpathy/2", origin_handle="karpathy"),
        _ev("x.com/evalmaxxer", "https://x.com/evalmaxxer/9", origin_handle="evalmaxxer",
            via_handle="karpathy"),
    ]
    assert RUN.count_independent_sources(ev) == 1        # pre-fix: 2 (false >=2-source card)


def test_independently_collected_quoted_voice_still_corroborates():
    # PRECISION: if the same voice ALSO appears as an independently-collected (non-quote) signal — the
    # keyword search surfaced it too — it is NOT discounted and genuinely corroborates -> 2 origins.
    ev = [
        _ev("x.com/karpathy", "https://x.com/karpathy/1"),
        _ev("x.com/evalmaxxer", "https://x.com/evalmaxxer/9", via_handle="karpathy"),   # quote-derived
        _ev("x.com/evalmaxxer", "https://x.com/evalmaxxer/9b"),                          # independent
    ]
    assert RUN.count_independent_sources(ev) == 2


def test_two_distinct_roster_handles_still_clear_the_red_line():
    # No false NEGATIVE: two genuinely different roster handles (neither a quote-derivative) still count.
    ev = [_ev("x.com/karpathy", "u1"), _ev("x.com/swyx", "u2")]
    assert RUN.count_independent_sources(ev) == 2


def test_quote_guard_leaves_non_quote_evidence_untouched():
    # The transload / ordinary path (no via_handle anywhere) is unaffected by the new guard.
    ev = [_ev(a, "http://" + a + "/x") for a in ("hn", "ph", "github")]
    assert RUN.count_independent_sources(ev) == 3


# ============================================================ Finding 3: engine wired (denominator + weekly pass)
def _v2ex_sources_payload():
    return {"community": {"v2ex": [
        {"title": "AI agent tool", "url": "https://v2ex/1", "category": "create", "heat": 5,
         "ts": "2026-06-25T08:00:00Z", "summary": "x"},
    ]}}


def test_run_sources_writes_the_pulls_log_denominator(tmp_path, monkeypatch, capsys):
    # THE finding-3 core: append_pulls was never called from any entry point, so the pulls-log
    # DENOMINATOR was never written and every yield stayed 'unknown'. run.py --sources now writes it.
    monkeypatch.setenv(lib.CONFIG_ENV, str(tmp_path / "cfg"))   # hermetic: empty companion -> defaults
    (tmp_path / "cfg").mkdir()
    archive = tmp_path / "archive"
    sfile = tmp_path / "sources.json"
    sfile.write_text(json.dumps(_v2ex_sources_payload()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["run.py", "--sources", str(sfile), "--archive-dir", str(archive)])
    rc = RUN.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pulls_written"] == 1 and out["pulls_log"]        # a denominator line was written
    pulls_files = list(archive.glob("pulls-*.jsonl"))
    assert pulls_files, "run.py --sources MUST write the pulls-log denominator (was never written)"
    lines = [json.loads(x) for x in pulls_files[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(l.get("source") == "v2ex" for l in lines)


def test_dry_run_sources_writes_no_denominator(tmp_path, monkeypatch, capsys):
    # A preview must never inflate the denominator (mirrors archive/push dry_run).
    monkeypatch.setenv(lib.CONFIG_ENV, str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    archive = tmp_path / "archive"
    sfile = tmp_path / "sources.json"
    sfile.write_text(json.dumps(_v2ex_sources_payload()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["run.py", "--sources", str(sfile), "--archive-dir", str(archive), "--dry-run"])
    assert RUN.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pulls_log"] is None
    assert list(archive.glob("pulls-*.jsonl")) == []            # nothing written on dry-run


def test_pulls_log_closes_the_yield_denominator_loop(tmp_path, monkeypatch, capsys):
    # End-to-end: after run.py --sources writes the denominator, the yield engine READS it and the
    # source's yield becomes a NUMBER (0 contributions / N pulls), not the permanent 'unknown' (None)
    # the pre-fix inert engine produced for every origin.
    monkeypatch.setenv(lib.CONFIG_ENV, str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    archive = tmp_path / "archive"
    sfile = tmp_path / "sources.json"
    sfile.write_text(json.dumps(_v2ex_sources_payload()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["run.py", "--sources", str(sfile), "--archive-dir", str(archive)])
    assert RUN.main() == 0
    capsys.readouterr()

    pulls = Y.load_pulls(str(archive))
    assert pulls, "the yield engine must find the denominator run.py --sources wrote"
    later = parse_ts("2026-06-26T12:00:00Z")                    # a day after the pull -> in 30d window
    yields = Y.compute_yield([], pulls, later, Y.yield_cfg({}))
    st = yields.get(Y.okey(Y.KIND_SOURCE, "v2ex"))
    assert st is not None and st["pulls"] == 1
    assert st["yield"] == 0.0                                   # KNOWN (0/1), not None (unknown)


def test_run_yield_cli_entrypoint_exists(tmp_path, monkeypatch, capsys):
    # The spec §8 "runnable as run.py --yield" surface must exist and emit the yield report shape.
    monkeypatch.setenv(lib.CONFIG_ENV, str(tmp_path))          # empty companion -> defaults, empty roster
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(sys, "argv", ["run.py", "--yield", "--archive-dir", str(archive)])
    rc = RUN.main()
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert {"cold_start", "yields", "prune", "propose_add", "window_days"} <= set(rep)
