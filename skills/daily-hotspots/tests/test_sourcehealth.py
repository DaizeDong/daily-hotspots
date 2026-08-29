"""Tests for scripts/sourcehealth.py, the fail-open detector.

The negative controls ARE the point of this file. The failure being hunted is a source that returns
a well formed, empty, successful payload, so the first and loudest test here is the one that proves
an empty success is NOT `ok`. Every other test exists to keep that verdict honest: a detector that
flagged everything would pass the fail-open tests and be worthless, so the over-rejection controls
(real content is ok, a large real page quoting a challenge phrase is ok) carry equal weight.

No test in this file touches the network. `_no_network` is autouse and makes urlopen explode, so a
regression that starts reaching out during a probe fails here instead of on a plane.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import sourcehealth as sh

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sourcehealth.py"


# --------------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any live HTTP from a unit test is a bug in the test, not a slow test."""
    def _boom(*a, **k):
        raise AssertionError("unit tests must never open a socket")
    monkeypatch.setattr(sh.urllib.request, "urlopen", _boom)


def const(payload):
    """A fetcher that hands back `payload` and claims success, the shape brightdata took."""
    return lambda _query: payload


def raiser(exc):
    def _f(_query):
        raise exc
    return _f


SCRAPE = sh.CONTROLS["web_scrape"]
SEARCH = sh.CONTROLS["web_search"]
RSS = sh.CONTROLS["appstore_rss"]

GOOD_PAGE = ("<!doctype html><html><head><title>Example Domain</title></head><body>"
             "<h1>Example Domain</h1><p>This domain is for use in illustrative examples.</p>"
             + "<p>filler</p>" * 20 + "</body></html>")
GOOD_SEARCH = {"organic": [{"title": "Weather today", "link": "https://example.com/w"}],
               "current_page": 1}
GOOD_RSS = {"feed": {"entry": [{"title": {"label": "one star, app crashes"}} for _ in range(50)]}}


def interstitial_body(n: int = 160) -> str:
    """The Trustpilot-without-stealth shape: a short HTTP 200 body that is a challenge page.

    Measured at 153 to 170 bytes in the session that motivated this module."""
    core = "<html><body>Verifying your connection</body></html>"
    return core + "<!--" + "x" * max(0, n - len(core) - 7) + "-->"


def probe(payload, control=SCRAPE, name="src"):
    return sh.probe_source(name, const(payload), control)


# --------------------------------------------------------------------------- THE fail-open tests

def test_empty_string_success_is_fail_open_not_ok():
    """FIRST TEST, and the reason the module exists.

    brightdata scrape_as_markdown on https://example.com returned an empty content block with no
    error. The control guarantees "Example Domain". Empty here can only mean the transport lied."""
    state, detail = sh.classify("", SCRAPE)
    assert state == sh.FAIL_OPEN
    assert state != sh.OK
    assert "no content" in detail


def test_empty_dict_success_is_fail_open():
    state, _ = sh.classify({}, SCRAPE)
    assert state == sh.FAIL_OPEN


def test_empty_organic_list_success_is_fail_open():
    """The literal payload measured from brightdata search_engine for "weather today".

    Note the payload is NOT empty as a dict: it carries current_page. Only the control's items_path
    assertion can tell that the answer is missing, which is why controls declare where to look."""
    payload = {"organic": [], "current_page": 1}
    assert payload, "the payload is truthy; a naive emptiness check would call this ok"
    state, detail = sh.classify(payload, SEARCH)
    assert state == sh.FAIL_OPEN
    assert "organic" in detail


def test_empty_mcp_content_block_is_fail_open():
    """The MCP wire shape of the same failure: a well formed block list holding empty text."""
    state, _ = sh.classify({"content": [{"type": "text", "text": ""}]}, SCRAPE)
    assert state == sh.FAIL_OPEN


def test_none_payload_is_fail_open():
    state, detail = sh.classify(None, SCRAPE)
    assert state == sh.FAIL_OPEN
    assert "None" in detail


def test_missing_items_path_is_fail_open():
    """A success payload that does not even carry the key the control asserts on."""
    state, detail = sh.classify({"current_page": 1}, SEARCH)
    assert state == sh.FAIL_OPEN
    assert "absent" in detail


def test_empty_rss_feed_is_fail_open():
    state, _ = sh.classify({"feed": {"entry": []}}, RSS)
    assert state == sh.FAIL_OPEN


def test_rss_feed_with_no_entry_key_at_all_is_fail_open():
    """A REAL payload, measured 2026-08-29 against the live App Store RSS on the first live run of
    this module: HTTP 200, a complete and well formed feed object, and simply no `entry` key. Every
    field a reader would sanity check is present. Only the control notices."""
    feed_without_reviews = {"feed": {"author": {"name": {"label": "iTunes Store"}},
                                     "icon": {"label": "http://itunes.apple.com/favicon.ico"},
                                     "id": {"label": "https://itunes.apple.com/us/rss/..."},
                                     "link": [], "rights": {"label": "Copyright 2026 Apple Inc."},
                                     "title": {"label": "iTunes Store: Customer Reviews"},
                                     "updated": {"label": "2026-08-29T04:00:00-07:00"}}}
    assert feed_without_reviews["feed"], "the feed is fully populated; only the reviews are gone"
    state, detail = sh.classify(feed_without_reviews, RSS)
    assert state == sh.FAIL_OPEN
    assert "feed.entry" in detail


def test_fail_open_reaches_the_probe_result():
    r = probe("")
    assert r["state"] == sh.FAIL_OPEN
    assert r["state"] in sh.ALARM_STATES


# --------------------------------------------------------------------------- down, not fail-open

def test_fetcher_that_raises_is_down_not_fail_open():
    r = sh.probe_source("boom", raiser(RuntimeError("connection reset")), SCRAPE)
    assert r["state"] == sh.DOWN
    assert r["state"] != sh.FAIL_OPEN
    assert "connection reset" in r["detail"]


def test_timeout_is_down():
    r = sh.probe_source("slowpoke", raiser(socket.timeout("timed out")), SCRAPE)
    assert r["state"] == sh.DOWN
    assert "timeout" in r["detail"]


def test_timeout_error_class_is_down():
    r = sh.probe_source("slowpoke", raiser(TimeoutError("deadline exceeded")), SCRAPE)
    assert r["state"] == sh.DOWN


def test_error_payload_is_down_the_tavily_shape():
    """tavily over quota fails CLOSED with a clear error. That is the CORRECT behavior, and it must
    classify as down, never as fail-open: the contrast is what makes brightdata legible."""
    state, detail = sh.classify({"error": "usage limit exceeded"}, SEARCH)
    assert state == sh.DOWN
    assert "usage limit exceeded" in detail


def test_http_status_payload_is_down_the_arctic_shift_shape():
    """arctic-shift returned HTTP 500 on 6 of 12 sequential calls. Honest failure, so: down."""
    state, detail = sh.classify({"status": 500}, sh.CONTROLS["reddit_archive"])
    assert state == sh.DOWN
    assert "HTTP 500" in detail


def test_error_payload_wins_over_emptiness():
    """A payload that is BOTH errored and empty is down: the source told us, so believe it."""
    state, _ = sh.classify({"error": "429", "organic": []}, SEARCH)
    assert state == sh.DOWN


def test_fetch_outcome_error_is_down():
    r = sh.probe_source("q", lambda _q: sh.FetchOutcome(payload=GOOD_PAGE, error="401 no key"),
                        SCRAPE)
    assert r["state"] == sh.DOWN


# --------------------------------------------------------------------------- over-rejection control

def test_real_page_is_ok():
    """A detector that flags everything is useless. This is the control on the control."""
    r = probe(GOOD_PAGE)
    assert r["state"] == sh.OK
    assert r["latency_ms"] is not None


def test_real_search_result_is_ok():
    state, _ = sh.classify(GOOD_SEARCH, SEARCH)
    assert state == sh.OK


def test_real_rss_page_is_ok():
    state, _ = sh.classify(GOOD_RSS, RSS)
    assert state == sh.OK


def test_root_list_payload_is_ok():
    """v2ex hands back a bare list; items_path "" means the payload root."""
    ctl = sh.CONTROLS["json_root_list"]
    assert sh.classify([{"id": 1}, {"id": 2}], ctl)[0] == sh.OK
    assert sh.classify([], ctl)[0] == sh.FAIL_OPEN


def test_mcp_content_block_with_real_text_is_ok():
    state, _ = sh.classify({"content": [{"type": "text", "text": GOOD_PAGE}]}, SCRAPE)
    assert state == sh.OK


# --------------------------------------------------------------------------- interstitials

def test_bot_interstitial_is_fail_open_not_ok():
    body = interstitial_body(160)
    assert 150 <= len(body) <= 200, "the measured Trustpilot interstitial was 153 to 170 bytes"
    state, detail = sh.classify(body, sh.CONTROLS["trustpilot_scrape"])
    assert state == sh.FAIL_OPEN
    assert state != sh.OK
    assert "interstitial" in detail


def test_interstitial_branch_runs_before_the_emptiness_branch():
    """The detail must name the challenge page, otherwise the operator debugs the wrong thing."""
    ctl = dict(sh.CONTROLS["trustpilot_scrape"], expect={"min_chars": 1})
    state, detail = sh.classify(interstitial_body(170), ctl)
    assert state == sh.FAIL_OPEN
    assert "interstitial" in detail


def test_large_real_page_quoting_the_phrase_is_not_flagged():
    """Over-rejection control: a 35k char Trustpilot page whose reviews complain about a captcha
    wall is real content. Only a SHORT body carrying the phrase is a challenge page."""
    page = ("Example Domain " + "review text " * 3000
            + "one reviewer wrote: verifying your connection forever, terrible")
    assert len(page) > sh.INTERSTITIAL_MAX_CHARS
    state, _ = sh.classify(page, sh.CONTROLS["trustpilot_scrape"])
    assert state == sh.OK


# --------------------------------------------------------------------------- degraded

def test_slow_success_is_degraded():
    ticks = iter([0.0, 30.0])   # 30 seconds, well past the 15s scrape budget
    r = sh.probe_source("slow", const(GOOD_PAGE), SCRAPE, clock=lambda: next(ticks))
    assert r["state"] == sh.DEGRADED
    assert "slow" in r["detail"]
    assert r["latency_ms"] == 30000.0


def test_retry_success_is_degraded():
    r = sh.probe_source("retried", lambda _q: sh.FetchOutcome(GOOD_PAGE, attempts=3), SCRAPE)
    assert r["state"] == sh.DEGRADED
    assert "3 attempts" in r["detail"]


def test_partial_success_is_degraded():
    r = sh.probe_source("partial", lambda _q: sh.FetchOutcome(GOOD_PAGE, partial=True), SCRAPE)
    assert r["state"] == sh.DEGRADED
    assert "partial" in r["detail"]


def test_wrong_page_with_content_is_degraded_not_fail_open():
    """Content came back, just not the control's content. That is a real answer to the wrong
    question: degraded. Calling it fail-open would dilute the one state that must stay sharp."""
    other = "<html><body>" + "a totally different page " * 40 + "</body></html>"
    state, detail = sh.classify(other, SCRAPE)
    assert state == sh.DEGRADED
    assert "assertion missed" in detail


def test_fast_success_is_not_degraded():
    ticks = iter([0.0, 0.4])
    r = sh.probe_source("fast", const(GOOD_PAGE), SCRAPE, clock=lambda: next(ticks))
    assert r["state"] == sh.OK


# --------------------------------------------------------------------------- unknown

def test_missing_fetcher_is_unknown_and_is_neither_ok_nor_down():
    r = sh.probe_source("nofetch", None, SCRAPE)
    assert r["state"] == sh.UNKNOWN
    assert r["state"] not in (sh.OK, sh.DOWN, sh.FAIL_OPEN)
    assert r["latency_ms"] is None, "nothing ran, so there is no latency to report"
    assert "nothing was checked" in r["detail"]


def test_non_callable_fetcher_is_unknown():
    r = sh.probe_source("weird", "not a function", SCRAPE)
    assert r["state"] == sh.UNKNOWN


def test_no_control_is_unknown():
    r = sh.probe_source("nocontrol", const(GOOD_PAGE), None)
    assert r["state"] == sh.UNKNOWN
    assert r["control_used"] is None


def test_control_that_asserts_nothing_is_unknown_not_ok():
    """The module's negative control on itself: a probe that cannot fail is not a probe."""
    toothless = {"id": "toothless", "kind": "x", "query": {}, "expect": {}}
    assert sh.control_assertions(toothless) == []
    r = sh.probe_source("toothless", const(""), toothless)
    assert r["state"] == sh.UNKNOWN
    assert "asserts nothing" in r["detail"]


def test_unknown_is_never_folded_into_ok_or_down():
    specs = [{"name": "a", "kind": "web_scrape"},
             {"name": "b", "kind": "web_scrape"},
             {"name": "c", "kind": "web_scrape"}]
    summary = sh.probe_all(specs, {"a": const(GOOD_PAGE)})
    assert summary[sh.OK] == 1
    assert summary[sh.UNKNOWN] == 2
    assert summary[sh.DOWN] == 0
    assert summary[sh.FAIL_OPEN] == 0


# --------------------------------------------------------------------------- probe contract

CONTRACT_KEYS = {"name", "state", "detail", "latency_ms", "checked_at", "control_used"}


def test_probe_result_carries_exactly_the_contract_keys():
    r = probe(GOOD_PAGE)
    assert set(r) == CONTRACT_KEYS
    assert r["name"] == "src"
    assert r["state"] in sh.STATES
    assert isinstance(r["detail"], str) and r["detail"]
    assert r["checked_at"].endswith("Z")
    assert r["control_used"] == "web_scrape:example.com"


def test_every_exit_path_returns_the_same_contract_keys_and_a_legal_state():
    """Including the paths that never called anything. A result that is missing a key, or that
    carries an extra one, is a result some caller will read wrong."""
    cases = [probe(GOOD_PAGE), probe(""), probe(interstitial_body()),
             sh.probe_source("nofetch", None, SCRAPE),
             sh.probe_source("uncallable", 7, SCRAPE),
             sh.probe_source("nocontrol", const(GOOD_PAGE), None),
             sh.probe_source("toothless", const(""), {"id": "t", "expect": {}}),
             sh.probe_source("boom", raiser(RuntimeError("x")), SCRAPE),
             sh.probe_source("slow", raiser(socket.timeout()), SCRAPE)]
    for r in cases:
        assert set(r) == CONTRACT_KEYS, r
        assert r["state"] in sh.STATES, r
        assert isinstance(r["detail"], str) and r["detail"].strip(), r


def test_probe_never_raises_whatever_the_fetcher_does():
    """A failed probe is a RESULT, not an exception. The health check must survive its own subjects."""
    for bad in (raiser(RuntimeError("x")), raiser(ValueError("y")), raiser(KeyError("z")),
                raiser(socket.timeout()), lambda _q: 1 / 0, const(object())):
        r = sh.probe_source("bad", bad, SCRAPE)
        assert r["state"] in sh.STATES


def test_probe_all_contract_shape_and_counts_add_up():
    specs = [{"name": "up", "kind": "web_scrape"},
             {"name": "liar", "kind": "web_scrape"},
             {"name": "dead", "kind": "web_scrape"},
             {"name": "unprobed", "kind": "web_scrape"}]
    summary = sh.probe_all(specs, {"up": const(GOOD_PAGE), "liar": const(""),
                                   "dead": raiser(RuntimeError("nope"))})
    assert set(summary) == {"results", sh.OK, sh.DEGRADED, sh.DOWN, sh.FAIL_OPEN, sh.UNKNOWN}
    assert len(summary["results"]) == 4
    assert sum(summary[s] for s in sh.STATES) == len(summary["results"])
    assert (summary[sh.OK], summary[sh.FAIL_OPEN], summary[sh.DOWN], summary[sh.UNKNOWN]) \
        == (1, 1, 1, 1)


def test_probe_all_accepts_a_mapping_of_specs():
    summary = sh.probe_all({"up": {"kind": "web_scrape"}}, {"up": const(GOOD_PAGE)})
    assert summary[sh.OK] == 1
    assert summary["results"][0]["name"] == "up"


def test_probe_all_survives_a_malformed_spec():
    summary = sh.probe_all([{"kind": "web_scrape"}, "bare-name", 17], {})
    assert len(summary["results"]) == 3
    assert summary[sh.UNKNOWN] == 3


# --------------------------------------------------------------------------- coverage block

def test_coverage_block_names_the_broken_sources():
    specs = [{"name": "up", "kind": "web_scrape"},
             {"name": "brightdata", "kind": "web_search"},
             {"name": "arctic", "kind": "reddit_archive"},
             {"name": "unprobed", "kind": "web_scrape"}]
    summary = sh.probe_all(specs, {"up": const(GOOD_PAGE),
                                   "brightdata": const({"organic": [], "current_page": 1}),
                                   "arctic": const({"status": 500})})
    block = sh.coverage_block(summary)
    assert set(block) == set(sh.STATES) | {"names_down", "names_fail_open"}
    assert block["names_fail_open"] == ["brightdata"]
    assert block["names_down"] == ["arctic"]
    assert block[sh.UNKNOWN] == 1


def test_coverage_block_of_an_all_unknown_run_is_not_a_clean_bill():
    summary = sh.probe_all([{"name": "a", "kind": "web_scrape"}], {})
    block = sh.coverage_block(summary)
    assert block[sh.OK] == 0 and block[sh.UNKNOWN] == 1
    assert sh.verdict(summary) == "unchecked"


# --------------------------------------------------------------------------- controls as data

def test_control_for_resolves_inline_kind_and_named_controls():
    assert sh.control_for({"control": {"id": "mine", "expect": {"min_chars": 1}}})["id"] == "mine"
    assert sh.control_for({"kind": "web_search"})["id"] == SEARCH["id"]
    assert sh.control_for({"control": "appstore_rss"})["id"] == RSS["id"]
    assert sh.control_for({"name": "orphan"}) is None
    assert sh.control_for({"kind": "no-such-kind"}) is None
    assert sh.control_for("not a spec") is None


def test_a_new_source_can_declare_its_own_control_without_touching_the_classifier():
    spec = {"name": "brand-new", "control": {"id": "brand-new:ping", "query": {"url": "x"},
                                             "expect": {"items_path": "rows", "min_items": 2}}}
    ok = sh.probe_all([spec], {"brand-new": const({"rows": [1, 2]})})
    thin = sh.probe_all([spec], {"brand-new": const({"rows": [1]})})
    assert ok[sh.OK] == 1
    assert thin[sh.FAIL_OPEN] == 1
    assert ok["results"][0]["control_used"] == "brand-new:ping"


def test_every_shipped_spec_resolves_to_a_control_that_can_fail():
    """Guards the next source someone adds: a spec with no assertion silently probes nothing."""
    for spec in sh.DEFAULT_SPECS:
        ctl = sh.control_for(spec)
        assert ctl is not None, "%s declares no control" % spec["name"]
        assert sh.control_assertions(ctl), "%s has a control that can never fail" % spec["name"]


def test_every_shipped_control_rejects_an_empty_payload():
    """The one property every control must have, checked against every control we ship."""
    for kind, ctl in sh.CONTROLS.items():
        for empty in ("", {}, [], None):
            state, _ = sh.classify(empty, ctl)
            assert state == sh.FAIL_OPEN, "%s called %r ok" % (kind, empty)


def test_wiring_is_explicit_about_what_it_cannot_reach():
    """MCP-routed lanes have no in-process fetcher, and the module says so rather than guessing."""
    for name in sh.MCP_ONLY:
        assert name not in sh.LIVE_FETCHERS
    declared = {s["name"] for s in sh.DEFAULT_SPECS}
    for name in sh.LIVE_FETCHERS:
        assert name in declared, "%s is wired but not declared as a source" % name


def test_default_ua_carries_no_real_contact():
    """A public repo has leaked a real address in a SEC User-Agent before. The default stays fake."""
    assert "example.com" in sh.DEFAULT_UA
    assert "gmail" not in sh.DEFAULT_UA.lower()


# --------------------------------------------------------------------------- verdict and reports

def test_verdict_and_exit_code_mapping():
    def summary(**counts):
        s = {st: 0 for st in sh.STATES}
        s.update(counts)
        s["results"] = [{"name": "x", "state": sh.OK}] * 0
        return s
    assert sh.exit_code(summary(**{sh.OK: 2})) == 0
    assert sh.exit_code(summary(**{sh.OK: 1, sh.UNKNOWN: 1})) == 1
    assert sh.exit_code(summary(**{sh.OK: 1, sh.DEGRADED: 1})) == 1
    assert sh.exit_code(summary(**{sh.UNKNOWN: 3})) == 2
    assert sh.exit_code(summary(**{sh.OK: 5, sh.DOWN: 1})) == 3
    assert sh.exit_code(summary(**{sh.OK: 5, sh.FAIL_OPEN: 1})) == 3


def test_report_envelope_separates_clean_from_unchecked():
    clean = sh.report_envelope(sh.probe_all([{"name": "a", "kind": "web_scrape"}],
                                            {"a": const(GOOD_PAGE)}))
    unchecked = sh.report_envelope(sh.probe_all([{"name": "a", "kind": "web_scrape"}], {}))
    assert clean["verdict"] == "all_ok" and clean["sources_checked"] == 1
    assert unchecked["verdict"] == "unchecked" and unchecked["sources_checked"] == 0
    assert clean["verdict"] != unchecked["verdict"]


def test_text_summary_is_loud_about_fail_open_and_quiet_never_lies():
    env = sh.report_envelope(sh.probe_all(
        [{"name": "brightdata", "kind": "web_search"}, {"name": "ok-src", "kind": "web_scrape"}],
        {"brightdata": const({"organic": []}), "ok-src": const(GOOD_PAGE)}))
    line = sh.text_summary(env)
    assert "brightdata" in line and "!!" in line
    unchecked = sh.text_summary(sh.report_envelope(
        sh.probe_all([{"name": "a", "kind": "web_scrape"}], {})))
    assert "未检查" in unchecked
    assert unchecked != line


def test_write_then_read_report_roundtrips(tmp_path):
    env = sh.report_envelope(sh.probe_all([{"name": "a", "kind": "web_scrape"}],
                                          {"a": const(GOOD_PAGE)}))
    p = sh.write_report(tmp_path / "nested" / "health.json", env)
    assert sh.read_report(p)["verdict"] == "all_ok"


def test_write_report_hard_fails_it_does_not_shrug(tmp_path):
    """Writer rule: an IO failure propagates. A health report that silently failed to save is the
    same class of lie this module was built to catch."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(OSError):
        sh.write_report(blocker / "sub" / "health.json", {"verdict": "all_ok"})


def test_read_report_degrades_to_none(tmp_path):
    assert sh.read_report(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sh.read_report(bad) is None


def test_specs_from_config_skips_disabled_and_honors_an_inline_health_block():
    cfg = {"sources": {"v2ex": {"enabled": True},
                       "trend-pulse": {"enabled": False},
                       "custom": {"enabled": True,
                                  "health": {"kind": "web_search"}}}}
    specs = sh.specs_from_config(cfg)
    names = {s["name"] for s in specs}
    assert names == {"v2ex", "custom"}
    assert sh.control_for([s for s in specs if s["name"] == "custom"][0])["id"] == SEARCH["id"]


def test_specs_from_config_leaves_an_unknown_source_probeless():
    specs = sh.specs_from_config({"sources": {"mystery": {"enabled": True}}})
    assert sh.control_for(specs[0]) is None
    assert sh.probe_all(specs, {})[sh.UNKNOWN] == 1


# --------------------------------------------------------------------------- CLI exit codes

def run_cli(tmp_path, specs, observations=None, extra=None):
    sp = tmp_path / "specs.json"
    sp.write_text(json.dumps(specs), encoding="utf-8")
    argv = [sys.executable, str(SCRIPT), "--specs", str(sp)]
    if observations is not None:
        op = tmp_path / "obs.json"
        op.write_text(json.dumps(observations), encoding="utf-8")
        argv += ["--observations", str(op)]
    argv += list(extra or [])
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(argv, capture_output=True, env=env)
    out = proc.stdout.decode("utf-8", "replace")
    return proc.returncode, json.loads(out.strip().splitlines()[-1]) if out.strip() else {}


SPECS_ONE = [{"name": "brightdata", "kind": "web_scrape"}]


def test_cli_exits_0_when_all_ok(tmp_path):
    rc, env = run_cli(tmp_path, SPECS_ONE, {"brightdata": GOOD_PAGE})
    assert rc == 0
    assert env["verdict"] == "all_ok"
    assert env["counts"]["ok"] == 1


def test_cli_exits_3_when_a_source_fails_open(tmp_path):
    """The alarm. An empty success must page someone, because nothing downstream will."""
    rc, env = run_cli(tmp_path, SPECS_ONE, {"brightdata": ""})
    assert rc == 3
    assert env["verdict"] == "attention"
    assert env["coverage"]["names_fail_open"] == ["brightdata"]


def test_cli_exits_3_when_a_source_is_down(tmp_path):
    rc, env = run_cli(tmp_path, [{"name": "tavily", "kind": "web_search"}],
                      {"tavily": {"error": "usage limit exceeded"}})
    assert rc == 3
    assert env["coverage"]["names_down"] == ["tavily"]


def test_cli_exits_2_when_nothing_could_be_checked(tmp_path):
    """No fetchers wired at all. This must NOT be exit 0: "clean" and "did not check" are different
    outputs, and the exit code is the first place that has to hold."""
    rc, env = run_cli(tmp_path, SPECS_ONE)
    assert rc == 2
    assert env["verdict"] == "unchecked"
    assert env["sources_checked"] == 0
    assert "nothing was checked" in env["note"]


def test_cli_exits_1_when_partly_checked(tmp_path):
    rc, env = run_cli(tmp_path, SPECS_ONE + [{"name": "v2ex", "kind": "json_root_list"}],
                      {"brightdata": GOOD_PAGE})
    assert rc == 1
    assert env["verdict"] == "partial"


def test_cli_clean_and_unchecked_reports_are_visibly_different(tmp_path):
    _, clean = run_cli(tmp_path, SPECS_ONE, {"brightdata": GOOD_PAGE})
    _, blind = run_cli(tmp_path, SPECS_ONE)
    assert clean["verdict"] != blind["verdict"]
    assert clean["counts"] != blind["counts"]


def test_cli_exits_4_when_the_probe_cannot_even_be_set_up(tmp_path):
    """An unreadable input must not land on 1 (partial) or 0 (clean). It gets its own code, and it
    prints NO report, so neither a human nor a cron job can read it as a result."""
    sp = tmp_path / "specs.json"
    sp.write_text(json.dumps(SPECS_ONE), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--specs", str(sp),
                           "--observations", str(tmp_path / "absent.json")],
                          capture_output=True, env=env)
    assert proc.returncode == 4
    assert proc.returncode not in (0, 1, 2, 3)
    assert proc.stdout.decode("utf-8", "replace").strip() == ""
    assert "nothing was checked" in proc.stderr.decode("utf-8", "replace")


def test_cli_exits_4_on_a_malformed_specs_file(tmp_path):
    sp = tmp_path / "specs.json"
    sp.write_text("{not json", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--specs", str(sp)],
                          capture_output=True, env=env)
    assert proc.returncode == 4
    assert proc.stdout.decode("utf-8", "replace").strip() == ""


def test_cli_writes_a_report_file(tmp_path):
    out = tmp_path / "health.json"
    rc, _ = run_cli(tmp_path, SPECS_ONE, {"brightdata": ""}, extra=["--out", str(out)])
    assert rc == 3
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["coverage"]["names_fail_open"] == ["brightdata"]


def test_cli_text_mode_prints_the_chinese_line(tmp_path):
    sp = tmp_path / "specs.json"
    sp.write_text(json.dumps(SPECS_ONE), encoding="utf-8")
    op = tmp_path / "obs.json"
    op.write_text(json.dumps({"brightdata": ""}), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--specs", str(sp),
                           "--observations", str(op), "--text"], capture_output=True, env=env)
    assert proc.returncode == 3
    assert "假成功" in proc.stderr.decode("utf-8", "replace")
