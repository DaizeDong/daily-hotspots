#!/usr/bin/env python3
"""The shipped source config has to be checkable, or it is prose that happens to be a dict.

The agent that wrote the new source entries said this plainly rather than burying it: five
mutations, including DELETING THE ENTIRE sources block, turned zero of 1007 tests red. A config
nobody asserts on is a config that can rot, be half-deleted in a merge, or quietly disagree with the
recipe that reads it, and nothing goes red.

These tests pin the properties that actually matter operationally. They deliberately do NOT pin
tunable values (a weight, a page count), because a test that freezes a knob turns tuning into a test
edit and teaches people to edit tests. They pin STRUCTURE and the safety rules:

  * the block exists and is not empty                       (deleting it goes red)
  * every source declares the fields the pipeline reads     (a half-written entry goes red)
  * every source carries a health control                   (an unprobeable source goes red)
  * a source needing a credential does not ship ON          (a surprise dependency goes red)
  * no contact detail is hardcoded anywhere in the block    (the SEC User-Agent leak goes red)
"""
from __future__ import annotations

import re

import pytest

from lib import DEFAULT_CONFIG

SOURCES = DEFAULT_CONFIG.get("sources", {})

# The six lanes probed and judged on 2026-08-27 and re-verified 2026-08-29. Named explicitly so
# deleting one is a test failure rather than a silently smaller fleet.
EXPECTED_NEW = ("trustpilot", "appstore-rss", "sec-edgar-fts", "federal-register",
                "usaspending", "the-muse")


def test_the_sources_block_exists_and_is_not_empty():
    """The mutation the author reported as undetectable: delete the block."""
    assert isinstance(SOURCES, dict)
    assert len(SOURCES) >= len(EXPECTED_NEW), f"sources block shrank to {sorted(SOURCES)}"


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_each_probed_source_is_present(name):
    assert name in SOURCES, f"{name} vanished from DEFAULT_CONFIG['sources']"


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_each_source_declares_what_the_pipeline_reads(name):
    src = SOURCES[name]
    assert isinstance(src, dict)
    for key in ("enabled", "side", "fetch"):
        assert key in src, f"{name} is missing {key}"
    assert isinstance(src["enabled"], bool), f"{name}.enabled must be a bool, not a truthy string"
    assert src["side"] in ("demand", "supply", "both"), f"{name}.side is {src['side']!r}"


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_each_source_carries_a_health_control(name):
    """A source with no control cannot be probed, and an unprobeable source is exactly the one that
    dies quietly. sourcehealth refuses an assertion-free control, so the reference must exist here."""
    src = SOURCES[name]
    ctrl = src.get("_control") or src.get("control")
    assert ctrl, f"{name} declares no health control, so it can never be checked"
    assert len(str(ctrl)) > 40, f"{name}'s control is too thin to describe an assertion: {ctrl!r}"


# --------------------------------------------------------------------------- safety rules
def test_a_source_needing_a_credential_does_not_ship_enabled():
    """Trustpilot needs a Firecrawl key. Shipping it ON would make a fresh clone fail in a way that
    reads as "the source returned nothing" rather than as "you never gave it a key"."""
    tp = SOURCES["trustpilot"]
    assert tp["enabled"] is False, "trustpilot ships ON but the public default cannot assume a key"


def test_appstore_rss_stays_off_until_it_is_re_probed():
    """Independently re-probed 2026-08-29: HTTP 200, 873 bytes, ZERO entries, on a track id the
    search endpoint confirms is correct. Alive at the transport layer, empty at the content layer,
    which is the fail-open shape. It must not ship ON on the strength of the earlier measurement."""
    assert SOURCES["appstore-rss"]["enabled"] is False


def test_keyless_sources_that_reproduced_are_on():
    """Over-rejection control. A config where everything is off is as useless as one where a broken
    lane is on, and these three were each verified twice with real calls."""
    for name in ("federal-register", "usaspending", "the-muse", "sec-edgar-fts"):
        assert SOURCES[name]["enabled"] is True, f"{name} verified working but ships disabled"


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ALLOWED_SYNTHETIC = ("example.com", "example.org", "example.net")


def test_no_real_contact_detail_is_hardcoded_in_the_source_block():
    """SEC REQUIRES a User-Agent carrying a contact address, and a real address baked into a public
    repo is the leak this fleet has already had to rewrite git history to remove. Any address that
    survives here must be in the synthetic namespace, and the operator supplies the real one through
    private config at run time."""
    blob = repr(SOURCES)
    for hit in _EMAIL.findall(blob):
        assert any(hit.lower().endswith(d) for d in _ALLOWED_SYNTHETIC), \
            f"a non-synthetic contact address is hardcoded in the source config: {hit}"


def test_the_sec_lane_says_the_user_agent_comes_from_private_config():
    """The requirement is easy to satisfy wrongly by pasting an address, so the entry must state
    where the real value comes from."""
    blob = repr(SOURCES["sec-edgar-fts"]).lower()
    assert "user-agent" in blob or "user_agent" in blob
    assert any(w in blob for w in ("private", "env", "config")), \
        "the SEC entry does not say where its User-Agent comes from"


def test_every_source_states_its_cost():
    """A source whose cost nobody wrote down is a source whose bill arrives as a surprise, and two of
    the probed candidates were rejected on cost alone."""
    for name in EXPECTED_NEW:
        assert SOURCES[name].get("_cost"), f"{name} does not state its cost"


# --------------------------------------------------------------------------- config reaches code
# Measured 2026-08-29: of the six demand lanes, only trustpilot and usaspending matched the parser
# registry by string. The config spells them with hyphens (appstore-rss, federal-register) and two
# under different names entirely (sec-edgar-fts vs sec_fulltext, the-muse vs muse_jobs), so four of
# six could be enabled in watchlist.json and never reach a parser. The unknown-lane branch recorded
# that as a failed pull rather than losing it silently, which is the only reason it was findable,
# but a lane that can only ever fail is not wired. These tests are the wire.
import run as RUN


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_every_configured_lane_resolves_to_a_parser(name):
    parser = RUN.lane_parser(name)
    assert parser is not None, \
        f"{name} is configured but no parser answers to that name; the lane can only ever fail"
    assert callable(parser)


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_every_configured_lane_resolves_to_a_distinct_origin(name):
    """The origin is what the yield engine keys on. If a lane falls through to its raw config name
    while its signals carry the host, the numerator and the denominator key on different strings."""
    origin = RUN.NEW_SOURCE_ORIGINS.get(RUN.canonical_lane(name))
    assert origin, f"{name} has no origin mapping, so its pulls and its signals would disagree"
    assert "." in origin, f"{origin!r} does not look like a host"


def test_the_six_lanes_map_to_six_distinct_parsers():
    """An alias table is easy to get wrong in the other direction: two config names collapsing onto
    one parser would silently merge two lanes."""
    got = [RUN.lane_parser(n) for n in EXPECTED_NEW]
    assert len({p.__name__ for p in got}) == len(EXPECTED_NEW), \
        f"two configured lanes share a parser: {[p.__name__ for p in got]}"


@pytest.mark.parametrize("spelling", [
    "appstore-rss", "appstore_rss", "APPSTORE_RSS", "Appstore Rss", "appstore.rss",
])
def test_lane_names_are_matched_insensitively_to_separator_and_case(spelling):
    assert RUN.lane_parser(spelling) is RUN.parse_appstore_rss


def test_a_genuinely_unknown_lane_still_has_no_parser():
    """Over-rejection control. Normalizing must not turn every string into a match, or the
    unknown-lane guard stops guarding."""
    for bogus in ("", "nope", "reddit", "twitterapi", "not-a-lane"):
        assert RUN.lane_parser(bogus) is None, f"{bogus!r} resolved to a parser"


def test_an_unknown_lane_is_recorded_as_a_failed_pull_not_dropped():
    """The guard that made this bug findable in the first place. It must stay."""
    out = RUN.collect_new_source("definitely-not-a-lane", {"x": 1}, run_id="daily-2026-08-29")
    assert out["signals"] == []
    assert out["pulls"] and out["pulls"][0].get("error")
    assert "unknown source lane" in out["pulls"][0]["error"]


@pytest.mark.parametrize("name", EXPECTED_NEW)
def test_the_dispatch_path_itself_accepts_every_configured_name(name):
    """Testing lane_parser() alone is NOT enough, and that is not a hypothetical: the first version
    of these tests passed while the real dispatch site still did NEW_SOURCE_PARSERS.get(lane), so
    the original bug survived its own regression test. Drive collect_new_source, the function the
    --sources leg actually calls, with the name as the CONFIG spells it."""
    out = RUN.collect_new_source(name, {}, run_id="daily-2026-08-29")
    err = (out["pulls"][0].get("error") or "") if out.get("pulls") else ""
    assert "unknown source lane" not in err, \
        f"the dispatch path does not recognize {name!r} as the config spells it: {err}"
