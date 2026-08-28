"""The yield pass must FAIL DEADLY on the write path, and must never report a guess as a measurement.

Every test here is a NEGATIVE CONTROL for one confirmed defect. The shape is always the same and it
is deliberate: a POSITIVE control first that proves the scenario really is prune-shaped (so a gate
that simply never prunes anything cannot pass), then the poisoned variant that must be refused.

The defect that motivated the file: auto-prune did not depend on its own numerator at all. A reviewer
copied the real pulls logs and the real roster into a scratch directory with NO opportunities.jsonl,
ran at a frozen clock, and got a byte-identical 23-handle prune list to the live run that had 112
contributions. ``_read_jsonl`` returned ``[]`` for a missing file, for an unreadable file and for a
genuinely empty one, and ``decide_prune`` gates on ``contributions <= floor`` with ``floor == 0``, so
"I could not read the numerator" was arithmetically identical to "this handle produced nothing".
"""
import importlib
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

import bandit as B
import roster as RT
from lib import now_utc

# ``yield`` is a Python keyword, so the module cannot be a bare ``import``; load it by string name.
Y = importlib.import_module("yield")

NOW = now_utc()             # frozen by conftest at 2026-06-25T12:00:00Z


# --------------------------------------------------------------------------- fixture builders

def _entry(handle, enabled=True, track="dev-tools", tier=1):
    return {"handle": handle, "track": track, "tier": tier, "enabled": enabled,
            "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}


def _roster(*entries):
    return {"schema_version": 1, "entries": list(entries)}


def _pull(handle, day_offset, kept=0):
    """One pulls-log line, ``day_offset`` whole days before NOW. ``kept=0`` keeps the section 7 kept
    guard out of the way so these tests exercise the numerator gate and the coverage gate only."""
    ts = NOW - timedelta(days=day_offset)
    return {"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "handle": handle, "kept": kept}


def _daily_pulls(handle, days, kept=0):
    return [_pull(handle, d, kept=kept) for d in days]


# Two FULLY covered weeks. The buckets are half open: week 0 is [NOW-7d, NOW) so day offsets 1..7
# land in it, week 1 is [NOW-14d, NOW-7d) so offsets 8..14 land in it. That is 7 distinct pulled days
# each, which makes coverage never the thing that blocks a prune in the numerator tests.
FULL_TWO_WEEKS = list(range(1, 15))


def _archive(tmp_path, opportunities=None, pulls=None):
    """Write a scratch archive dir. ``opportunities=None`` means the numerator file is ABSENT."""
    d = tmp_path / "archive"
    d.mkdir(parents=True, exist_ok=True)
    if opportunities is not None:
        with (d / "opportunities.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for r in opportunities:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    if pulls is not None:
        with (d / "pulls-2026-06.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for r in pulls:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return d


def _run_from(archive_dir, roster, apply=False):
    """Exactly what the CLI does: AUDITED reads, then the report."""
    recs, nst = Y.load_opportunities_audited(str(archive_dir))
    pulls, dst = Y.load_pulls_audited(str(archive_dir))
    return Y.run_yield(roster, recs, pulls, cfg={}, now=NOW, apply=apply,
                       numerator_status=nst, denominator_status=dst)


def _prune_scenario_roster():
    return _roster(_entry("deadweight"))


# ============================================================ 1. the numerator must be auditable

def test_absent_and_empty_are_different_reads(tmp_path):
    # "clean" and "did not check anything" must not be the same output. Both yield zero records; only
    # the state tells them apart, and the state is what the prune gate reads.
    absent = tmp_path / "nope.jsonl"
    recs, st = Y.read_jsonl_audited(absent)
    assert recs == [] and st["state"] == Y.READ_ABSENT

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    recs, st = Y.read_jsonl_audited(empty)
    assert recs == [] and st["state"] == Y.READ_OK
    assert st["records"] == 0 and st["decode_clean"] is True


def test_undecodable_bytes_are_reported_not_swallowed(tmp_path):
    # One partial write or one lone surrogate used to cost the whole month in silence: the read
    # raised, the exception was swallowed, and the caller got []. Recovery is still tolerant, but the
    # loss is now ON THE RECORD.
    p = tmp_path / "opportunities.jsonl"
    good = json.dumps({"opportunity_id": "op-1", "last_seen": "2026-06-20T09:00:00Z",
                       "evidence": [{"origin_handle": "keeper", "url": "u"}]})
    p.write_bytes(good.encode("utf-8") + b"\n" + b'{"opportunity_id": "op-\xff\xfe2"}\n')
    recs, st = Y.read_jsonl_audited(p)
    assert st["state"] == Y.READ_CORRUPT
    assert st["decode_clean"] is False and "UnicodeDecodeError" in (st["error"] or "")
    assert len(recs) >= 1                       # the intact record around the bad byte survives


def test_unparseable_line_is_counted_not_dropped_in_silence(tmp_path):
    p = tmp_path / "opportunities.jsonl"
    p.write_text('{"opportunity_id": "op-1"}\nnot json at all\n\n', encoding="utf-8")
    recs, st = Y.read_jsonl_audited(p)
    assert len(recs) == 1
    assert st["bad_lines"] == 1 and st["blank_lines"] == 1
    assert st["state"] == Y.READ_CORRUPT


def test_unreadable_file_is_its_own_state(tmp_path, monkeypatch):
    p = tmp_path / "opportunities.jsonl"
    p.write_text('{"opportunity_id": "op-1"}\n', encoding="utf-8")

    def boom(self, *a, **k):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(Path, "read_bytes", boom)
    recs, st = Y.read_jsonl_audited(p)
    assert recs == [] and st["state"] == Y.READ_UNREADABLE
    assert "PermissionError" in (st["error"] or "")


# ============================================================ 1b. THE negative control: fail closed

def test_positive_control_this_scenario_really_does_prune(tmp_path):
    # Without this, every "prune is empty" assertion below would be vacuous: a gate that refuses
    # everything passes them all. Same pulls, same roster, numerator PRESENT and readable (empty but
    # really read) means the handle IS pruned.
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, _prune_scenario_roster())
    assert rep["numerator_source"]["state"] == Y.READ_OK
    assert [p["handle"] for p in rep["prune"]] == ["deadweight"]


def test_prune_is_empty_when_pulls_exist_and_the_numerator_file_is_missing(tmp_path):
    # THE reproduced defect. Byte for byte the same pulls and roster as the positive control above,
    # with opportunities.jsonl simply not there. "I could not read the numerator" is not "zero".
    d = _archive(tmp_path, opportunities=None, pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    assert not (d / "opportunities.jsonl").exists()
    rep = _run_from(d, _prune_scenario_roster())

    assert rep["prune"] == []
    assert rep["numerator_source"]["state"] == Y.READ_ABSENT
    assert rep["numerator_source"]["trusted"] is False
    assert rep["prune_blocked_reason"] and "UNKNOWN" in rep["prune_blocked_reason"]
    assert rep["report_only_reason"] == "numerator_untrusted"
    assert any("numerator" in w for w in rep["warnings"])


def test_prune_is_empty_when_the_numerator_does_not_decode(tmp_path):
    # One UnicodeDecodeError disabled 23 of 139 handles in the live run. Records still recover, and
    # the prune still refuses, because a PARTIAL numerator cannot prove a handle produced nothing.
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    (d / "opportunities.jsonl").write_bytes(b'{"opportunity_id": "op-\xff\xfe1"}\n')
    rep = _run_from(d, _prune_scenario_roster())
    assert rep["prune"] == []
    assert rep["numerator_source"]["state"] == Y.READ_CORRUPT
    assert rep["numerator_source"]["decode_clean"] is False
    assert rep["report_only_reason"] == "numerator_untrusted"


def test_apply_writes_nothing_when_the_numerator_is_untrusted(tmp_path, monkeypatch):
    # The WRITE path, end to end through main(): --apply with a missing numerator must leave
    # roster.json byte identical. A writer that shrugs here disables live handles on no evidence.
    d = _archive(tmp_path, opportunities=None, pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(_prune_scenario_roster(), ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    before = rpath.read_bytes()

    monkeypatch.setattr(Y, "load_config", lambda *a, **k: {})
    rc = Y.main(["--archive-dir", str(d), "--roster", str(rpath), "--apply", "--write-review"])
    assert rc == 0
    assert rpath.read_bytes() == before
    entry = RT.find_entry(json.loads(rpath.read_text(encoding="utf-8")), "deadweight")
    assert entry["enabled"] is True

    md = (d / "roster-review.md").read_text(encoding="utf-8")
    assert "roster_written: false" in md
    assert "numerator: absent" in md


def test_records_handed_in_directly_are_still_trusted():
    # The gate must bind the FILE READING path only. A library caller that passed records in never
    # had a file to be lied to by, and blocking it would be an un-auditable "never prunes" no-op.
    rep = Y.run_yield(_prune_scenario_roster(), [], _daily_pulls("deadweight", FULL_TWO_WEEKS),
                      cfg={}, now=NOW)
    assert rep["numerator_source"]["state"] == Y.READ_PROVIDED
    assert rep["numerator_source"]["trusted"] is True
    assert [p["handle"] for p in rep["prune"]] == ["deadweight"]


def test_numerator_source_is_on_every_report(tmp_path):
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, _prune_scenario_roster())
    src = rep["numerator_source"]
    assert {"state", "trusted", "records_in", "path", "bad_lines", "decode_clean",
            "error"} <= set(src)
    assert src["path"].endswith("opportunities.jsonl")
    assert rep["denominator_source"]["lines_in"] == len(FULL_TWO_WEEKS)


# ============================================================ 2. the pre-viral guard is not protection

# Exactly the keys the live archive writes onto an origin tagged evidence item, and nothing else.
_LIVE_EVIDENCE_KEYS = {"source": "x", "origin": "roster", "url": "https://example.com/1",
                       "signal": "s", "ts": "2026-06-20T09:00:00Z", "origin_handle": "deadweight"}


def _card(evidence, ts="2026-06-20T09:00:00Z", oid="op-1"):
    return {"opportunity_id": oid, "first_seen": ts, "last_seen": ts, "track": "dev-tools",
            "evidence": evidence}


def test_guard_reports_inert_on_archive_shaped_evidence():
    # The archived evidence carries source/origin/url/signal/ts/origin_handle and NOTHING the guard
    # can read, which is why 152 of 152 live origins evaluated pre_viral=0 and the guard had never
    # spared anything. That is UNKNOWN, and it must not read as "no pre-viral catch".
    pv = Y.pre_viral_observability([_card([dict(_LIVE_EVIDENCE_KEYS)])], NOW, Y.yield_cfg({}))
    assert pv["state"] == "inert"
    assert pv["evidence_items"] == 1 and pv["with_engagement"] == 0


def test_guard_reports_live_when_an_engagement_key_is_actually_written():
    ev = dict(_LIVE_EVIDENCE_KEYS)
    ev["faves"] = 12
    pv = Y.pre_viral_observability([_card([ev])], NOW, Y.yield_cfg({}))
    assert pv["state"] == "live" and pv["with_engagement"] == 1


def test_no_evidence_is_empty_not_inert():
    # "nothing to judge" and "judged and found nothing readable" are different answers.
    pv = Y.pre_viral_observability([], NOW, Y.yield_cfg({}))
    assert pv["state"] == "empty" and pv["evidence_items"] == 0


def test_a_prune_taken_while_the_guard_is_inert_is_warned_in_report_and_artifact(tmp_path):
    # A handle pruned while the guard that was supposed to spare it cannot fire must say so on the
    # artifact, next to the decision.
    d = _archive(tmp_path,
                 opportunities=[_card([dict(_LIVE_EVIDENCE_KEYS, origin_handle="other")],
                                      oid="op-other")],
                 pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, _prune_scenario_roster())
    assert [p["handle"] for p in rep["prune"]] == ["deadweight"]
    assert rep["pre_viral_guard"]["state"] == "inert"
    assert rep["pre_viral_guard"]["note"]
    assert any("INERT" in w for w in rep["warnings"])
    assert "INERT" in Y.render_review_md(rep)


def test_every_prune_decision_carries_the_guard_state_that_let_it_through(tmp_path):
    # The row a human reads before un-pruning must say whether the guard could fire at all. A header
    # level warning is not enough: the reader is looking at one handle.
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, _prune_scenario_roster())
    assert [p["pre_viral_guard_state"] for p in rep["prune"]] == ["empty"]

    d2 = _archive(tmp_path / "b",
                  opportunities=[_card([dict(_LIVE_EVIDENCE_KEYS, origin_handle="other")],
                                       oid="op-other")],
                  pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep2 = _run_from(d2, _prune_scenario_roster())
    assert [p["pre_viral_guard_state"] for p in rep2["prune"]] == ["inert"]


def test_the_guard_spares_and_says_so_when_it_can_actually_fire(tmp_path):
    # NEGATIVE CONTROL for the liveness report itself: with a readable engagement key below the
    # threshold the guard fires, the handle is spared, and "spared" names it. A liveness field that
    # said "inert" no matter what would be as useless as the guard it describes.
    ev = dict(_LIVE_EVIDENCE_KEYS)
    ev["faves"] = 3
    d = _archive(tmp_path, opportunities=[_card([ev])],
                 pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, _prune_scenario_roster())
    assert rep["pre_viral_guard"]["state"] == "live"
    assert rep["pre_viral_guard"]["spared"] == ["deadweight"]
    assert rep["prune"] == []


# ============================================================ 3. proposed is not applied

def test_a_proposed_prune_is_never_printed_as_a_disable(tmp_path):
    # 23 handles were documented as "recently pruned ... enabled=false" while still enabled in
    # roster.json, because the section listed DECISIONS, not the roster.
    roster = _prune_scenario_roster()
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, roster, apply=False)

    assert [p["handle"] for p in rep["prune"]] == ["deadweight"]
    assert rep["roster_written"] is False
    assert rep["prune_proposed"] == 1 and rep["prune_applied"] == 0
    assert rep["report_only_reason"] == "apply_not_requested"
    assert RT.find_entry(roster, "deadweight")["enabled"] is True   # untouched, as the artifact says

    md = Y.render_review_md(rep)
    proposed_sect = md.split("### proposed prunes")[1].split("\n## ")[0]
    applied_sect = md.split("## recently pruned")[1].split("\n## ")[0]
    assert "deadweight" in proposed_sect
    assert "deadweight" not in applied_sect
    assert "_none_" in applied_sect
    assert "roster_written: false" in md
    assert "NOTHING WAS APPLIED" in md


def test_after_apply_the_same_handle_moves_to_the_applied_section(tmp_path):
    # NEGATIVE CONTROL for the split above: a renderer that always calls everything "proposed" would
    # be just as much a liar, in the other direction.
    roster = _prune_scenario_roster()
    d = _archive(tmp_path, opportunities=[], pulls=_daily_pulls("deadweight", FULL_TWO_WEEKS))
    rep = _run_from(d, roster, apply=True)

    assert rep["roster_written"] is True and rep["prune_applied"] == 1
    assert RT.find_entry(roster, "deadweight")["enabled"] is False

    md = Y.render_review_md(rep)
    proposed_sect = md.split("### proposed prunes")[1].split("\n## ")[0]
    applied_sect = md.split("## recently pruned")[1].split("\n## ")[0]
    assert "deadweight" in applied_sect
    assert "deadweight" not in proposed_sect
    assert "roster_written: true" in md
    assert "NOTHING WAS APPLIED" not in md


def test_report_only_flag_no_longer_stands_in_for_was_it_written():
    # ``report_only`` is the COLD START gate and nothing else; it read as "nothing was applied" and
    # that is how a run which merely proposed 23 prunes got reported as if it had made them.
    roster = _prune_scenario_roster()
    rep = Y.run_yield(roster, [], _daily_pulls("deadweight", FULL_TWO_WEEKS), cfg={}, now=NOW,
                      apply=True)
    assert rep["cold_start"] is False and rep["report_only"] is False
    assert rep["roster_written"] is True
    assert rep["report_only_reason"] is None


# ============================================================ 4. a week must be really observed

def test_a_thinly_covered_week_cannot_carry_a_prune():
    # The two weeks behind the 23 live prune decisions had 4/7 and 5/7 days of coverage, and a single
    # pull event made a bucket read "fully observed". The relative bar catches exactly that: this
    # origin demonstrated 5 day coverage, so a 2 day week is a gap in OUR observation.
    pulls = _daily_pulls("deadweight", [1, 2, 3, 4, 5]) + _daily_pulls("deadweight", [8, 9])
    rep = Y.run_yield(_prune_scenario_roster(), [], pulls, cfg={}, now=NOW)
    assert rep["prune"] == []


def test_positive_control_evenly_covered_weeks_still_prune():
    pulls = (_daily_pulls("deadweight", [1, 2, 3, 4, 5])
             + _daily_pulls("deadweight", [8, 9, 10, 11, 12]))
    rep = Y.run_yield(_prune_scenario_roster(), [], pulls, cfg={}, now=NOW)
    assert [p["handle"] for p in rep["prune"]] == ["deadweight"]


def test_every_prune_decision_carries_its_observed_day_count():
    pulls = (_daily_pulls("deadweight", [1, 2, 3, 4, 5])
             + _daily_pulls("deadweight", [8, 9, 10, 11, 12]))
    rep = Y.run_yield(_prune_scenario_roster(), [], pulls, cfg={}, now=NOW)
    d = rep["prune"][0]
    assert d["weekly_observed_days"] == [5, 5]
    assert d["required_observed_days"] == 5
    assert d["full_week_days"] == Y.FULL_WEEK_DAYS
    assert d["full_coverage"] is False                 # 5 of 7 is not a fully observed week
    assert "weekly observed days [5, 5]" in d["reason"]
    assert any("NOT fully observed" in w for w in rep["warnings"])
    assert "NOT fully observed" in Y.render_review_md(rep)


def test_full_coverage_is_true_only_at_a_real_calendar_week():
    rep = Y.run_yield(_prune_scenario_roster(), [], _daily_pulls("deadweight", FULL_TWO_WEEKS),
                      cfg={}, now=NOW)
    d = rep["prune"][0]
    assert d["weekly_observed_days"] == [7, 7] and d["full_coverage"] is True
    assert not any("NOT fully observed" in w for w in rep["warnings"])


def test_absolute_coverage_bar_is_config_driven_and_blocks():
    pulls = (_daily_pulls("deadweight", [1, 2, 3, 4, 5])
             + _daily_pulls("deadweight", [8, 9, 10, 11, 12]))
    cfg = {"yield": {"min_observed_days_per_week": 7}}
    assert Y.run_yield(_prune_scenario_roster(), [], pulls, cfg=cfg, now=NOW)["prune"] == []
    # and the same bar does NOT block a genuinely full week
    full = Y.run_yield(_prune_scenario_roster(), [], _daily_pulls("deadweight", FULL_TWO_WEEKS),
                       cfg=cfg, now=NOW)
    assert [p["handle"] for p in full["prune"]] == ["deadweight"]


def test_absolute_bar_cannot_be_set_to_an_unsatisfiable_value():
    # Above 7 distinct days inside a 7 day bucket is unsatisfiable: it would silently disable pruning
    # forever, which is a no-op wearing a guard's clothes. Clamped to a real week.
    assert Y.yield_cfg({"yield": {"min_observed_days_per_week": 99}})["min_observed_days_per_week"] == 7


def test_weekly_observations_reports_days_not_just_events():
    pulls = _daily_pulls("hh", [1, 1, 1])          # three pulls, ONE distinct day
    obs = Y.weekly_observations((Y.KIND_HANDLE, "hh"), [], pulls, NOW, 1)
    assert obs[0] == (0, 3, 1)


# ============================================================ 5. the pull cap must not be silent

_CAP_CFG = {"sources": {"twitterapi": {"max_handles_per_run": 2}}}


def _five():
    return _roster(*[_entry("h%d" % i) for i in range(1, 6)])


def test_truncation_names_every_dropped_handle(capsys):
    plan = RT.plan_pulls(_five(), cfg=_CAP_CFG)
    err = capsys.readouterr().err
    assert [t["handle"] for t in plan] == ["h1", "h2"]
    assert "max_handles_per_run=2" in err
    for h in ("h3", "h4", "h5"):
        assert h in err
    assert "NOT pulled this run (3)" in err


def test_an_uncapped_plan_says_nothing_and_drops_nothing(capsys):
    # NEGATIVE CONTROL for the log: a notice that fires unconditionally teaches the operator to
    # ignore it, and an uncapped roster must plan byte identically to before the cap existed.
    plan = RT.plan_pulls(_five(), cfg={})
    assert capsys.readouterr().err == ""
    assert [t["handle"] for t in plan] == ["h1", "h2", "h3", "h4", "h5"]
    rep = RT.plan_pulls_report(_five(), cfg={})
    assert rep["truncated"] is False and rep["dropped"] == [] and rep["cap"] is None


def test_plan_and_dropped_partition_the_eligible_roster_exactly():
    rep = RT.plan_pulls_report(_five(), cfg=_CAP_CFG)
    assert sorted([t["handle"] for t in rep["plan"]] + rep["dropped"]) == \
        ["h1", "h2", "h3", "h4", "h5"]
    assert rep["eligible"] == 5 and rep["cap"] == 2 and rep["truncated"] is True


def test_rotation_reaches_the_tail_instead_of_blinding_it():
    # Without rotation the same first N were pulled forever, the tail accrued no denominator, and the
    # pruner then read a never observed handle as deadweight. Three capped runs must see all five.
    roster = _five()
    seen = []
    for _ in range(3):
        plan = RT.plan_pulls(roster, cfg=_CAP_CFG, warn=False)
        seen.extend(t["handle"] for t in plan)
        RT.advance_rotation(roster, len(plan))
    assert set(seen) == {"h1", "h2", "h3", "h4", "h5"}


def test_rotation_is_durable_and_pure_until_advanced():
    # The cursor lives on the roster, not on a clock: the same roster plans the same pulls until
    # someone advances it, so a plan stays byte reproducible.
    roster = _five()
    assert RT.plan_pulls(roster, cfg=_CAP_CFG, warn=False) == \
        RT.plan_pulls(roster, cfg=_CAP_CFG, warn=False)
    RT.advance_rotation(roster, 2)
    assert roster[RT.ROTATION_CURSOR_KEY] == 2
    assert [t["handle"] for t in RT.plan_pulls(roster, cfg=_CAP_CFG, warn=False)] == ["h3", "h4"]


def test_rotation_wraps_and_reports_that_it_did():
    roster = _five()
    RT.advance_rotation(roster, 4)
    rep = RT.plan_pulls_report(roster, cfg=_CAP_CFG)
    assert [t["handle"] for t in rep["plan"]] == ["h5", "h1"]
    assert rep["wrapped"] is True and rep["rotation_offset"] == 4


# ============================================================ 6. the bandit switch

def test_bandit_is_off_by_default():
    assert B.bandit_enabled({}) is False
    assert B.bandit_enabled({"scoring": {}}) is False
    assert B.bandit_enabled({"scoring": {"bandit": {}}}) is False


def test_only_a_literal_true_turns_the_learning_loop_on():
    # A truthy string must never silently enable a loop that rewrites ranking. NEGATIVE CONTROL for
    # the switch: if it coerced, "false" would turn it ON.
    for raw in ("true", "false", 1, 0, "yes", None, [], {}):
        assert B.bandit_enabled({"scoring": {"bandit": {"enabled": raw}}}) is False
    assert B.bandit_enabled({"scoring": {"bandit": {"enabled": True}}}) is True


def test_bandit_switch_survives_a_garbled_config():
    assert B.bandit_enabled({"scoring": "not a dict"}) is False


@pytest.mark.parametrize("track", ["ai-agents", "dev-tools"])
def test_bandit_stays_deterministic_so_enabling_it_is_replayable(track):
    arms = {track: {"alpha": 3.0, "beta": 2.0, "n": 5}}
    a = B.thompson_sample(arms, [track], seed=7)
    b = B.thompson_sample(arms, [track], seed=7)
    assert a == b


def test_the_bandit_cli_reports_whether_it_is_switched_on(monkeypatch, capsys):
    # The draws are always computable, which is precisely why they could never tell an operator
    # whether the loop was turning. The CLI now says so first, and says it even when the answer is no.
    payload = json.dumps({"arms": {"ai-agents": {"alpha": 2.0, "beta": 1.0, "n": 1}},
                          "tracks": ["ai-agents"], "seed": 3, "config": {}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert B.main() == 0
    off = json.loads(capsys.readouterr().out)
    assert off["enabled"] is False and "thompson" in off

    payload_on = json.dumps({"arms": {"ai-agents": {"alpha": 2.0, "beta": 1.0, "n": 1}},
                             "tracks": ["ai-agents"], "seed": 3,
                             "config": {"scoring": {"bandit": {"enabled": True}}}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload_on))
    assert B.main() == 0
    on = json.loads(capsys.readouterr().out)
    assert on["enabled"] is True
    assert on["thompson"] == off["thompson"]      # the switch reports, it does not perturb the draw
