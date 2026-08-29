#!/usr/bin/env python3
"""Coverage for the run.py gap-accounting rewrite (2026-08-27), which landed with no tests.

The whole change exists to make one class of lie impossible: a run that lost 95% of its collected
signal, or failed to write its digest, must not print the same thing as a clean run. Every test here
is written so that it goes RED against the pre-fix behaviour it pins, never merely green against
whatever the code happens to do.

Contents, in the order the pipeline meets them:
  1. main() exit code, a side-effect error must reach the shell as a nonzero rc.
  2. the coverage contract, measured vs unmeasured, and that the two never render alike.
  3. the 625-to-12 shape, signals_unaccounted must be nonzero and reported.
  4. classify, an unmatched item lands in unclassified at weight 1.0, and keywords match on tokens.
  5. _topic_filter_match, a hyphenated filter term is not shredded into generic halves.
  6. the community lane keep/drop keyword filter, it filters AND reports what each list dropped.
  7. append_pulls, idempotent per (run_id, unit), so a re-run cannot double the yield denominator.
  8. a FAILED pull is an error record, never an observation of zero yield.
"""
import json
import os
import sys

import pytest

import collect as CO

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import classify as cl  # noqa: E402
import digest as dg  # noqa: E402
import lib  # noqa: E402
import run  # noqa: E402


# ============================================================================ 1. main() exit code
# The reported defect: process() accumulates side-effect errors (a failed digest write, a failed
# ledger upsert, a held watermark) into result["errors"], and main() printed the JSON and returned 0
# anyway. A cron wrapper reads the rc, so a day whose digest never got written looked byte-for-byte
# as successful as a day that shipped. Every stage name in this list is a WRITE that the artifact
# depends on.
SIDE_EFFECT_STAGES = ("digest_file", "upsert", "watermark", "bandit_persist",
                      "digest_item", "pulse_seen")


def _main_rc(monkeypatch, capsys, result: dict, argv=None):
    """Run run.main() over an empty candidate list with process() stubbed to return ``result``."""
    monkeypatch.setattr(sys, "argv", argv or ["run.py", "--no-ledger", "--in", os.devnull])
    monkeypatch.setattr(run, "process", lambda *a, **k: dict(result))
    rc = run.main()
    return rc, capsys.readouterr().out


@pytest.mark.parametrize("stage", SIDE_EFFECT_STAGES)
def test_main_returns_nonzero_when_a_side_effect_write_failed(monkeypatch, capsys, stage):
    res = {"run_id": "daily-2026-06-25", "errors": [{"stage": stage, "err": "boom"}],
           "watermark_advanced": False, "digest_path": None}
    rc, out = _main_rc(monkeypatch, capsys, res)
    assert rc != 0, f"stage {stage} failed but main() reported success (rc={rc})"
    # the failure must also be VISIBLE, not just encoded in the rc
    assert stage in out, out


def test_main_returns_zero_on_a_clean_run(monkeypatch, capsys):
    """NEGATIVE CONTROL for the parametrized test above. If main() simply returned 1 always, or keyed
    its rc off some field that is always truthy, every case above would pass for the wrong reason. A
    result with an EMPTY errors list is a successful day and must still exit 0."""
    res = {"run_id": "daily-2026-06-25", "errors": [], "watermark_advanced": True,
           "digest_path": "/tmp/x.md", "empty_day": True, "below_sources": [{"title": "t"}],
           "blocked": [{"title": "b"}]}
    rc, out = _main_rc(monkeypatch, capsys, res)
    assert rc == 0, out
    # and the rc is keyed off errors specifically, not off "did anything at all get dropped":
    # an empty day with blocked/below-source items is NOT a failure of the run.
    assert json.loads(out)["empty_day"] is True


def test_main_rc_is_driven_by_errors_not_by_the_watermark_flag(monkeypatch, capsys):
    """A held watermark with no recorded error (dry-run, --no-ledger) is not a failure; a recorded
    error with an ADVANCED watermark still is. Pins the rc to the errors list itself so a later
    refactor cannot quietly re-key it to watermark_advanced, which is False on every dry run."""
    rc_dry, _ = _main_rc(monkeypatch, capsys,
                         {"errors": [], "watermark_advanced": False})
    rc_err, _ = _main_rc(monkeypatch, capsys,
                         {"errors": [{"stage": "upsert", "err": "x"}], "watermark_advanced": True})
    assert (rc_dry, rc_err) == (0, 1)


# ======================================================== 2. the coverage contract, measured or not
# Every field on the coverage line is either a real observation or named in ``unmeasured``. There is
# no third state. The defect this replaced printed "(see SKILL run)" and a bare 0, both of which read
# as values and assert nothing.
CONTRACT_FIELDS = ("signals_collected", "signals_unaccounted", "sources_invoked",
                   "sources_available", "sources_failed", "candidates", "below_sources",
                   "community_pulse", "suppressed", "below_floor", "pushed", "deepdived")


def _coverage(collection=None, candidates=(), gate=None):
    return run.build_coverage(list(candidates), [], [], [], [], gate, [], collection)


def test_coverage_reports_every_contract_field():
    cov = _coverage()
    missing = [f for f in CONTRACT_FIELDS if f not in cov]
    assert not missing, missing


def test_unmeasured_names_are_all_declared_in_COVERAGE_FIELDS():
    """COVERAGE_FIELDS is the declared vocabulary of "may be missing". A name the renderer has never
    heard of would silently render as a number again, so the producer may not invent one."""
    for coll in (None, {"signals_collected": 4, "signal_keys": [], "sources_invoked": 1},
                 {"signals_collected": 0, "signal_keys": [], "sources_invoked": 2,
                  "sources_available": 0}):
        cov = _coverage(coll)
        stray = set(cov["unmeasured"]) - set(run.COVERAGE_FIELDS)
        assert not stray, (coll, stray)


def test_no_collection_leg_is_unmeasured_not_zero():
    """THE point of the mechanism: "we collected 0 signals" and "we do not know how many signals were
    collected" must be different outputs. With no collection record the counts are placeholders and
    every one of them is named."""
    cov = _coverage(None)
    for f in ("signals_collected", "signals_unaccounted", "sources_invoked",
              "sources_available", "sources_failed"):
        assert f in cov["unmeasured"], (f, cov["unmeasured"])
    line = dg.coverage_line(cov, qualified=0)
    assert "信号 未统计" in line, line
    assert "信号 0" not in line, line


def test_a_genuinely_empty_collection_leg_reports_zero_not_unmeasured():
    """NEGATIVE CONTROL for the test above, and the half that actually goes red if ``unmeasured``
    collapses back into 0 (or if everything is blanketed as unmeasured to make the first test pass).
    A --sources leg that ran and honestly collected nothing is a MEASURED zero: it must print 信号 0
    and must NOT be named unmeasured. If the two ever render the same, one of these two tests fails."""
    coll = {"schema_version": 1, "run_id": "daily-2026-06-25", "signals_collected": 0,
            "signal_keys": [], "sources_invoked": 3, "sources_available": 3, "sources_failed": []}
    cov = _coverage(coll)
    assert cov["signals_collected"] == 0
    assert "signals_collected" not in cov["unmeasured"], cov["unmeasured"]
    assert "signals_unaccounted" not in cov["unmeasured"], cov["unmeasured"]
    line = dg.coverage_line(cov, qualified=0)
    assert "信号 0" in line, line
    # the two outputs are literally different strings, which is the whole claim
    assert line != dg.coverage_line(_coverage(None), qualified=0)


def test_a_count_without_keys_cannot_claim_a_zero_gap():
    """A collection record that states a count but carries no signal_keys can support the count and
    NOTHING more. Subtracting an unknowable ``accounted`` from 625 would print 未归因信号 625, and
    reporting 0 would claim every signal was accounted for. Both are lies, so the field is unmeasured
    while signals_collected still stands."""
    coll = {"signals_collected": 625, "signal_keys": [], "sources_invoked": 4,
            "sources_available": 4, "sources_failed": []}
    cov = _coverage(coll)
    assert cov["signals_collected"] == 625
    assert "signals_unaccounted" in cov["unmeasured"]
    assert "signals_collected" not in cov["unmeasured"]
    line = dg.coverage_line(cov, qualified=0)
    assert "信号 625" in line and "未归因信号 未统计" in line, line


def test_below_floor_is_unmeasured_when_the_gate_did_not_report_it():
    """An older verify_gate that returns no ``below_floor`` key means nobody counted the cards the
    score floor dropped. Reporting [] there would print 未达门槛 0, i.e. "the floor dropped nothing"."""
    assert "below_floor" in _coverage(None, gate={"pushable": [], "archivable": []})["unmeasured"]
    # NEGATIVE CONTROL: a gate that DOES report an empty list is a measured zero, not unmeasured.
    cov = _coverage(None, gate={"pushable": [], "archivable": [], "below_floor": []})
    assert "below_floor" not in cov["unmeasured"]
    assert "未达门槛 0" in dg.coverage_line(cov, qualified=0)


# ============================================================== 3. the 625-to-12 hole, made visible
# The shape that motivated the whole change: the --sources leg reported 625 collected signals, the
# --in leg saw 12 candidate clusters, and the run reported below_sources [] and community_pulse [],
# i.e. NOTHING dropped. 613 signals evaporated between two processes and every number the run printed
# was individually true. The fixture below is SYNTHETIC and reproduces only the shape; the operator's
# real records stay in the private data repo and are never copied here.
_LANES = ("v2ex", "linux.do", "x.com/synthetic_a", "x.com/synthetic_b")


def _synthetic_signals(n: int, first: int = 0) -> list:
    """n origin-tagged collected signals, each with a distinct URL (the reconciliation join key)."""
    return [{"source": _LANES[i % len(_LANES)], "origin": _LANES[i % len(_LANES)],
             "url": f"https://example.com/thread/{i}", "ts": "2026-06-25T09:00:00Z",
             "title": f"synthetic signal {i}", "text": "synthetic body"}
            for i in range(first, first + n)]


def _collection_of(signals: list, sources_available: int = 4) -> dict:
    """Run the synthetic signals through the REAL producer, so the test pins the shipped record
    shape rather than a hand-written dict that could drift away from it."""
    out = {"run_id": "daily-2026-08-27", "signals": signals,
           "pulls": [{"run_id": "daily-2026-08-27", "source": s, "pulled": 1, "kept": 1}
                     for s in _LANES[:sources_available]],
           "filtered": {}}
    cfg = lib.load_config()
    cfg["sources"] = {s: {"enabled": True} for s in _LANES[:sources_available]}
    return run.build_collection_record(out, cfg=cfg, run_id="daily-2026-08-27")


def _candidate_over(signals: list, idx: int) -> dict:
    """One candidate cluster whose evidence points back at two of the collected signals."""
    ev = [dict(s) for s in signals]
    return {"title": f"synthetic opportunity {idx}", "summary": "an agent tooling gap",
            "entities": [f"SyntheticCo{idx}"],
            "evidence": ev,
            "score_breakdown": {"track_fit": 70, "timing": 70, "feasibility": 70,
                                "competition": 70, "executability": 70},
            "age_hours": 5.0, "velocity": 0.2, "lifecycle_stage": "emerging"}


def test_625_to_12_collapse_is_reported_as_unaccounted_signal():
    sig = _synthetic_signals(625)
    coll = _collection_of(sig)
    assert coll["signals_collected"] == 625 and len(coll["signal_keys"]) == 625
    # 12 candidate clusters, each tracing back to 2 of the collected signals -> 24 accounted.
    cands = [_candidate_over(sig[2 * i:2 * i + 2], i) for i in range(12)]
    res = run.process(cands, lib.load_config(), None, dry_run=True,
                      run_id="daily-2026-08-27", collection=coll)
    cov = res["coverage"]
    assert cov["signals_collected"] == 625
    assert cov["candidates"] == 12
    assert cov["signals_unaccounted"] == 601, cov
    assert "signals_unaccounted" not in cov["unmeasured"]
    # and it is REPORTED, not merely computed: the number reaches the digest's public claim line.
    assert "未归因信号 601" in dg.coverage_line(cov, qualified=len(res["archived"])), cov
    # the exact shape of the original miss: nothing was reported dropped on either lane, and before
    # the gap field existed that combination read as a complete, healthy run.
    assert res["below_sources"] == [] and res["community_pulse"] == []


def test_a_fully_accounted_run_reports_a_zero_gap():
    """NEGATIVE CONTROL for the test above. If signals_unaccounted were simply signals_collected, or
    any constant, this would go red: when every collected signal is reachable from some candidate's
    evidence, the gap is a genuine, measured 0."""
    sig = _synthetic_signals(24)
    coll = _collection_of(sig)
    cands = [_candidate_over(sig[2 * i:2 * i + 2], i) for i in range(12)]
    res = run.process(cands, lib.load_config(), None, dry_run=True,
                      run_id="daily-2026-08-27", collection=coll)
    cov = res["coverage"]
    assert cov["signals_collected"] == 24 and cov["signals_unaccounted"] == 0, cov
    assert "signals_unaccounted" not in cov["unmeasured"]
    assert "未归因信号 0" in dg.coverage_line(cov, qualified=0)


def test_signal_key_join_survives_url_normalization():
    """The gap number is only honest if the join key does not manufacture a mismatch. A collected
    signal and the candidate evidence built from it may differ in scheme, www., or a trailing slash;
    those must reconcile, or every clean run would report a fake 100% gap."""
    coll = _collection_of([{"source": "v2ex", "origin": "v2ex", "ts": "2026-06-25T09:00:00Z",
                            "url": "https://www.example.com/thread/7/", "title": "t"}])
    cand = _candidate_over([{"source": "v2ex", "origin": "v2ex",
                             "url": "http://example.com/thread/7"},
                            {"source": "hn", "origin": "hn", "url": "http://example.com/other"}], 0)
    cov = run.build_coverage([cand], [], [], [], [], {"below_floor": []}, [], coll)
    assert cov["signals_unaccounted"] == 0, cov
    # NEGATIVE CONTROL: a genuinely different URL does NOT reconcile away.
    cand2 = _candidate_over([{"source": "v2ex", "origin": "v2ex",
                              "url": "http://example.com/thread/8"}], 1)
    cov2 = run.build_coverage([cand2], [], [], [], [], {"below_floor": []}, [], coll)
    assert cov2["signals_unaccounted"] == 1, cov2


def test_sources_failed_is_carried_from_collection_into_coverage():
    """A half-failed roster must not round down to "twitterapi worked". The failed unit travels from
    the pull record into the coverage contract by name."""
    out = {"run_id": "daily-2026-08-27", "signals": _synthetic_signals(3),
           "pulls": [{"run_id": "daily-2026-08-27", "source": "v2ex", "pulled": 3, "kept": 3},
                     run.failed_pull({"handle": "synthetic_a"}, "429 rate limited",
                                     "daily-2026-08-27", lib.now_utc())],
           "filtered": {}}
    cfg = lib.load_config()
    cfg["sources"] = {"v2ex": {"enabled": True}, "linux.do": {"enabled": True}}
    coll = run.build_collection_record(out, cfg=cfg, run_id="daily-2026-08-27")
    cov = run.build_coverage([], [], [], [], [], {"below_floor": []}, [], coll)
    assert [f["source"] for f in cov["sources_failed"]] == ["x.com/synthetic_a"], cov
    assert "sources_failed" not in cov["unmeasured"]
    assert "失败源 1" in dg.coverage_line(cov, qualified=0)


# ================================================ 4. classify, the unclassified track and boundaries
# Two bugs in one place. (a) A candidate that matched NO track keyword fell back to ``tracks[0]``,
# which is ai-agents, the track carrying the LARGEST weight (1.3): the least classifiable item in the
# system was handed the biggest scoring bonus and keyed under ``::ai-agents`` for cross-day dedup.
# (b) Keywords were bare substrings, so ``ci`` fired inside *decision* and ``api`` inside *capital*,
# manufacturing the very hits that decided which track a card was filed under.
#
# "a rapid decision about capital" is the joint witness: under substring matching it scores TWO
# dev-tools hits (ci, api) and would be filed under dev-tools; under token matching it hits nothing
# at all and is honestly unclassified.
_NO_KEYWORD_TEXT = "a rapid decision about capital"


def test_an_unmatched_item_lands_in_unclassified_not_in_tracks_zero():
    cfg = lib.load_config()
    res = cl.classify("a rapid decision", _NO_KEYWORD_TEXT, cfg)
    assert res["track"] == cl.UNCLASSIFIED_TRACK == "unclassified", res
    assert res["track_matched"] is False, res
    assert res["track"] != cfg["tracks"][0]["id"], "fell back to tracks[0] again"


def test_the_unclassified_weight_is_neutral_and_is_not_the_ai_agents_bonus():
    cfg = lib.load_config()
    ai = run.effective_track_weight("ai-agents", cfg)
    unc = run.effective_track_weight(cl.UNCLASSIFIED_TRACK, cfg)
    assert ai == 1.3, ai            # the bonus the old fallback handed out
    assert unc == 1.0, unc          # neutral: neither promoted nor buried
    assert unc != ai


def test_a_config_without_the_unclassified_entry_still_weights_it_neutrally():
    """The catch-all is a real config enum member, but a config that predates it must not crash and
    must not inherit some other track's weight: the documented fallback is a neutral 1.0."""
    cfg = lib.load_config()
    cfg["tracks"] = [t for t in cfg["tracks"] if t.get("id") != cl.UNCLASSIFIED_TRACK]
    assert run._track_weight(cl.UNCLASSIFIED_TRACK, cfg) == 1.0


def test_an_unclassified_card_is_scored_at_the_neutral_weight_end_to_end():
    """The classifier label is only half of it; the weight it implies has to reach the live score.
    An unclassified card must not score like an ai-agents card built from identical evidence."""
    cfg = lib.load_config()
    base = {"summary": _NO_KEYWORD_TEXT, "entities": ["SyntheticCo"],
            "evidence": [{"source": "v2ex", "origin": "v2ex", "url": "https://example.com/a"},
                         {"source": "hn", "origin": "hn", "url": "https://example.com/b"}],
            "score_breakdown": {"track_fit": 70, "timing": 70, "feasibility": 70,
                                "competition": 70, "executability": 70},
            "age_hours": 5.0, "velocity": 0.2}
    unc = run.build_card(dict(base, title="a rapid decision"), cfg, "r")
    ai = run.build_card(dict(base, title="a rapid decision", track="ai-agents"), cfg, "r")
    assert unc["track"] == "unclassified" and unc["track_matched"] is False
    assert unc["final_score"] < ai["final_score"], (unc["final_score"], ai["final_score"])
    # and the cross-day dedup key is filed under the honest track, not under ai-agents
    assert unc["canonical_key"].endswith("::unclassified"), unc["canonical_key"]


@pytest.mark.parametrize("kw,text", [
    ("ci", "social media strategy"),
    ("ci", "a difficult decision"),
    ("ci", "specific requirements"),
    ("api", "rapid growth"),
    ("api", "raising capital"),
    ("app", "an apple a day"),
    ("app", "a happy customer"),
])
def test_a_short_keyword_does_not_fire_inside_a_longer_word(kw, text):
    assert cl.keyword_hit(text, kw) is False, (kw, text)


@pytest.mark.parametrize("kw,text", [
    ("ci", "our ci pipeline is red"),
    ("ci", "ci/cd for everyone"),
    ("api", "the api is public"),
    ("api", "rate-limited api, sadly"),
    ("app", "ship the app today"),
    ("open source", "an open source database"),
    ("self-host", "you can self-host it"),
    ("done-for-you", "a done-for-you service"),
])
def test_the_boundary_rule_still_matches_real_occurrences(kw, text):
    """NEGATIVE CONTROL for the parametrized test above: a boundary rule that never matched anything
    would make every case above pass while destroying the classifier. Real hits, including multi-word
    and hyphenated keywords and a keyword next to punctuation, must still fire."""
    assert cl.keyword_hit(text, kw) is True, (kw, text)


def test_a_cjk_keyword_still_matches_as_a_substring():
    """Chinese has no spaces, so an ASCII word boundary would never match. A CJK keyword keeps plain
    substring semantics; without this the entire Chinese community lane would classify nothing."""
    assert cl.keyword_hit("这是一个自动化工作流的讨论", "自动化") is True


def test_the_exclude_mute_stays_a_greedy_substring():
    """The mute list is a safety veto, so over-matching is the SAFE direction and the token-boundary
    rewrite must NOT have tightened it: ``memecoins`` is still muted by ``memecoin``."""
    assert cl.check_excluded("new memecoins launching", "", lib.load_config()) == "memecoin"


# ============================================== 5. _topic_filter_match keeps hyphenated terms whole
# The tokenizer split on every non-alphanumeric character, so a hyphenated filter term was shredded
# into its generic halves: ``open-source`` became ``open`` + ``source``, and either half satisfied the
# OR on its own. A filter written to TIGHTEN a noisy handle then kept nearly everything, which is the
# exact "the filter does not filter" bug it was supposed to close.
def test_a_hyphenated_filter_term_is_not_split_into_generic_halves():
    assert CO._topic_filter_terms("open-source OR self-host") == ["open-source", "self-host"]
    assert CO._topic_filter_terms("(no-code OR AI)") == ["no-code", "ai"]


@pytest.mark.parametrize("text", [
    "we are open to feedback",              # 'open' alone
    "the source of the problem",            # 'source' alone
    "host your own weekend project",        # 'host' alone
    "no more meetings, just code",          # 'no' and 'code' as separate words
])
def test_a_half_of_a_hyphenated_term_does_not_satisfy_the_filter(text):
    assert CO._topic_filter_match(text, "open-source OR self-host OR no-code") is False, text


@pytest.mark.parametrize("text", [
    "an open-source alternative",
    "you can self-host the whole thing",
    "a no-code builder for teams",
])
def test_the_whole_hyphenated_term_still_matches(text):
    """NEGATIVE CONTROL: a tokenizer that dropped hyphenated terms entirely would make the test above
    pass while silently discarding the filter."""
    assert CO._topic_filter_match(text, "open-source OR self-host OR no-code") is True, text


def test_a_short_filter_term_does_not_match_inside_a_longer_word():
    f = "(AI OR coding OR startup OR ship)"
    assert CO._topic_filter_match("email is a training brain drain", f) is False
    assert CO._topic_filter_match("a long-term relationship", f) is False
    assert CO._topic_filter_match("AI agents are shipping fast", f) is True   # 'AI' as a term
    assert CO._topic_filter_match("we ship on friday", f) is True


def test_an_empty_or_operator_only_filter_keeps_everything():
    for f in ("", None, "OR", "AND OR NOT"):
        assert CO._topic_filter_match("anything at all", f) is True, f
