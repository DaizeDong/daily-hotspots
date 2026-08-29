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
