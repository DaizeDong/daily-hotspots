"""test_yield_temporal.py, TEMPORAL simulation of the self-evolve yield engine (spec §8/§9).

The unit tests in test_yield.py check the engine on SINGLE frozen snapshots. This file checks the
engine's behavior ACROSS SIMULATED WEEKS: it programmatically generates one multi-week, append-only
history (``opportunities.jsonl`` + month-split ``pulls-YYYY-MM.jsonl``) and REPLAYS it at a sequence
of weekly checkpoints, advancing only the injected clock (``now=``, the same frozen-clock seam the
engine already supports). No network, no live MCP, no live config: deterministic, stdlib-only.

Synthetic cast (all timestamps are day-offsets from EPOCH = 2026-05-04):

  * ``steady``, rostered, productive: a >=2-origin card every week, pulled daily. NEVER pruned.
  * ``deadhandle``, rostered, zero-yield from day 0: pulled daily (kept=0), contributes nothing.
                     Report-only during cold-start, then auto-pruned once history >= 7 days.
  * ``fader``, rostered, productive weeks 1-2 then goes quiet: demonstrates that a prune fires
                     only AFTER ``prune_after_weeks`` CONSECUTIVE below-floor weeks (not the instant
                     it goes quiet). Its cards clear the pre-viral faves floor so the pre-viral guard
                     does not confound the timing.
  * ``ghost``, rostered, UNKNOWN-yield: contributes a card but is NEVER in the pulls-log
                     (missing denominator). Must be excluded from pruning and never read as 0.
  * ``newcomer``, NOT rostered: appears frequently in evidence from week 3 on. Must surface in the
                     propose-add queue (ranked by frequency) but NEVER be auto-added.
  * ``linux.do``, a productive community SOURCE (not a handle): exercises the source lane and
                     confirms sources are neither pruned nor proposed-as-a-handle.

Weekly checkpoints (the injected ``now`` at each pass), 7 days apart:

  idx 0 = day  6  (< 7 days of history  -> COLD-START, report-only)
  idx 1 = day 13  (deadhandle pruned: dead since day 0, now fully-observed)
  idx 2 = day 20  (fader has only ONE quiet week -> NOT yet pruned)
  idx 3 = day 27  (fader now has TWO consecutive quiet weeks -> pruned)
  idx 4..6        (nothing left to prune)
"""
import copy
import importlib
import json
from datetime import timedelta
from pathlib import Path

import importlib.util

_source11_spec = importlib.util.spec_from_file_location(
    "source11_" + Path(__file__).stem, Path(__file__).parents[3] / "tools" / "make_fixtures.py")
_source11_gen = importlib.util.module_from_spec(_source11_spec)
_source11_spec.loader.exec_module(_source11_gen)

import pytest

import roster as R
from lib import iso, parse_ts

# ``yield`` is a Python keyword -> load the module by string name (mirrors test_yield.py).
Y = importlib.import_module("yield")

# --------------------------------------------------------------------------- synthetic clock grid
_SCHEDULE = _source11_gen.source11_temporal_schedule()
EPOCH = parse_ts(_SCHEDULE["epoch"])

def _iso(day: int, hour: int = 0) -> str:
    return _source11_gen.source11_temporal_iso(day, hour)


# Pulls land at 08:00, cards at 09:00, and each weekly pass runs at 12:00, so on any given day the
# pull precedes the card precedes the yield pass, exactly as the live pipeline orders them.
PULL_HOUR, CARD_HOUR, PASS_HOUR = (_SCHEDULE[key] for key in ("pull_hour", "card_hour", "pass_hour"))
SIM_DAYS = _SCHEDULE["days"]
CHECKPOINT_DAYS = _SCHEDULE["checkpoints"]
CHECKPOINTS = [parse_ts(_iso(day, PASS_HOUR)) for day in CHECKPOINT_DAYS]
STEADY_CARD_DAYS, FADER_CARD_DAYS = _SCHEDULE["steady"], _SCHEDULE["fader"]
GHOST_CARD_DAY = _SCHEDULE["ghost"]
NEWCOMER_CARD_DAYS, LINUXDO_CARD_DAYS = _SCHEDULE["newcomer"], _SCHEDULE["linux.do"]


# --------------------------------------------------------------------------- history generation
def _handle_ev(handle: str, faves: int, n: int = 1) -> dict:
    return _source11_gen.source11_temporal_handle(handle, faves, n)


def _source_ev(name: str, n: int = 1) -> dict:
    return _source11_gen.source11_temporal_source(name, n)


def _card(oid: str, day: int, track: str, evidence: list, pushed: bool) -> dict:
    return _source11_gen.source11_temporal_card(oid, day, track, evidence, pushed)


def _pull(day: int, kept: int, handle: str | None = None, source: str | None = None,
          pulled: int = 5) -> dict:
    return _source11_gen.source11_temporal_pull(day, kept, handle, source, pulled)


def _build_history() -> tuple[list, list]:
    """Generate the fictional archive through the fixture tool."""
    return _source11_gen.source11_temporal_history()


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8", newline="\n")


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    """Generate the archive on disk ONCE and load it back through the engine's real JSONL readers.

    Splitting the pulls-log by calendar month (day 0..27 -> May, day 28..48 -> June) exercises
    load_pulls' multi-file glob+merge, not just an in-memory list."""
    d = tmp_path_factory.mktemp("yield_temporal")
    records, pulls = _build_history()
    _write_jsonl(d / "opportunities.jsonl", records)
    by_month: dict = {}
    for p in pulls:
        by_month.setdefault(p["ts"][:7], []).append(p)   # "2026-05" / "2026-06"
    for month, rows in by_month.items():
        _write_jsonl(d / f"pulls-{month}.jsonl", rows)
    return {
        "dir": d,
        "records": Y.load_opportunities(str(d)),
        "pulls": Y.load_pulls(str(d)),
        "checkpoints": CHECKPOINTS,
    }


# --------------------------------------------------------------------------- replay driver
def _as_of(rows: list, key: str, now) -> list:
    """The append-only slice visible AT ``now`` (honest replay: nothing timestamped in the future)."""
    return [r for r in rows if parse_ts(r[key]) <= now]


def _replay(sim, cfg=None, apply=True):
    """Step the engine through every weekly checkpoint on ONE persistent roster.

    Returns ``(roster, reports)`` where reports[i] is run_yield's report at checkpoint i. Auto-prune
    is applied in place at each step (reversible enabled=false), so a handle disabled at week i stays
    disabled at week i+1, exactly the live weekly cadence."""
    roster = _fresh_roster()
    reports = []
    for now in sim["checkpoints"]:
        recs = _as_of(sim["records"], "last_seen", now)
        pls = _as_of(sim["pulls"], "ts", now)
        reports.append(Y.run_yield(roster, recs, pls,
                                   cfg={} if cfg is None else cfg, now=now, apply=apply))
    return roster, reports


def _fresh_roster() -> dict:
    """A fresh roster each replay so mutation (auto-prune) never bleeds between tests."""
    def e(handle, track):
        return {"handle": handle, "track": track, "tier": 1, "enabled": True,
                "added_at": _iso(0, 0), "provenance": "seed"}
    return {"schema_version": 1, "entries": [
        e("steady", "ai-agents"), e("deadhandle", "dev-tools"),
        e("fader", "dev-tools"), e("ghost", "ai-agents")]}


def _prune_handles(rep) -> list:
    return sorted(d["handle"] for d in rep["prune"])


def _first_prune_idx(reports, handle):
    for i, rep in enumerate(reports):
        if any(d["handle"] == handle for d in rep["prune"]):
            return i
    return None


def _propose_handles(rep) -> set:
    return {c["handle"] for c in rep["propose_add"]}


# =================================================================== sanity: the sim is well-formed
def test_history_spans_two_months_and_loads(sim):
    # the multi-file pulls glob merged May + June -> the replay really crosses a month boundary.
    files = sorted(p.name for p in sim["dir"].glob("pulls-*.jsonl"))
    assert files == ["pulls-2026-05.jsonl", "pulls-2026-06.jsonl"]
    assert sim["pulls"] and any(p["ts"].startswith("2026-06") for p in sim["pulls"])
    assert sim["records"] and (sim["dir"] / "opportunities.jsonl").is_file()


# =================================================================== (a) cold-start report-only (<7d)
def test_first_week_is_cold_start_report_only_no_prune(sim):
    _, reports = _replay(sim, apply=True)
    c0 = reports[0]                                   # day 6 -> < 7 days of real history
    assert c0["history_days"] < 7
    assert c0["cold_start"] is True and c0["report_only"] is True
    assert c0["prune"] == []                          # deadweight is dead already, yet nothing pruned...
    assert c0["applied"] is False                     # ...and apply=True is a safe no-op on cold-start
    # the very next week crosses the 7-day line -> pruning goes live (the timeline actually transitions)
    assert reports[1]["history_days"] >= 7 and reports[1]["cold_start"] is False


def test_deadhandle_survives_cold_start_then_is_disabled_after(sim):
    roster, reports = _replay(sim, apply=True)
    # it existed and stayed enabled through the cold-start pass; only a LATER live week disables it.
    assert R.find_entry(roster, "deadhandle") is not None
    assert _first_prune_idx(reports, "deadhandle") == 1   # first LIVE week, not the cold-start week 0


# =================================================================== (b) auto-prune after N below-floor weeks
def test_zero_yield_handle_pruned_after_consecutive_below_floor_weeks(sim):
    roster, reports = _replay(sim, apply=True)
    # 'fader' is productive weeks 1-2, then quiet. The prune must NOT fire the instant it goes quiet
    # (one quiet week), only after prune_after_weeks (=2) CONSECUTIVE below-floor, fully-observed weeks.
    assert "fader" not in _prune_handles(reports[1])      # still contributing (last card day 11)
    assert "fader" not in _prune_handles(reports[2])      # exactly ONE quiet week so far -> spared
    assert "fader" in _prune_handles(reports[3])          # TWO consecutive quiet weeks -> pruned
    assert _first_prune_idx(reports, "fader") == 3
    # reversible: enabled=false, NOT deleted; a human can flip it back.
    e = R.find_entry(roster, "fader")
    assert e is not None and e["enabled"] is False
    R.set_enabled(roster, "fader", True)
    assert R.find_entry(roster, "fader")["enabled"] is True


def test_prune_decision_logs_reason_and_stats(sim):
    roster, reports = _replay(sim, apply=True)
    # the prune decision at the firing week carries a reason + stats (auditable, §8)...
    fader_dec = next(d for d in reports[3]["prune"] if d["handle"] == "fader")
    assert "floor" in fader_dec["reason"] and fader_dec["contributions"] == 0
    assert fader_dec["weeks"] == 2 and fader_dec["floor"] == 0 and fader_dec["pulls"] >= 2
    # ...and the reason is STAMPED durably onto the disabled entry's notes (survives to next week).
    assert R.find_entry(roster, "fader")["notes"].startswith("auto-pruned")
    assert "floor" in R.find_entry(roster, "fader")["notes"]
    # a pruned-in-a-PRIOR-week handle stays discoverable in the durable un-prune list.
    disabled_last = {e["handle"] for e in reports[-1]["disabled"]}
    assert {"deadhandle", "fader"} <= disabled_last


def test_exact_prune_signature_across_the_timeline(sim):
    _, reports = _replay(sim, apply=True)
    # the whole temporal fingerprint, pinned week by week.
    assert [_prune_handles(r) for r in reports] == [
        [], ["deadhandle"], [], ["fader"], [], [], []]


# =================================================================== (c) productive handle never pruned
def test_productive_handle_is_never_pruned(sim):
    roster, reports = _replay(sim, apply=True)
    for i, rep in enumerate(reports):
        assert "steady" not in _prune_handles(rep), f"steady pruned at checkpoint {i}"
    assert R.find_entry(roster, "steady")["enabled"] is True     # still enabled at the end
    # its yield is a real, positive ratio once out of cold-start (contributes + is pulled).
    y = reports[-1]["yields"][Y.okey(Y.KIND_HANDLE, "steady")]
    assert y["contributions"] >= 1 and y["pulls"] >= 1 and y["yield"] and y["yield"] > 0


def test_productive_source_is_never_pruned_or_proposed(sim):
    _, reports = _replay(sim, apply=True)
    for rep in reports:
        assert "linux.do" not in _prune_handles(rep)             # sources are never prune candidates
        assert "linux.do" not in _propose_handles(rep)           # ...nor proposed as an X handle
    ld = reports[-1]["yields"][Y.okey(Y.KIND_SOURCE, "linux.do")]
    assert ld["pulls"] >= 1 and ld["yield"] and ld["yield"] > 0  # a real source yield over time


# =================================================================== (d) propose-add, never auto-add
def test_frequent_nonroster_handle_emerges_in_propose_add_over_time(sim):
    _, reports = _replay(sim, apply=True)
    assert "newcomer" not in _propose_handles(reports[0])        # not seen yet (first card day 15)
    assert "newcomer" not in _propose_handles(reports[1])        # still < the min-count in window
    # from week 3 on it is proposed, ranked by frequency, above propose_add_min_count (=2).
    c2 = {c["handle"]: c for c in reports[2]["propose_add"]}
    assert set(c2) == {"newcomer"} and c2["newcomer"]["count"] == 2
    assert c2["newcomer"]["sample_url"].startswith("https://x.com/newcomer")
    for i in (2, 3, 4, 5, 6):
        assert "newcomer" in _propose_handles(reports[i])


def test_propose_add_never_auto_adds_the_handle(sim):
    roster, reports = _replay(sim, apply=True)      # apply=True at EVERY checkpoint
    assert any("newcomer" in _propose_handles(r) for r in reports)   # it WAS proposed...
    assert R.find_entry(roster, "newcomer") is None                  # ...but never entered the roster
    # apply only ever DISABLES rows; the roster's handle set never grew (no auto-add, no delete).
    assert {e["handle"] for e in R.entries_of(roster)} == {"steady", "deadhandle", "fader", "ghost"}


# =================================================================== (e) unknown-yield exclusion
def test_unknown_yield_handle_never_pruned_and_never_read_as_zero(sim):
    roster, reports = _replay(sim, apply=True)
    # ghost contributes but is NEVER in the pulls-log -> across the WHOLE timeline it is never pruned.
    for i, rep in enumerate(reports):
        assert "ghost" not in _prune_handles(rep), f"ghost pruned at checkpoint {i}"
    assert R.find_entry(roster, "ghost")["enabled"] is True

    # unknown != zero, made concrete at week 1: deadhandle (has pulls, 0 contributions) reads 0.0 and
    # is a prune target; ghost (0 pulls) reads None and is NOT, same emptiness, opposite verdict,
    # the only difference being whether a denominator exists.
    y1 = reports[1]["yields"]
    assert y1[Y.okey(Y.KIND_HANDLE, "deadhandle")]["yield"] == 0.0
    gy1 = y1[Y.okey(Y.KIND_HANDLE, "ghost")]
    assert gy1["pulls"] == 0 and gy1["yield"] is None and gy1["yield"] != 0


def test_unknown_yield_survives_the_deadweight_shaped_trap(sim):
    _, reports = _replay(sim, apply=True)
    # week 4 (day 34): ghost's only contribution (day 10) has aged out of the recent 2 prune weeks, so
    # it now presents the SAME shape a naive engine prunes deadhandle for, 0 recent contributions ,
    # yet it is spared because its weeks are UNOBSERVED (pulls=0), never fabricated to 0 (§9).
    c4 = reports[4]
    gy = c4["yields"][Y.okey(Y.KIND_HANDLE, "ghost")]
    assert gy["contributions"] == 1 and gy["pulls"] == 0 and gy["yield"] is None
    assert "ghost" not in _prune_handles(c4)


# =================================================================== (f) thresholds config-driven
def test_prune_after_weeks_threshold_shifts_the_prune_week(sim):
    # methodology constant, threshold tunable: raising prune_after_weeks to 3 requires a THIRD
    # consecutive quiet week, so fader's prune slides one checkpoint later (day 27 -> day 34).
    _, base = _replay(sim, cfg={}, apply=True)
    _, slow = _replay(sim, cfg={"yield": {"prune_after_weeks": 3}}, apply=True)
    assert _first_prune_idx(base, "fader") == 3
    assert _first_prune_idx(slow, "fader") == 4                  # strictly later, driven only by config
    assert base[3]["prune_after_weeks"] == 2 and slow[3]["prune_after_weeks"] == 3


def test_min_history_days_threshold_extends_cold_start(sim):
    # raising min_history_days to 15 keeps week 1 (day 13, ~13d history) in report-only, so deadhandle's
    # first prune slides from week 1 to week 2, the cold-start gate is read from config, not hardcoded.
    _, base = _replay(sim, cfg={}, apply=True)
    _, strict = _replay(sim, cfg={"yield": {"min_history_days": 15}}, apply=True)
    assert base[1]["cold_start"] is False and strict[1]["cold_start"] is True
    assert _first_prune_idx(base, "deadhandle") == 1
    assert _first_prune_idx(strict, "deadhandle") == 2
    assert strict[1]["min_history_days"] == 15


def test_floor_threshold_is_read_from_config(sim):
    # the reported floor tracks config (tightening to -1 is honored by the §9 clamp; the shipped 0 is
    # the cap it may never exceed). Under the default floor 0, a 0-contribution handle is "below floor"
    # and deadhandle is pruned at week 1. Tightening the floor to -1 makes the deadness bar
    # (contributions <= -1) UNREACHABLE for any real handle, so the SAME timeline now prunes nothing ,
    # the outcome is driven entirely by the config threshold, not a hardcoded 0.
    _, base = _replay(sim, cfg={}, apply=True)
    _, tight = _replay(sim, cfg={"yield": {"floor": -1}}, apply=True)
    assert base[1]["floor"] == 0 and tight[1]["floor"] == -1
    assert _first_prune_idx(base, "deadhandle") == 1
    assert _first_prune_idx(tight, "deadhandle") is None            # bar unreachable -> spared
    assert all(rep["prune"] == [] for rep in tight)                 # nothing at all is pruned


# =================================================================== determinism
def test_replay_is_deterministic(sim):
    _, a = _replay(sim, apply=True)
    _, b = _replay(sim, apply=True)
    # byte-identical reports across two independent replays of the same synthetic timeline.
    assert json.dumps(a, ensure_ascii=False, sort_keys=True, default=list) == \
           json.dumps(b, ensure_ascii=False, sort_keys=True, default=list)


def test_report_only_replay_mutates_nothing(sim):
    # a full apply=False replay leaves the roster pristine (report-only truly writes nothing).
    roster, reports = _replay(sim, apply=False)
    assert all(rep["applied"] is False for rep in reports)
    assert copy.deepcopy(R.entries_of(roster)) == R.entries_of(_fresh_roster())
