"""test_yield.py — the self-evolve signal-yield engine (design spec §8/§9).

Replays the append-only fixture archive (opportunities.jsonl + pulls-*.jsonl) against the small
roster fixture and pins the engine's contract:

  * yield math  = contributions / pulls over a rolling window (§8);
  * AUTO-PRUNE  = below floor for prune_after_weeks consecutive OBSERVED weeks -> enabled=false (§8);
  * PROPOSE-ADD = unrostered handles ranked by frequency into the review queue, NEVER auto-added (§8);
  * cold-start  = report-only until >= min_history_days of real history, no pruning (§9);
  * reversible  = prune sets enabled=false, never deletes (§9);
  * no-fabricate = a missing pulls-log entry is yield=None (unknown), NOT 0, and is prune-excluded (§9);
  * thresholds  = config-driven, not hardcoded (§9).

Deterministic: stdlib only, no network, no live MCP, no live config. The clock is frozen (conftest
+ an explicit NOW) and every run passes cfg={} so the module defaults apply and nothing probes the
companion repo.
"""
import copy
import importlib
import json
from pathlib import Path

import pytest

import roster as R
from lib import parse_ts

# ``yield`` is a Python keyword -> the module cannot be a bare ``import``; load it by string name.
Y = importlib.import_module("yield")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "yield"
NOW = parse_ts("2026-06-25T12:00:00Z")   # matches the frozen conftest clock the fixtures were cut to
YCFG = Y.yield_cfg({})                    # module defaults (window 30d, floor 0, prune_after_weeks 2)


def _roster() -> dict:
    """A FRESH normalized copy of the roster fixture (mutation tests must not bleed into each other)."""
    return R.load_roster(path=str(FIXTURES / "roster.json"))


def _records() -> list:
    return Y.load_opportunities(str(FIXTURES))


def _pulls() -> list:
    return Y.load_pulls(str(FIXTURES))


def _handles(roster) -> set:
    return {e.get("handle") for e in R.entries_of(roster)}


def _prune_handles(report) -> list:
    return [d["handle"] for d in report["prune"]]


# =================================================================== yield math (§8)
def test_yield_ratios_per_origin():
    y = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    # karpathy: 2 cards (op-a, op-b) over 4 pulls (06-19..06-22) -> 0.5
    assert y[Y.okey(Y.KIND_HANDLE, "karpathy")]["yield"] == 0.5
    # deadweight: 0 cards over 4 pulls -> 0.0 (a KNOWN zero, distinct from unknown/None below)
    assert y[Y.okey(Y.KIND_HANDLE, "deadweight")]["yield"] == 0.0
    # sometimes: 0 cards over 1 pull -> 0.0
    assert y[Y.okey(Y.KIND_HANDLE, "sometimes")]["yield"] == 0.0
    # linux.do (a SOURCE, namespaced separately from handles): 3 cards over 3 pulls -> 1.0
    assert y[Y.okey(Y.KIND_SOURCE, "linux.do")]["yield"] == 1.0


def test_yield_counts_contributions_once_per_card():
    y = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    k = y[Y.okey(Y.KIND_HANDLE, "karpathy")]
    assert k["contributions"] == 2 and k["pulls"] == 4
    ld = y[Y.okey(Y.KIND_SOURCE, "linux.do")]
    assert ld["contributions"] == 3 and ld["pulls"] == 3


def test_yield_secondary_metrics_pushed_and_pre_viral():
    y = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    k = y[Y.okey(Y.KIND_HANDLE, "karpathy")]
    # op-a is pushed (op-b is not) -> 1 pushed contribution
    assert k["pushed_contributions"] == 1
    # op-a faves=120 < 500 (a pre-viral catch keyword search would have dropped); op-b faves=800 not
    assert k["pre_viral"] == 1
    ld = y[Y.okey(Y.KIND_SOURCE, "linux.do")]
    assert ld["pushed_contributions"] == 1        # op-c pushed; op-d/op-e not
    assert ld["pre_viral"] == 0                    # community items carry no faves -> never pre-viral


def test_handle_and_source_keys_do_not_collide():
    y = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    assert Y.okey(Y.KIND_HANDLE, "karpathy") in y
    assert Y.okey(Y.KIND_SOURCE, "linux.do") in y
    # a source key and a handle key are distinct namespaces even if the raw names rhymed
    assert Y.okey(Y.KIND_HANDLE, "linux.do") != Y.okey(Y.KIND_SOURCE, "linux.do")


def test_compute_yield_is_deterministic():
    a = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    b = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    assert a == b


# =================================================================== unknown-yield (§9 no-fabrication)
def test_missing_pulls_is_unknown_yield_not_zero():
    # hotfounder reached 2 cards (op-f, op-g) but has NO pulls-log line: yield is UNKNOWN (None),
    # never coerced to 0 — the numerator without a denominator is not a real ratio (§9).
    y = Y.compute_yield(_records(), _pulls(), NOW, YCFG)
    hf = y[Y.okey(Y.KIND_HANDLE, "hotfounder")]
    assert hf["contributions"] == 2
    assert hf["pulls"] == 0
    assert hf["yield"] is None            # UNKNOWN, not 0.0
    assert hf["yield"] != 0               # explicit: missing pulls != 0


def test_unknown_yield_handle_is_excluded_from_prune():
    # A rostered, enabled handle that contributed but was NEVER pulled -> yield unknown -> it must
    # NOT be pruned (can't call a handle dead when we never measured its denominator).
    roster = {"schema_version": 1, "entries": [
        {"handle": "unpulled", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"},
    ]}
    records = [{"opportunity_id": "x", "first_seen": "2026-06-20T09:00:00Z",
                "last_seen": "2026-06-20T09:00:00Z", "pushed": True, "track": "ai-agents",
                "evidence": [{"origin_handle": "unpulled", "url": "https://x.com/unpulled/1"}]}]
    decisions = Y.decide_prune(roster, records, [], YCFG, NOW)
    assert decisions == []                # no pulls anywhere -> unobserved -> spared


# =================================================================== auto-prune decision (§8)
def test_prune_targets_only_the_dead_handle():
    decisions = Y.decide_prune(_roster(), _records(), _pulls(), YCFG, NOW)
    handles = [d["handle"] for d in decisions]
    assert handles == ["deadweight"]      # pulled every week, zero contributions -> below floor
    d = decisions[0]
    assert d["contributions"] == 0
    assert d["pulls"] >= 2                 # every one of the last 2 weeks was observed (p >= 1)
    assert d["weeks"] == 2 and d["floor"] == 0
    assert "floor" in d["reason"]


def test_prune_spares_productive_handle():
    handles = [d["handle"] for d in Y.decide_prune(_roster(), _records(), _pulls(), YCFG, NOW)]
    assert "karpathy" not in handles      # 2 contributions in-window -> above floor -> kept


def test_prune_spares_handle_with_an_unobserved_week():
    # 'sometimes' was pulled only in the most recent week; the prior week has 0 pulls -> UNKNOWN, not
    # a below-floor week -> the consecutive run is broken -> spared (§9 unknown exclusion at week grain).
    handles = [d["handle"] for d in Y.decide_prune(_roster(), _records(), _pulls(), YCFG, NOW)]
    assert "sometimes" not in handles


def test_decide_prune_does_not_mutate_roster():
    roster = _roster()
    before = copy.deepcopy(R.entries_of(roster))
    Y.decide_prune(roster, _records(), _pulls(), YCFG, NOW)
    assert R.entries_of(roster) == before  # pure decision — application happens only in run_yield(apply=True)


def test_prune_thresholds_are_config_driven():
    # Raising prune_after_weeks to 3 means the third-back week must also be observed; it isn't for
    # deadweight (pulls only span 2 weeks) -> deadweight is now SPARED. Methodology constant, threshold tunable (§9).
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={"yield": {"prune_after_weeks": 3}}, now=NOW)
    assert _prune_handles(rep) == []


# =================================================================== apply = enabled=false, reversible (§8/§9)
def test_apply_sets_enabled_false_on_pruned_handle():
    roster = _roster()
    rep = Y.run_yield(roster, _records(), _pulls(), cfg={}, now=NOW, apply=True)
    assert rep["applied"] is True
    assert _prune_handles(rep) == ["deadweight"]
    assert R.find_entry(roster, "deadweight")["enabled"] is False
    assert R.find_entry(roster, "karpathy")["enabled"] is True   # productive handle untouched
    assert R.find_entry(roster, "sometimes")["enabled"] is True  # spared handle untouched


def test_prune_is_reversible_not_a_delete():
    roster = _roster()
    n_before = len(R.entries_of(roster))
    Y.run_yield(roster, _records(), _pulls(), cfg={}, now=NOW, apply=True)
    # still present, just disabled — nothing was deleted
    assert len(R.entries_of(roster)) == n_before
    assert R.find_entry(roster, "deadweight") is not None
    assert R.find_entry(roster, "deadweight")["enabled"] is False
    # a human can un-prune by flipping it back
    R.set_enabled(roster, "deadweight", True)
    assert R.find_entry(roster, "deadweight")["enabled"] is True


def test_report_only_default_applies_nothing():
    roster = _roster()
    rep = Y.run_yield(roster, _records(), _pulls(), cfg={}, now=NOW)   # apply defaults to False
    assert rep["applied"] is False
    assert R.find_entry(roster, "deadweight")["enabled"] is True       # nothing written


# =================================================================== propose-add queue (§8)
def test_propose_add_lists_unrostered_handle():
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={}, now=NOW)
    pa = {c["handle"]: c for c in rep["propose_add"]}
    assert "hotfounder" in pa              # seen in op-f/op-g evidence, not in roster
    assert pa["hotfounder"]["count"] == 2
    assert pa["hotfounder"]["tracks"] == ["dev-tools"]
    assert pa["hotfounder"]["sample_url"].startswith("https://x.com/hotfounder")


def test_propose_add_excludes_rostered_and_source_origins():
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={}, now=NOW)
    proposed = {c["handle"] for c in rep["propose_add"]}
    assert "karpathy" not in proposed      # already rostered -> not an add candidate
    assert "linux.do" not in proposed      # a SOURCE, never proposed as an X handle


def test_propose_add_respects_min_count_config():
    # hotfounder appears on exactly 2 cards; raise the floor to 3 and it drops out of the queue.
    rep = Y.run_yield(_roster(), _records(), _pulls(),
                      cfg={"yield": {"propose_add_min_count": 3}}, now=NOW)
    assert rep["propose_add"] == []


# =================================================================== never auto-ADD (§9 anti-echo-chamber)
def test_apply_never_auto_adds_a_proposed_handle():
    roster = _roster()
    handles_before = _handles(roster)
    rep = Y.run_yield(roster, _records(), _pulls(), cfg={}, now=NOW, apply=True)
    # the engine proposed hotfounder...
    assert any(c["handle"] == "hotfounder" for c in rep["propose_add"])
    # ...but apply=True NEVER put it in the roster (addition is human-gated only)
    assert R.find_entry(roster, "hotfounder") is None
    # apply only ever DISABLES existing rows; the handle SET is unchanged (no additions, no deletions)
    assert _handles(roster) == handles_before


# =================================================================== cold-start report-only (§9)
def test_cold_start_is_report_only_and_prunes_nothing():
    # < 7 days of real history (earliest pull 06-23, NOW 06-25) -> cold-start: honest report-only,
    # no pruning even though deadweight has zero contributions on the days we DID observe.
    cold_pulls = [
        {"run_id": "r1", "ts": "2026-06-23T08:00:00Z", "handle": "deadweight", "pulled": 3},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "deadweight", "pulled": 2},
    ]
    roster = {"schema_version": 1, "entries": [
        {"handle": "deadweight", "track": "dev-tools", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"},
    ]}
    assert Y.history_days([], cold_pulls, NOW) < 7
    rep = Y.run_yield(roster, [], cold_pulls, cfg={}, now=NOW, apply=True)
    assert rep["cold_start"] is True
    assert rep["report_only"] is True
    assert rep["prune"] == []              # cold-start gate empties the prune list...
    assert rep["applied"] is False         # ...so apply=True is a safe no-op
    assert R.find_entry(roster, "deadweight")["enabled"] is True   # roster untouched


def test_not_cold_start_with_sufficient_history():
    # The main fixture spans ~13 days (>= 7) -> NOT cold-start -> pruning is live.
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={}, now=NOW)
    assert rep["history_days"] >= 7
    assert rep["cold_start"] is False
    assert rep["report_only"] is False
    assert _prune_handles(rep) == ["deadweight"]


def test_cold_start_threshold_is_config_driven():
    # Even with the full ~13-day fixture, raising min_history_days above it forces report-only.
    rep = Y.run_yield(_roster(), _records(), _pulls(),
                      cfg={"yield": {"min_history_days": 30}}, now=NOW, apply=True)
    assert rep["cold_start"] is True
    assert rep["prune"] == []
    assert rep["applied"] is False


# =================================================================== full report shape + review render
def test_run_yield_report_shape():
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={}, now=NOW)
    for key in ("generated_at", "window_days", "prune_after_weeks", "floor", "history_days",
                "min_history_days", "cold_start", "report_only", "yields", "prune",
                "propose_add", "suggest_filters", "applied"):
        assert key in rep
    assert rep["window_days"] == 30 and rep["prune_after_weeks"] == 2 and rep["floor"] == 0


def test_render_review_md_surfaces_proposals_and_pruned():
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={}, now=NOW, apply=True)
    md = Y.render_review_md(rep)
    assert md.endswith("\n")
    assert "propose-add" in md and "hotfounder" in md          # human-gated add queue
    assert "recently pruned" in md and "deadweight" in md      # reversible / un-prune log
    # rendering the same report twice is byte-identical (deterministic)
    assert md == Y.render_review_md(rep)


# =================================================================== HARDEN: config threshold coercion (§9)
def test_yield_cfg_coerces_nonnumeric_floor_to_default_int():
    # A JSON typo like floor:"0" (a STRING) must not survive into decide_prune's `c <= floor` (int <=
    # str raises TypeError). yield_cfg coerces every threshold to a number, keeping int defaults int.
    yc = Y.yield_cfg({"yield": {"floor": "0"}})
    assert yc["floor"] == 0 and isinstance(yc["floor"], int)


def test_yield_cfg_bad_values_fall_back_to_module_defaults():
    yc = Y.yield_cfg({"yield": {"floor": "oops", "prune_after_weeks": None, "noisy_yield_max": "x"}})
    assert yc["floor"] == 0 and yc["prune_after_weeks"] == 2 and yc["noisy_yield_max"] == 0.1
    assert isinstance(yc["noisy_yield_max"], float)   # float default stays float


def test_run_yield_survives_string_floor_same_outcome():
    # The whole weekly pass must not die on a stringy floor: it runs and prunes exactly as with int 0.
    rep = Y.run_yield(_roster(), _records(), _pulls(), cfg={"yield": {"floor": "0"}}, now=NOW)
    assert rep["floor"] == 0
    assert _prune_handles(rep) == ["deadweight"]      # identical to the numeric-floor decision


# =================================================================== HARDEN: identity sweep flags (§9 rail 4)
_USER_INFO = json.loads((Path(__file__).resolve().parent / "fixtures" / "sources"
                         / "x-get_user_info.json").read_text(encoding="utf-8"))


def _sweep_roster() -> dict:
    return {"schema_version": 1, "entries": [
        {"handle": h, "track": t, "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}
        for h, t in [("karpathy", "ai-agents"), ("marc_louvion", "dev-tools"),
                     ("realGeorgeHotz", "infra-systems"), ("ghosted", "dev-tools")]]}


def test_flag_drift_and_dead_flags_rename_and_dead():
    flags = Y.flag_drift_and_dead(_sweep_roster(), _USER_INFO)
    by = {f["handle"]: f for f in flags}
    assert "karpathy" not in by                                 # healthy (userName matches, > 0) -> no flag
    assert by["marc_louvion"]["kind"] == "drift"
    assert by["marc_louvion"]["current_handle"] == "marclou"    # renamed (spec Appendix A)
    assert by["realGeorgeHotz"]["kind"] == "dead"               # statusesCount 0 (purged)
    assert by["ghosted"]["kind"] == "dead"                      # empty payload = 404 / suspended
    # deterministic ordering: dead before drift, then by handle
    assert [f["handle"] for f in flags] == ["ghosted", "realGeorgeHotz", "marc_louvion"]


def test_flag_drift_and_dead_is_pure_and_ignores_unswept_handles():
    roster = _sweep_roster()
    before = copy.deepcopy(R.entries_of(roster))
    # only the healthy handle was swept -> the others are unobserved, never fabricated into a flag
    assert Y.flag_drift_and_dead(roster, {"karpathy": _USER_INFO["karpathy"]}) == []
    assert R.entries_of(roster) == before                       # never mutates the roster (§9)


def test_flag_drift_and_dead_skips_already_disabled_handles():
    roster = _sweep_roster()
    R.set_enabled(roster, "realGeorgeHotz", False)              # already pruned/disabled
    flags = Y.flag_drift_and_dead(roster, _USER_INFO)
    assert all(f["handle"] != "realGeorgeHotz" for f in flags)  # not re-flagged once disabled


def test_run_yield_carries_flags_and_review_renders_them():
    rep = Y.run_yield(_sweep_roster(), _records(), _pulls(), cfg={}, now=NOW, user_infos=_USER_INFO)
    assert [f["handle"] for f in rep["flags"]] == ["ghosted", "realGeorgeHotz", "marc_louvion"]
    md = Y.render_review_md(rep)
    assert "flagged accounts" in md
    assert "marclou" in md and "drift" in md and "dead" in md
    assert md == Y.render_review_md(rep)                        # deterministic render


def test_run_yield_without_sweep_has_empty_flags_but_renders_section():
    rep = Y.run_yield(_sweep_roster(), _records(), _pulls(), cfg={}, now=NOW)   # no user_infos
    assert rep["flags"] == []
    assert "## flagged accounts" in Y.render_review_md(rep)     # section still present (empty)


# =================================================================== HARDEN r2: §9 clamp at the boundary
def test_yield_cfg_clamps_loosened_9_rails_at_the_boundary():
    # §9 anti-mass-prune rails hold BY CONSTRUCTION at the yield boundary, not only via load_config's
    # clamp: a hand-built cfg that tries to LOOSEN them (floor up / weeks down / history down) is
    # clamped back to the shipped defaults, so a caller that never routes through load_config (a future
    # run.py --yield, a subprocess) cannot gut the roster (audit HARDEN round 2).
    yc = Y.yield_cfg({"yield": {"floor": 1000, "prune_after_weeks": 1, "min_history_days": 0}})
    assert yc["floor"] == 0 and yc["prune_after_weeks"] == 2 and yc["min_history_days"] == 7
    # tightening the SAME rails is still honored both ways (prune slower / require more history)
    yt = Y.yield_cfg({"yield": {"floor": -1, "prune_after_weeks": 4, "min_history_days": 30}})
    assert yt["floor"] == -1 and yt["prune_after_weeks"] == 4 and yt["min_history_days"] == 30
    # non-mass-prune knobs stay freely tunable (not a §9 safety rail)
    assert Y.yield_cfg({"yield": {"window_days": 60}})["window_days"] == 60


def test_run_yield_boundary_clamp_prevents_mass_prune():
    # The finding's reproduction: run_yield handed a loosened cfg with apply=True must NOT mass-prune a
    # productive handle. The boundary clamp pins floor=0 (and weeks>=2, history>=7), so a handle with
    # real contributions stays above the floor and enabled.
    roster = {"schema_version": 1, "entries": [
        {"handle": "keepme", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    records = [
        {"opportunity_id": "k1", "first_seen": "2026-06-20T09:00:00Z",
         "last_seen": "2026-06-20T09:00:00Z", "pushed": True, "track": "ai-agents",
         "evidence": [{"origin_handle": "keepme", "url": "https://x.com/keepme/1", "faves": 700},
                      {"source": "hn", "url": "https://news.ycombinator.com/item?id=1"}]},
        {"opportunity_id": "k2", "first_seen": "2026-06-14T09:00:00Z",
         "last_seen": "2026-06-14T09:00:00Z", "pushed": True, "track": "ai-agents",
         "evidence": [{"origin_handle": "keepme", "url": "https://x.com/keepme/2", "faves": 700},
                      {"source": "hn", "url": "https://news.ycombinator.com/item?id=2"}]},
    ]
    pulls = [
        {"run_id": "r0", "ts": "2026-06-13T08:00:00Z", "handle": "keepme", "pulled": 5},
        {"run_id": "r1", "ts": "2026-06-20T08:00:00Z", "handle": "keepme", "pulled": 5},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "keepme", "pulled": 5},
    ]
    rep = Y.run_yield(roster, records, pulls,
                      cfg={"yield": {"floor": 1000, "prune_after_weeks": 1, "min_history_days": 0}},
                      now=NOW, apply=True)
    assert rep["floor"] == 0                                    # clamped, not the loosened 1000
    assert rep["prune_after_weeks"] == 2 and rep["min_history_days"] == 7
    assert _prune_handles(rep) == []                           # productive handle NOT mass-pruned
    assert R.find_entry(roster, "keepme")["enabled"] is True   # ...and left enabled


# =================================================================== HARDEN r2: pre-viral guard (§1/§9)
def test_prune_spares_handle_with_a_pre_viral_catch_in_window():
    # §1/§9: a handle that CAUGHT a pre-viral signal in the rolling window (a founder post surfaced
    # below the 500-fave keyword floor that reached a >=2-origin card) is doing the roster's raison
    # d'être. Even when the last prune-window weeks read quiet (0 recent contributions) it must NOT be
    # auto-disabled — the pre_viral metric now protects it (previously computed but ignored by prune).
    roster = {"schema_version": 1, "entries": [
        {"handle": "previral", "track": "dev-tools", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    records = [
        {"opportunity_id": "pv1", "first_seen": "2026-06-04T09:00:00Z",
         "last_seen": "2026-06-04T09:00:00Z", "pushed": True, "track": "dev-tools",
         "evidence": [{"origin_handle": "previral", "url": "https://x.com/previral/1", "faves": 90},
                      {"source": "hn", "url": "https://news.ycombinator.com/item?id=1"}]},
    ]
    # pulled in BOTH recent weeks (fully observed) but no RECENT contribution -> below-floor both weeks.
    pulls = [
        {"run_id": "r1", "ts": "2026-06-13T08:00:00Z", "handle": "previral", "pulled": 4},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "previral", "pulled": 4},
    ]
    y = Y.compute_yield(records, pulls, NOW, YCFG)
    assert y[Y.okey(Y.KIND_HANDLE, "previral")]["pre_viral"] == 1   # the metric SEES the catch
    rep = Y.run_yield(roster, records, pulls, cfg={}, now=NOW, apply=True)
    assert rep["cold_start"] is False                              # ~12d history -> pruning is live
    assert _prune_handles(rep) == []                              # ...yet the pre-viral catcher is spared
    assert R.find_entry(roster, "previral")["enabled"] is True


def test_prune_guard_is_pre_viral_specific_quiet_handle_without_a_catch_still_pruned():
    # Control for the guard above: the SAME pulled-but-quiet shape, but with NO pre-viral catch (no
    # contributing card at all), IS still pruned -> proves the pre_viral catch (not some unrelated
    # sparing) is what saved 'previral'.
    roster = {"schema_version": 1, "entries": [
        {"handle": "deadquiet", "track": "dev-tools", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    pulls = [
        {"run_id": "r0", "ts": "2026-06-04T08:00:00Z", "handle": "deadquiet", "pulled": 4},  # history anchor
        {"run_id": "r1", "ts": "2026-06-13T08:00:00Z", "handle": "deadquiet", "pulled": 4},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "deadquiet", "pulled": 4},
    ]
    rep = Y.run_yield(roster, [], pulls, cfg={}, now=NOW)
    assert rep["cold_start"] is False
    assert _prune_handles(rep) == ["deadquiet"]                   # no pre-viral catch -> pruned
