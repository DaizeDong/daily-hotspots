#!/usr/bin/env python3
"""A platform agreeing with itself is one channel of information, not N.

Measured on 197 archived cards over 44 days: 8 of them cleared the >=2 independent origin red line
with counts between 2 and 6 while EVERY piece of evidence came from x.com alone. Several were crypto
narratives echoing across six accounts inside a day. Per-handle origins are deliberate and must stay,
because the roster exists to surface a founder's post by identity and two different founders are two
different people. But six accounts on one platform were buying the TOP confidence multiplier, which
is what a story independently carried by three unrelated outlets is supposed to earn.

So each platform contributes at most `max_origins_per_platform` toward the count. Proportionate, not
fatal: the six-handle card still clears the red line, it just stops outranking better corroborated
work. These tests pin the three properties that make that true, and the one that makes it safe:

  * a single platform cannot manufacture a high count          (test_single_platform_*)
  * genuinely distinct platforms are untouched                 (test_cross_platform_*)
  * the roster's per-handle distinction survives               (test_two_handles_still_clear_*)
  * the cap is configurable and can be turned off              (test_cap_is_configurable, *_disabled)
"""
from __future__ import annotations

import copy

import pytest

import run as RUN
from lib import load_config


def _ev(origin, url, source=None):
    return {"origin": origin, "url": url, "source": source or origin,
            "signal": "x", "ts": "2026-08-27T00:00:00Z"}


def _cfg(cap=None):
    c = copy.deepcopy(load_config())
    if cap is not None:
        c["scoring"]["max_origins_per_platform"] = cap
    return c


# --------------------------------------------------------------------------- platform mapping
@pytest.mark.parametrize("origin,expected", [
    ("x.com/karpathy", "x.com"),
    ("x.com/levelsio", "x.com"),
    ("X.COM/Karpathy", "x.com"),
    ("https://x.com/foo", "x.com"),
    ("twitter.com/foo", "x.com"),
    ("twitterapi", "x.com"),
    ("x-roster", "x.com"),
    ("news.ycombinator.com", "news.ycombinator.com"),
    ("hackernews", "news.ycombinator.com"),
    ("www.reddit.com/r/Insurance", "reddit.com"),
    ("producthunt.com", "producthunt.com"),
    ("product-hunt", "producthunt.com"),
    ("mturk.com", "mturk.com"),
    ("some-outlet.example", "some-outlet.example"),
])
def test_platform_of_folds_accounts_and_lane_aliases_onto_their_host(origin, expected):
    assert RUN._platform_of(origin) == expected


def test_platform_of_keeps_genuinely_different_hosts_apart():
    """Over-rejection control: a mapper that folds everything together would silently reject the
    whole feed, which is a worse failure than the one being fixed."""
    hosts = {RUN._platform_of(h) for h in
             ("cnbc.com", "reuters.com", "geekpark.net", "qbitai.com", "v2ex.com", "linux.do")}
    assert len(hosts) == 6


# --------------------------------------------------------------------------- the defect itself
def test_single_platform_echo_cannot_manufacture_a_high_count():
    """THE measured case: six x.com accounts on one narrative reported six independent origins."""
    ev = [_ev(f"x.com/acct{i}", f"https://x.com/acct{i}/status/{i}") for i in range(6)]
    assert RUN.count_independent_sources(ev, _cfg()) == 2


def test_single_platform_echo_would_have_counted_six_before():
    """Pins that this test family is measuring the fix and not a tautology: with the cap disabled the
    old number comes back, so a regression that drops the cap goes red here."""
    ev = [_ev(f"x.com/acct{i}", f"https://x.com/acct{i}/status/{i}") for i in range(6)]
    assert RUN.count_independent_sources(ev, _cfg(cap=0)) == 6


def test_single_platform_echo_still_clears_the_red_line():
    """Proportionate, not fatal. The card is demoted by the confidence multiplier, not rejected: a
    guard that silently deleted a whole lane of cards would be the same class of defect it fixes."""
    cfg = _cfg()
    ev = [_ev(f"x.com/acct{i}", f"https://x.com/acct{i}/status/{i}") for i in range(6)]
    assert RUN.count_independent_sources(ev, cfg) >= int(cfg["scoring"]["min_independent_sources"])


def test_two_handles_still_clear_the_red_line_so_the_roster_keeps_working():
    """The roster's entire purpose is that two DIFFERENT founders are two different signals. The cap
    must not collapse them, or the pre-viral lane dies."""
    ev = [_ev("x.com/founder_a", "https://x.com/founder_a/status/1"),
          _ev("x.com/founder_b", "https://x.com/founder_b/status/2")]
    assert RUN.count_independent_sources(ev, _cfg()) == 2


def test_one_handle_alone_is_still_one():
    ev = [_ev("x.com/solo", "https://x.com/solo/status/1"),
          _ev("x.com/solo", "https://x.com/solo/status/2")]
    assert RUN.count_independent_sources(ev, _cfg()) == 1


# --------------------------------------------------------------------------- unaffected shapes
def test_cross_platform_evidence_is_untouched():
    ev = [_ev("x.com/founder", "https://x.com/founder/status/1"),
          _ev("news.ycombinator.com", "https://news.ycombinator.com/item?id=1"),
          _ev("reuters.com", "https://reuters.com/a"),
          _ev("arxiv.org", "https://arxiv.org/abs/1")]
    assert RUN.count_independent_sources(ev, _cfg()) == 4


def test_a_real_mixed_card_keeps_its_count():
    """The shape of a strong archived card: one platform contributes two accounts, three other
    outlets contribute one each. Nothing here should be discounted."""
    ev = [_ev("x.com/a", "https://x.com/a/status/1"),
          _ev("x.com/b", "https://x.com/b/status/2"),
          _ev("news.ycombinator.com", "https://news.ycombinator.com/item?id=9"),
          _ev("cnbc.com", "https://cnbc.com/x"),
          _ev("geekpark.net", "http://geekpark.net/news/1")]
    assert RUN.count_independent_sources(ev, _cfg()) == 5


def test_the_cap_composes_with_the_transload_guard():
    """Two guards, one answer. Identical URLs still collapse first, so the cap cannot be used to
    smuggle a wire reprint past the URL cap."""
    ev = [_ev(f"outlet{i}.example", "https://wire.example/same-story") for i in range(4)]
    assert RUN.count_independent_sources(ev, _cfg()) == 1


# --------------------------------------------------------------------------- configurability
def test_cap_is_configurable():
    ev = [_ev(f"x.com/acct{i}", f"https://x.com/acct{i}/status/{i}") for i in range(6)]
    assert RUN.count_independent_sources(ev, _cfg(cap=3)) == 3
    assert RUN.count_independent_sources(ev, _cfg(cap=1)) == 1


def test_cap_disabled_restores_the_old_behavior_exactly():
    ev = [_ev(f"x.com/acct{i}", f"https://x.com/acct{i}/status/{i}") for i in range(4)]
    assert RUN.count_independent_sources(ev, _cfg(cap=0)) == 4


def test_a_malformed_cap_falls_back_to_the_default_rather_than_disabling_the_guard():
    """Fail toward the guard being ON. A typo in the config must not silently switch off a control."""
    for bad in ("two", None, -1, [], {}):
        cfg = _cfg()
        cfg["scoring"]["max_origins_per_platform"] = bad
        got = RUN.cfg_max_origins_per_platform(cfg)
        assert got >= 0
        if bad in (None, "two", [], {}):
            assert got == RUN._DEFAULT_MAX_ORIGINS_PER_PLATFORM, f"{bad!r} disabled the guard"
