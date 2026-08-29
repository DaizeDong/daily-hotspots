#!/usr/bin/env python3
"""The demand lane has to be visible in the durable record, or the loop cannot learn about it.

Measured 2026-08-28: 150 demand candidates had been produced over the archive's lifetime and NOT ONE
of the 197 archived rows carried `side`, `crowdedness` or `pain_evidence`. `_jsonl_record` is an
allow list, which is the right shape, but an allow list silently drops whatever nobody remembered to
add. The weekly yield pass replays that ledger as its numerator, so the lane the digest LEADS with
was not underperforming, it was unmeasurable. Every historical question about it was unanswerable,
and an empty demand column read as a quiet day rather than as a broken lane.

Two properties, both of the kind that rot quietly:

  * the archive row carries the three fields that define a demand card   (test_archive_row_*)
  * the yield pass reports contributions PER LANE, and reports "unknown"
    for pre-schema-2 rows instead of quietly calling them supply         (test_yield_*)

The second half of that second point is the one that matters most. Coercing an absent field to
"supply" would make every old row look like evidence about the supply lane, which is exactly the
fabrication the no-fabrication rule exists to stop.
"""
from __future__ import annotations

import importlib

import archive as A
from lib import parse_ts

Y = importlib.import_module("yield")

NOW = parse_ts("2026-08-28T12:00:00Z")


def _card(**kw):
    card = {
        "canonical_key": "k::saas-niche",
        "opportunity_id": "op-test",
        "title": "t", "summary": "s", "track": "saas-niche",
        "final_score": 72.0, "grade": "B",
        "score_breakdown": {"track_fit": 70, "timing": 70, "feasibility": 70,
                            "competition": 70, "executability": 70},
        "why_now": "w", "action": "a", "contrarian_insight": "c",
        "independent_source_count": 3,
        "confidence": 1.0,
        "source_set": ["reddit", "glassdoor", "capterra"],
        "evidence": [{"origin": "reddit.com", "url": "https://reddit.com/1",
                      "source": "reddit", "ts": "2026-08-27T00:00:00Z"}],
        "run_id": "daily-2026-08-28",
    }
    card.update(kw)
    return card


# --------------------------------------------------------------------------- the archive row
def test_archive_row_keeps_the_three_fields_that_define_a_demand_card():
    rec = A._jsonl_record(_card(side="demand", crowdedness=35,
                                pain_evidence="I re-key the same PDF four times a week"))
    assert rec["side"] == "demand"
    assert rec["crowdedness"] == 35
    assert rec["pain_evidence"] == "I re-key the same PDF four times a week"


def test_archive_row_defaults_side_to_supply_only_when_the_card_never_had_one():
    """A card with no `side` IS a supply card by the pipeline's own convention, so defaulting here is
    correct. What must not happen is the field vanishing, which is the defect."""
    rec = A._jsonl_record(_card())
    assert rec["side"] == "supply"


def test_archive_row_normalizes_side_casing():
    assert A._jsonl_record(_card(side="  DEMAND "))["side"] == "demand"


def test_archive_row_keeps_corroboration_detail_so_a_ranking_can_be_re_justified():
    rec = A._jsonl_record(_card(side="supply"))
    assert rec["confidence"] == 1.0
    assert rec["source_set"] == ["reddit", "glassdoor", "capterra"]
    assert rec["independent_source_count"] == 3


def test_archive_schema_version_moved_so_readers_can_tell_the_generations_apart():
    assert A.ARCHIVE_SCHEMA_VERSION >= 2
    assert A._jsonl_record(_card())["schema_version"] == A.ARCHIVE_SCHEMA_VERSION


def test_crowdedness_absent_is_none_not_zero():
    """Zero crowdedness means "a blue ocean", which is the strongest possible reading. An unmeasured
    field must never be written as the most favorable value."""
    assert A._jsonl_record(_card(side="demand"))["crowdedness"] is None


# --------------------------------------------------------------------------- the yield split
def _rec(opp, side, origin="reddit.com", pushed=True):
    r = {"opportunity_id": opp, "run_id": "daily-2026-08-27",
         "last_seen": "2026-08-27T00:00:00Z", "first_seen": "2026-08-27T00:00:00Z",
         "pushed": pushed,
         "evidence": [{"origin": origin, "source": origin, "url": f"https://{origin}/{opp}",
                       "ts": "2026-08-27T00:00:00Z"}]}
    if side is not None:
        r["side"] = side
    return r


def _pulls(origin, n=4):
    return [{"run_id": f"daily-2026-08-2{i}", "ts": f"2026-08-2{i}T00:00:00Z",
             "source": origin, "pulled": 10, "kept": 1} for i in range(1, n + 1)]


def _yc():
    return Y.yield_cfg({})


def test_yield_splits_contributions_by_lane():
    recs = [_rec("op-a", "demand"), _rec("op-b", "demand"), _rec("op-c", "supply")]
    out = Y.compute_yield(recs, _pulls("reddit.com"), NOW, _yc())
    st = [v for v in out.values() if v["name"] == "reddit"][0]
    assert st["contributions"] == 3
    assert st["demand_contributions"] == 2
    assert st["supply_contributions"] == 1
    assert st["unknown_side_contributions"] == 0


def test_a_row_without_side_is_unknown_and_is_never_counted_as_supply():
    """Pre-schema-2 rows. Coercing them to supply would manufacture evidence about a lane from rows
    that say nothing about it, and the archive holds 197 such rows."""
    out = Y.compute_yield([_rec("op-old", None)], _pulls("reddit.com"), NOW, _yc())
    st = [v for v in out.values() if v["name"] == "reddit"][0]
    assert st["contributions"] == 1
    assert st["unknown_side_contributions"] == 1
    assert st["supply_contributions"] == 0
    assert st["demand_contributions"] == 0


def test_the_lane_split_always_sums_to_the_total():
    recs = [_rec("op-a", "demand"), _rec("op-b", None), _rec("op-c", "supply"),
            _rec("op-d", "demand")]
    out = Y.compute_yield(recs, _pulls("reddit.com"), NOW, _yc())
    for st in out.values():
        assert (st["demand_contributions"] + st["supply_contributions"]
                + st["unknown_side_contributions"]) == st["contributions"], st


def test_a_source_that_feeds_only_one_lane_reads_that_way():
    """The point of the split: two sources with the SAME total are not interchangeable when one of
    them is the only thing feeding the lane the product leads with."""
    recs = [_rec("op-a", "demand", origin="capterra.com"),
            _rec("op-b", "demand", origin="capterra.com"),
            _rec("op-c", "supply", origin="news.ycombinator.com"),
            _rec("op-d", "supply", origin="news.ycombinator.com")]
    out = Y.compute_yield(recs, _pulls("capterra.com") + _pulls("news.ycombinator.com"),
                          NOW, _yc())
    by = {v["name"]: v for v in out.values()}
    assert by["capterra.com"]["demand_contributions"] == 2
    assert by["capterra.com"]["supply_contributions"] == 0
    assert by["hackernews"]["supply_contributions"] == 2
    assert by["hackernews"]["demand_contributions"] == 0
    assert by["capterra.com"]["contributions"] == by["hackernews"]["contributions"]


def test_source_aliases_fold_so_one_channel_is_not_listed_twice(tmp_path):
    """Lane names and hostnames for the same channel were both emitted over the archive's life, so
    the raw key space double counted: `hackernews` and `news.ycombinator.com` were two rows with 68
    and 29 contributions. A table that lists one source twice understates both halves."""
    recs = [_rec("op-a", "supply", origin="news.ycombinator.com"),
            _rec("op-b", "supply", origin="hackernews"),
            _rec("op-c", "supply", origin="https://www.producthunt.com/posts/x"),
            _rec("op-d", "supply", origin="product-hunt")]
    out = Y.compute_yield(recs, _pulls("hackernews"), NOW, _yc())
    names = {v["name"] for v in out.values()}
    assert "news.ycombinator.com" not in names
    assert "producthunt.com" not in names
    assert {"hackernews", "product-hunt"} <= names
    assert [v for v in out.values() if v["name"] == "hackernews"][0]["contributions"] == 2


def test_alias_folding_does_not_merge_genuinely_different_sources():
    """Over-rejection control: an aliaser that over-folds would hide a real source behind another."""
    assert Y._norm_source_key("v2ex") != Y._norm_source_key("linux.do")
    assert Y._norm_source_key("qbitai") != Y._norm_source_key("geekpark")
    assert Y._norm_source_key("capterra.com") == "capterra.com"
