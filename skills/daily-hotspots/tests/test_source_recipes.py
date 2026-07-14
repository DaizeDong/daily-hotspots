#!/usr/bin/env python3
"""Source-recipe parse fixtures (source-coverage design §6 / §11) — PARSE-ONLY, no live calls.

Pins the deterministic field-extraction contract for the three new source shapes, driven by the
committed sample fixtures (tests/fixtures/sources/):

  * linux.do  /latest.rss   -> run.parse_rss     (title/link/category/pubDate/description)
  * V2EX      hot.json       -> run.parse_v2ex    (title/url/node.name/replies/created)
  * X tweet   get_user_last_tweets -> run._parse_created_at + run._tweet_faves (the two
              non-trivial tweet fields: the twitter date format and the like-count field)

Everything here is pure: the FETCH belongs to the SKILL/MCP layer; only the parse is under test.
Stdlib only, no network, clock frozen by conftest (DAILY_HOTSPOTS_NOW = 2026-06-25T12:00:00Z).
"""
import json
from pathlib import Path

import run as R

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sources"


def _rss_items():
    return R.parse_rss((FIXTURES / "linuxdo-latest.rss").read_text(encoding="utf-8"))


def _v2ex_items():
    return R.parse_v2ex(json.loads((FIXTURES / "v2ex-hot.json").read_text(encoding="utf-8")))


def _x_payload():
    return json.loads((FIXTURES / "x-get_user_last_tweets.json").read_text(encoding="utf-8"))


# =============================================================== linux.do RSS (parse_rss)
def test_rss_extracts_every_item():
    items = _rss_items()
    assert len(items) == 6                              # 6 <item>s in the fixture
    assert all(set(("title", "url", "category", "heat", "ts", "summary")) <= set(it) for it in items)


def test_rss_first_item_fields():
    it = _rss_items()[0]
    assert "multi-agent" in it["title"]
    assert it["url"] == "https://linux.do/t/topic/512001/1"      # <link>, not <guid>
    assert it["category"] == "前沿快讯"                            # <category> child
    assert it["ts"] == "2026-06-25T09:12:00Z"                    # <pubDate> RFC822 -> ISO Z
    assert it["heat"] is None                                    # RSS carries no reply count
    assert "MCP" in it["summary"]                                # <description> is DATA


def test_rss_category_routing_labels_are_preserved():
    cats = [it["category"] for it in _rss_items()]
    # keep-side (前沿快讯 / 开发调优) and drop-side (搞七捻三 / 福利羊毛 / 跳蚤市场) both present so the
    # community-lane category filter has real material on both sides of the whitelist.
    assert cats == ["前沿快讯", "开发调优", "前沿快讯", "搞七捻三", "福利羊毛", "跳蚤市场"]


def test_rss_bad_input_is_empty_not_raise():
    assert R.parse_rss("") == []
    assert R.parse_rss(None) == []
    assert R.parse_rss("<rss><channel><item><title>unclosed") == []   # malformed XML -> [] not crash


# =============================================================== V2EX JSON (parse_v2ex)
def test_v2ex_extracts_every_topic():
    items = _v2ex_items()
    assert len(items) == 9
    assert all(set(("title", "url", "category", "heat", "ts", "summary")) <= set(it) for it in items)


def test_v2ex_first_topic_fields():
    it = _v2ex_items()[0]
    assert "AI agent" in it["title"]
    assert it["url"] == "https://www.v2ex.com/t/1099001"
    assert it["category"] == "create"                           # node.name is the routing category
    assert it["heat"] == 87                                     # replies -> heat
    assert it["ts"] == "2026-06-25T08:00:00Z"                   # epoch created -> ISO Z
    assert it["summary"].startswith("写了大半年")                 # content -> summary (DATA)


def test_v2ex_node_names_are_the_category_axis():
    cats = [it["category"] for it in _v2ex_items()]
    assert cats == ["create", "programmer", "cloud", "geek", "create", "programmer",
                    "jobs", "all4all", "flamewar"]


def test_v2ex_bad_input_is_tolerant():
    assert R.parse_v2ex(None) == []
    assert R.parse_v2ex({"not": "a list"}) == []
    # a malformed row is skipped, not fatal; a row missing node still parses with category None
    got = R.parse_v2ex([{"title": "no node", "url": "u", "replies": 3, "created": 1782374400},
                        "garbage", 42])
    assert len(got) == 1 and got[0]["category"] is None and got[0]["heat"] == 3


# =============================================================== X tweet field extraction
def test_x_created_at_twitter_format_parses():
    dt = R._parse_created_at("Thu Jun 25 08:30:00 +0000 2026")
    from lib import iso
    assert dt is not None and iso(dt) == "2026-06-25T08:30:00Z"


def test_x_created_at_iso_fallback_and_garbage():
    from lib import iso
    assert iso(R._parse_created_at("2026-06-25T09:00:00Z")) == "2026-06-25T09:00:00Z"
    assert R._parse_created_at("not-a-date") is None
    assert R._parse_created_at("") is None and R._parse_created_at(None) is None


def test_x_faves_reads_like_count_over_alternatives():
    tweets = _x_payload()["tweets"]
    assert R._tweet_faves(tweets[0]) == 5820.0          # viral
    assert R._tweet_faves(tweets[1]) == 63.0            # PRE-VIRAL (< 500 keyword floor)
    # field precedence + a bool is never a fave count
    assert R._tweet_faves({"favoriteCount": 12}) == 12.0
    assert R._tweet_faves({"likeCount": True}) == 0.0
    assert R._tweet_faves({}) == 0.0


def test_x_fixture_dates_extract_to_expected_iso():
    from lib import iso
    tweets = _x_payload()["tweets"]
    assert iso(R._parse_created_at(tweets[0]["createdAt"])) == "2026-06-25T08:30:00Z"   # fresh
    assert iso(R._parse_created_at(tweets[2]["createdAt"])) == "2026-06-10T14:00:00Z"   # stale
