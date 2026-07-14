#!/usr/bin/env python3
"""Attribution tagging end-to-end (source-coverage design §5.1/§6/§8/§11).

Every collected evidence item must carry its origin — ``origin_handle`` for an X account, or
``origin_source`` for a community lane — because that tag is the yield engine's NUMERATOR and the
thing the >=2-independent-origin red line counts. This suite drives the deterministic collect layer
(run.collect_roster / collect_community_source / collect_sources) against the committed source
fixtures and pins:

  * roster tweets are tagged origin_handle; the per-account origin makes two DIFFERENT handles count
    as two origins while one handle's many tweets collapse to one (no fake crowd);
  * a roster member QUOTING a non-roster voice surfaces THAT voice as a propose-add candidate
    (origin_handle=<quoted>, via_handle=<member>) — the §8 add feed;
  * a low rostered faves floor catches a PRE-VIRAL post the min_faves:500 keyword search would drop;
  * community items are tagged origin_source and category-filtered by the source's watchlist config;
  * the pulls-log DENOMINATOR is recorded honestly — one line per attempted handle/source, an absent
    handle gets NO line (unobserved, not fabricated), an empty pull gets pulled=0 (observable dead weight);
  * collect_sources merges both lanes and threads every tag + pulls line through.

Deterministic: stdlib only, no network/MCP, clock frozen by conftest; every cfg is the parse-only
fixture watchlist (never the live config), every path is explicit.
"""
import json
from pathlib import Path

import run as R
from lib import load_config, parse_ts
from run import count_independent_sources

FIX = Path(__file__).resolve().parent / "fixtures"
SRC = FIX / "sources"
NOW = parse_ts("2026-06-25T12:00:00Z")


def _cfg():
    return load_config(str(FIX / "watchlist.with-sources.json"))   # min_faves_rostered = 25


def _roster(*handles):
    return {"schema_version": 1, "entries": [
        {"handle": h, "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"} for h in handles]}


def _x_payload():
    return json.loads((SRC / "x-get_user_last_tweets.json").read_text(encoding="utf-8"))


def _v2ex():
    return R.parse_v2ex(json.loads((SRC / "v2ex-hot.json").read_text(encoding="utf-8")))


def _linuxdo():
    return R.parse_rss((SRC / "linuxdo-latest.rss").read_text(encoding="utf-8"))


# =============================================================== roster -> origin_handle
def test_roster_tweets_are_tagged_origin_handle():
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    roster_sigs = [s for s in out["signals"] if s.get("via_handle") is None]
    assert len(roster_sigs) == 3                       # tweet0,1,3 kept; tweet2 (06-10) < last_run dropped
    assert all(s["origin_handle"] == "karpathy" for s in roster_sigs)
    assert all(s["origin"] == "x.com/karpathy" for s in roster_sigs)
    assert all(s.get("origin_handle") for s in out["signals"])   # EVERY signal is attributed


def test_pre_viral_post_is_caught_by_low_rostered_floor():
    # tweet[1] has likeCount 63 — below the 500 keyword-search floor, above the rostered floor of 25.
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    assert any(s.get("faves") == 63 for s in out["signals"]), "the pre-viral post must survive"


def test_absurd_min_faves_rostered_cannot_blind_collection_and_gut_roster():
    # HARDEN r3: an UNBOUNDED min_faves_rostered would drop EVERY tweet (kept=0) each run while a
    # pulls-log line still accrues -> the yield engine eventually reads the whole roster as dead and
    # auto-disables it (routing around the §9 anti-mass-prune clamp). The floor is CAPPED at the
    # keyword faves floor (500), so a productive handle's viral posts still survive collection and
    # keep the numerator alive — the roster can't be gutted by one fat-fingered knob.
    cfg = load_config(str(FIX / "watchlist.with-sources.json"))
    cfg["sources"]["twitterapi"]["min_faves_rostered"] = 1_000_000
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=cfg,
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    kept = [s for s in out["signals"] if s.get("via_handle") is None]
    assert kept, "the cap must keep a productive handle's >=500-fave tweets (roster not gutted)"
    assert all(s["faves"] >= 500 for s in kept)          # only >=500-fave posts survive the capped floor
    assert out["pulls"][0]["kept"] == len(kept) >= 1     # numerator stays alive -> no mass-prune


def test_quoted_nonroster_voice_becomes_propose_add_candidate():
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    q = [s for s in out["signals"] if s.get("via_handle")]
    assert len(q) == 1
    assert q[0]["origin_handle"] == "evalmaxxer"       # the amplified non-roster voice
    assert q[0]["via_handle"] == "karpathy"            # amplified BY this roster member
    assert q[0]["origin"] == "x.com/evalmaxxer"


def test_per_handle_origin_feeds_the_two_origin_red_line():
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    all_sigs = out["signals"]
    karpathy_only = [s for s in all_sigs if s["origin_handle"] == "karpathy"]
    # one handle's three tweets are ONE independent origin (never a fabricated crowd)...
    assert count_independent_sources(karpathy_only) == 1
    # ...and a roster member QUOTING a non-roster voice does NOT manufacture a 2nd independent origin
    # from that single pull (anti-echo-chamber quote guard, HARDEN r4): karpathy + its quoted
    # evalmaxxer collapse to ONE independent origin — the quote is a propose-add feed, not
    # corroboration. Two GENUINELY distinct handles (see test_two_distinct_handles_clear... below)
    # are what clear the red line.
    assert count_independent_sources(all_sigs) == 1


def test_pulls_log_denominator_is_recorded_per_handle():
    out = R.collect_roster(_roster("karpathy"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    assert len(out["pulls"]) == 1
    p = out["pulls"][0]
    assert p["handle"] == "karpathy" and p["pulled"] == 4 and p["kept"] == 3


# =============================================================== §9 no-fabrication at the collect edge
def test_absent_handle_gets_no_pulls_line():
    # 'ghost' is rostered+enabled but was not attempted this run (no response) -> honestly unobserved.
    out = R.collect_roster(_roster("karpathy", "ghost"), {"karpathy": _x_payload()}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    handles = {p["handle"] for p in out["pulls"]}
    assert handles == {"karpathy"}                     # ghost emits NO denominator line


def test_empty_pull_is_observable_dead_weight():
    # a handle attempted but returning nothing STILL gets a line (pulled=0) so auto-prune can see it.
    out = R.collect_roster(_roster("karpathy"), {"karpathy": {"tweets": []}}, cfg=_cfg(),
                           last_run="2026-06-20T00:00:00Z", now=NOW)
    assert out["signals"] == []
    assert out["pulls"] == [{"run_id": out["pulls"][0]["run_id"], "ts": out["pulls"][0]["ts"],
                             "handle": "karpathy", "pulled": 0, "kept": 0}]


# =============================================================== community -> origin_source
def test_v2ex_items_tagged_origin_source_and_node_filtered():
    out = R.collect_community_source("v2ex", _v2ex(), cfg=_cfg(), last_run=None, now=NOW)
    assert len(out["signals"]) == 6                    # create/programmer/cloud/geek kept; jobs/all4all/flamewar dropped
    assert all(s["origin_source"] == "v2ex" for s in out["signals"])
    assert {s["category"] for s in out["signals"]} <= {"create", "programmer", "cloud", "geek"}
    assert out["pulls"] == [{"run_id": out["pulls"][0]["run_id"], "ts": out["pulls"][0]["ts"],
                             "source": "v2ex", "pulled": 9, "kept": 6}]


def test_linuxdo_items_tagged_origin_source_and_category_filtered():
    out = R.collect_community_source("linux.do", _linuxdo(), cfg=_cfg(), last_run=None, now=NOW)
    assert len(out["signals"]) == 3                    # 前沿快讯 x2 + 开发调优 x1 kept; gossip/welfare/market dropped
    assert all(s["origin_source"] == "linux.do" for s in out["signals"])
    p = out["pulls"][0]
    assert p["source"] == "linux.do" and p["pulled"] == 6 and p["kept"] == 3


# =============================================================== both lanes merged
def test_collect_sources_merges_both_lanes_with_tags_and_pulls():
    out = R.collect_sources(roster=_roster("karpathy"), roster_responses={"karpathy": _x_payload()},
                            community={"v2ex": _v2ex(), "linux.do": _linuxdo()},
                            cfg=_cfg(), last_run=None, now=NOW)
    sigs = out["signals"]
    assert any(s.get("origin_handle") == "karpathy" for s in sigs)
    assert any(s.get("origin_source") == "v2ex" for s in sigs)
    assert any(s.get("origin_source") == "linux.do" for s in sigs)
    pulls = out["pulls"]
    assert any(p.get("handle") == "karpathy" for p in pulls)
    assert any(p.get("source") == "v2ex" for p in pulls)
    assert any(p.get("source") == "linux.do" for p in pulls)
    assert out["run_id"]
