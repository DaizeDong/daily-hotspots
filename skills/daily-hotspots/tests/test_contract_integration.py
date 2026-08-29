#!/usr/bin/env python3
"""The source-health contract, checked by RUNNING the three implementations against each other.

Four agents built against one written contract: sourcehealth.py produces the probe result,
run.py folds it into coverage["source_health"], digest.py renders it. Each of those modules has its
own unit tests and each of them passed while the pieces still disagreed, because a unit test asks
"does my side match what I wrote down" and the interesting failure is "the two sides wrote it down
differently". So nothing here re-tests a single module. Every assertion below feeds one module's
real output into the next module's real input and asks whether the answer survives the trip.

The bug this file was written for: sourcehealth.coverage_block sorted names_down, and
run.normalize_source_health appended them in probe order. run.py accepts BOTH shapes, so the same
probe rendered two different coverage lines depending on which side happened to flatten it, and the
pushed message stopped being a function of the measured health alone.

Deterministic: stdlib only, no network, no live config. Fixture payloads are the documented response
shapes of the real services, hand synthesized, with example.com hosts and AcmeCorp brands.
"""
import json

import pytest

import digest as DG
import run as R
import collect as CO
import sourcehealth as SH


# The four states the round is actually about, on named lanes, in an order that is NOT sorted, so an
# implementation that preserves probe order and one that sorts cannot accidentally agree.
_OBSERVATIONS = {
    "tavily": {"error": "usage limit exceeded"},
    "brightdata": {"organic": [], "current_page": 1},
    "reddit": {"error": "HTTP 500"},
    "appstore-rss": {"feed": {"entry": [
        {"id": {"label": "1"},
         "content": {"label": "The app logs me out every single day and support never answers."},
         "im:rating": {"label": "1"},
         "link": {"attributes": {"href": "https://itunes.apple.com/us/review?id=1"}}}]}},
    "federal-register": {"results": [
        {"document_number": "2026-00001", "title": "A Rule", "type": "Rule",
         "publication_date": "2026-06-25",
         "html_url": "https://www.federalregister.gov/d/2026-00001"}]},
}

_LANES = ("tavily", "brightdata", "reddit", "appstore-rss", "federal-register")


def _summary(observations=None):
    """A REAL probe_all run over the real DEFAULT_SPECS, fed the raw payloads above."""
    obs = _OBSERVATIONS if observations is None else observations
    specs = [s for s in SH.DEFAULT_SPECS if s["name"] in _LANES]
    assert len(specs) == len(_LANES), "DEFAULT_SPECS lost a lane this test names"
    return SH.probe_all(specs, SH.observation_fetchers(obs))


# ===========================================================================
# The join: a name that exists on one side and not the other silently becomes "unknown"
# ===========================================================================

def test_every_default_config_source_can_be_probed_by_name():
    """lib.DEFAULT_CONFIG's source keys join to sourcehealth specs BY NAME. A rename on one side
    only turns a live lane into a permanent `unknown` and nothing else complains."""
    import lib
    cfg_names = set((lib.DEFAULT_CONFIG.get("sources") or {}))
    spec_names = {s["name"] for s in SH.DEFAULT_SPECS}
    assert cfg_names <= spec_names, (
        "config sources with no health spec (they would probe as `unknown` forever): %s"
        % sorted(cfg_names - spec_names))


def test_a_disabled_source_is_still_probed_by_default_specs():
    """lib disables brightdata for collection; the health probe must still watch it, because the
    reason it is disabled is exactly the thing being watched."""
    import lib
    bd = (lib.DEFAULT_CONFIG.get("sources") or {}).get("brightdata") or {}
    assert bd.get("enabled") is False
    assert "brightdata" not in {s["name"] for s in SH.specs_from_config(lib.DEFAULT_CONFIG)}
    assert "brightdata" in {s["name"] for s in SH.DEFAULT_SPECS}


# ===========================================================================
# The handoff: sourcehealth -> run
# ===========================================================================

def test_coverage_block_and_normalize_agree_key_for_key():
    summary = _summary()
    block = SH.coverage_block(summary)
    norm = R.normalize_source_health(summary)
    assert sorted(block) == sorted(norm), (
        "the two documented flatteners publish different key sets: %s vs %s"
        % (sorted(block), sorted(norm)))
    assert set(block) == set(R.SOURCE_HEALTH_STATES) | {"names_down", "names_fail_open"}
    assert set(R.SOURCE_HEALTH_STATES) == set(SH.STATES), (
        "run.py and sourcehealth.py disagree on the set of states themselves")


def test_both_flatteners_produce_the_identical_block_for_one_probe():
    """THE REGRESSION. Feeding run.py the raw probe and feeding it sourcehealth's own pre-flattened
    block are both supported, so they must not disagree, including on list order."""
    summary = _summary()
    from_raw = R.normalize_source_health(summary)
    from_block = R.normalize_source_health(SH.coverage_block(summary))
    assert from_raw == from_block, (
        "same probe, two coverage blocks: raw=%s block=%s"
        % (json.dumps(from_raw, sort_keys=True), json.dumps(from_block, sort_keys=True)))


def test_the_rendered_line_is_a_function_of_the_probe_not_of_who_flattened_it():
    summary = _summary()
    a = DG.source_health_segment({"source_health": R.normalize_source_health(summary)})
    b = DG.source_health_segment(
        {"source_health": R.normalize_source_health(SH.coverage_block(summary))})
    assert a == b, "the pushed coverage line depends on the handoff path: %r vs %r" % (a, b)


def test_normalize_is_a_fixed_point_over_its_own_output():
    """run.py reads back its own archived coverage. A second pass must not move anything."""
    once = R.normalize_source_health(_summary())
    assert R.normalize_source_health(once) == once


def test_probe_order_does_not_change_the_rendered_line():
    """Spec ordering is an accident of config iteration; the report is not allowed to depend on it."""
    specs = [s for s in SH.DEFAULT_SPECS if s["name"] in _LANES]
    fetchers = SH.observation_fetchers(_OBSERVATIONS)
    fwd = R.normalize_source_health(SH.probe_all(specs, fetchers))
    rev = R.normalize_source_health(SH.probe_all(list(reversed(specs)), fetchers))
    assert fwd == rev
    assert (DG.source_health_segment({"source_health": fwd})
            == DG.source_health_segment({"source_health": rev}))


# ===========================================================================
# The handoff: run -> digest, through build_coverage, on the real measured shapes
# ===========================================================================

def test_measured_failure_shapes_survive_to_the_rendered_line():
    """brightdata's well formed empty answer must reach the reader as `假成功`, not as `正常`, and
    must stay separate from an honest failure the whole way down."""
    cov = R.build_coverage([], [], [], [], [], {"below_floor": []}, [], None, health=_summary())
    sh = cov["source_health"]
    assert sh["fail_open_suspected"] == 1 and sh["names_fail_open"] == ["brightdata"]
    assert sh["down"] == 2 and sh["names_down"] == ["reddit", "tavily"]
    assert sh["ok"] == 2
    assert "source_health" not in cov["unmeasured"]

    seg = DG.source_health_segment(cov)
    assert "brightdata" in seg and "reddit" in seg and "tavily" in seg
    assert "假成功" in seg
    alert = DG.source_health_alert(cov)
    assert alert and any("brightdata" in ln for ln in alert)


def test_clean_and_never_checked_are_different_at_every_layer():
    """The house rule, checked across all three modules at once rather than inside any one."""
    green = _summary({n: _OBSERVATIONS[n] for n in ("appstore-rss", "federal-register")})
    green_specs = [s for s in SH.DEFAULT_SPECS if s["name"] in ("appstore-rss", "federal-register")]
    green = SH.probe_all(green_specs, SH.observation_fetchers(
        {n: _OBSERVATIONS[n] for n in ("appstore-rss", "federal-register")}))
    nothing = SH.probe_all(green_specs, {})

    assert SH.verdict(green) == "all_ok" and SH.verdict(nothing) == "unchecked"
    assert SH.exit_code(green) != SH.exit_code(nothing)

    cov_green = {"source_health": R.normalize_source_health(green)}
    cov_none = {"source_health": R.normalize_source_health(nothing)}
    cov_absent = {"source_health": None}
    assert R.normalize_source_health(None) is None

    segs = {DG.source_health_segment(c) for c in (cov_green, cov_none, cov_absent)}
    assert len(segs) == 3, "two of {clean, probed-nothing, never-probed} render alike: %s" % segs


def test_report_envelope_coverage_is_the_same_block_run_py_consumes():
    """The CLI's JSON output is what an operator pipes back in; it must be the same contract."""
    summary = _summary()
    env = SH.report_envelope(summary, wired=sorted(_OBSERVATIONS), note="observations")
    assert env["coverage"] == SH.coverage_block(summary)
    assert R.normalize_source_health(env["coverage"]) == R.normalize_source_health(summary)
    assert env["verdict"] == SH.verdict(summary)
    assert env["counts"] == {st: summary[st] for st in SH.STATES}
    assert env["sources_declared"] == len(_LANES)


@pytest.mark.parametrize("name", ["names_down", "names_fail_open"])
def test_name_lists_are_sorted_on_both_sides(name):
    summary = _summary()
    for produced in (SH.coverage_block(summary), R.normalize_source_health(summary)):
        assert produced[name] == sorted(produced[name])


# ===========================================================================
# The seam between the health probe and the parser, on the SHAPES the live endpoints actually
# return. Both sides were built from hand-written fixtures and each side's fixture was correct;
# what neither side had ever seen was the other one's blind spot on the same payload.
# ===========================================================================

def _sec_live_shape(n=3):
    """The shape efts.sec.gov ACTUALLY returns: `_source` metadata and NO `highlight` key.

    Synthesized from the live response measured 2026-08-29 (HTTP 200, 100 hits, zero highlights).
    Names are AcmeCorp/ExampleCo; the accession and CIK are made up in the real format."""
    return {"took": 31, "timed_out": False, "hits": {
        "total": {"value": 10000, "relation": "gte"},
        "hits": [{"_index": "edgar_file", "_id": "0001234567-26-00004%d:acme-8k.htm" % i,
                  "_score": 9.9,
                  "_source": {"ciks": ["0001234567"], "period_ending": "2026-06-2%d" % i,
                              "display_names": ["ACMECORP INC  (ACME)  (CIK 0001234567)"],
                              "root_forms": ["8-K"], "file_date": "2026-06-2%d" % i,
                              "form": "8-K", "adsh": "0001234567-26-00004%d" % i,
                              "file_type": "8-K", "items": ["2.02", "8.01"]}}
                 for i in range(n)]}}


def _sec_highlight_shape():
    """The shape the parser was BUILT for: the same hit, carrying a highlight fragment."""
    d = _sec_live_shape(1)
    d["hits"]["hits"][0]["highlight"] = {"content": [
        "identified a <em>material weakness</em> arising from <em>manual</em> reconciliation"]}
    return d


def test_the_sec_control_asserts_the_field_the_sec_parser_consumes():
    """MEASURED REGRESSION. The live endpoint answers 200 with a full hit list and no `highlight`
    at all, so the parser drops every hit under no_quote and the lane emits nothing. While the
    control asserted only that hits EXIST, the probe called that `ok`: green health, zero output,
    and no line anywhere that said so."""
    ctrl = SH.CONTROLS["sec_fts"]
    assert "substring" in SH.control_assertions(ctrl)

    live = _sec_live_shape()
    state, detail = SH.classify(live, ctrl)
    assert state == SH.DEGRADED, (
        "the probe calls a payload healthy that its own parser cannot use: %s" % detail)
    assert CO.parse_sec_fulltext(live)["kept"] == 0

    good = _sec_highlight_shape()
    assert SH.classify(good, ctrl)[0] == SH.OK
    assert CO.parse_sec_fulltext(good)["kept"] == 1


def test_health_verdict_and_parser_yield_do_not_contradict_each_other_on_sec():
    """The invariant, stated once: `ok` from the probe must mean the parser gets something."""
    for payload in (_sec_live_shape(), _sec_highlight_shape()):
        healthy = SH.classify(payload, SH.CONTROLS["sec_fts"])[0] == SH.OK
        usable = CO.parse_sec_fulltext(payload)["kept"] > 0
        assert healthy == usable, (
            "health says %s, parser kept %d" % (healthy, CO.parse_sec_fulltext(payload)["kept"]))


def _appstore_page(n=3, track="940247939"):
    """An iTunes reviews page in the live shape: every entry shares ONE app-level href, and the
    per-review identity lives in `entry.id.label`. Measured 2026-08-29 on a real 50-review page."""
    href = "https://itunes.apple.com/us/review?id=%s&type=Purple%%20Software" % track
    return {"feed": {"author": {"name": {"label": "iTunes Store"}}, "entry": [
        {"im:name": {"label": "AcmeCorp Field Ops"},
         "id": {"label": "https://itunes.apple.com/us/app/id%s" % track}},
    ] + [
        {"author": {"name": {"label": "reviewer%d" % i}},
         "im:rating": {"label": "1"}, "im:version": {"label": "8.4.%d" % i},
         "id": {"label": "1447172102%d" % i},
         "title": {"label": "Logs me out on shift %d" % i},
         "content": [{"label": "It logged me out mid shift %d and I lost the whole ticket." % i,
                      "attributes": {"type": "text"}}],
         "link": {"attributes": {"rel": "related", "href": href}},
         "updated": {"label": "2026-06-2%dT07:41:02-07:00" % i}}
        for i in range(n)]}}


def test_every_app_store_review_gets_its_own_reconcilable_identity():
    """MEASURED REGRESSION. Apple gives every review on a page the SAME app-level href, and
    run.signal_key joins on the url, so a real 46-review page collapsed to ONE identity: 46 pieces
    of verbatim pain that attribution, dedup and the unaccounted-signal count all saw as one."""
    res = CO.parse_appstore_rss(_appstore_page(3))
    sigs = res["signals"]
    assert len(sigs) == 3 and res["pulled"] == 4 and res["skipped_reasons"]["not_a_review"] == 1
    assert len({s["url"] for s in sigs}) == 3, "reviews share a url: %s" % [s["url"] for s in sigs]
    assert len({R.signal_key(s) for s in sigs}) == 3

    for s in sigs:
        # still the real, resolvable page: only a fragment was added, never a different host.
        assert s["url"].startswith("https://itunes.apple.com/us/review?id=940247939")
        assert "#" in s["url"]


def test_the_app_store_identity_is_stable_across_two_parses():
    a = [R.signal_key(s) for s in CO.parse_appstore_rss(_appstore_page(3))["signals"]]
    b = [R.signal_key(s) for s in CO.parse_appstore_rss(_appstore_page(3))["signals"]]
    assert a == b and len(set(a)) == 3


def test_an_empty_but_well_formed_apple_feed_is_an_honest_empty_not_a_failure():
    """Measured: three of five real apps answer HTTP 200 with an 873 byte feed and no entries.
    That is a real day for that app, and it must not read as an outage; the health control is what
    separates it from one, because the control's app id is chosen to always have reviews."""
    empty = {"feed": {"author": {"name": {"label": "iTunes Store"}}}}
    res = CO.parse_appstore_rss(empty)
    assert res["errors"] == [] and res["kept"] == 0 and res["pulled"] == 0
    assert SH.classify(empty, SH.CONTROLS["appstore_rss"])[0] == SH.FAIL_OPEN
