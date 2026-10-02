#!/usr/bin/env python3
"""Source-health contracts across probe, collection, coverage, and digest rendering.

Generated synthetic provider-shaped records pass through each module in sequence. These controls are deterministic and make no claim about current endpoint behavior."""
import json

import importlib.util
from pathlib import Path

_source11_spec = importlib.util.spec_from_file_location(
    "source11_" + Path(__file__).stem, Path(__file__).parents[3] / "tools" / "make_fixtures.py")
_source11_gen = importlib.util.module_from_spec(_source11_spec)
_source11_spec.loader.exec_module(_source11_gen)

import pytest

import digest as DG
import run as R
import collect as CO
import sourcehealth as SH


# The four states the round is actually about, on named lanes, in an order that is NOT sorted, so an
# implementation that preserves probe order and one that sorts cannot accidentally agree.
_OBSERVATIONS = _source11_gen.source12_contract_observations()

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
# The handoff: run -> digest, through build_coverage, with synthetic observations
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
# Generated provider-shaped payloads exercise the seam between health probe and parser.
# ===========================================================================

def _sec_live_shape(n=3):
    """Generated metadata-only SEC-shaped response."""
    return _source11_gen.source11_sec_page(n)


def _sec_highlight_shape():
    """Generated response with a parser-usable quote."""
    return _source11_gen.source11_sec_page(1, highlight=True)


def test_the_sec_control_asserts_the_field_the_sec_parser_consumes():
    """Metadata-only SEC-shaped hits must not pass a control requiring parser-usable quotes."""
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
    """Generated reviews using the path token required by the existing URL assertion."""
    return _source11_gen.source11_appstore_page(n, track)


def test_every_app_store_review_gets_its_own_reconcilable_identity():
    """Generated reviews sharing one app URL retain distinct reconciliation identities."""
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
    """An empty generated Apple feed is valid; the populated control establishes parser health."""
    empty = {"feed": {"author": {"name": {"label": "iTunes Store"}}}}
    res = CO.parse_appstore_rss(empty)
    assert res["errors"] == [] and res["kept"] == 0 and res["pulled"] == 0
    assert SH.classify(empty, SH.CONTROLS["appstore_rss"])[0] == SH.FAIL_OPEN
