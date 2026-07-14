#!/usr/bin/env python3
"""Community-pulse renderer — dual-track Track 2 (source-coverage design §7).

The renderer (digest.render_community_pulse) turns single-origin community signals into a separate
lightweight `## 社区脉搏` section, rendered AFTER the opportunity cards, labeled
"⚠️ 单源未验证 · 社区小道消息". Contract asserted here:

  * correct label + header (from config, with a safe default)
  * link-only + one-line-why, with NO score / NO deep-dive (a rumor is never a scored opportunity)
  * cap enforcement (community_pulse.max_per_day)
  * dedup — within the batch AND across days (seen_keys) so a rumor never re-bubbles
  * ranking by freshness + community heat
  * build_markdown integration: section lands after the cards, and still renders on an empty-card day

Deterministic: stdlib only, no network, clock frozen by conftest (DAILY_HOTSPOTS_NOW).
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lib  # noqa: E402
import digest as dg  # noqa: E402

FIXTURES = HERE / "fixtures"
# conftest freezes DAILY_HOTSPOTS_NOW = 2026-06-25T12:00:00Z; keep all ts near that.
FRESH = "2026-06-25T11:00:00Z"
MID = "2026-06-24T12:00:00Z"
STALE = "2026-06-20T12:00:00Z"


def _fixture_cfg():
    """Load the parse-only source fixture (has the community_pulse block: max_per_day=8 + label)."""
    return lib.load_config(str(FIXTURES / "watchlist.with-sources.json"))


def _item(source, title, url, ts, heat=None, signal=None):
    it = {"source": source, "origin": source, "origin_source": source,
          "title": title, "url": url, "ts": ts}
    if heat is not None:
        it["heat"] = heat
    if signal is not None:
        it["signal"] = signal
    return it


# --------------------------------------------------------------------------- basics / label

def test_renderer_exists():
    assert hasattr(dg, "render_community_pulse")


def test_empty_input_is_empty_section():
    assert dg.render_community_pulse(None, cfg=_fixture_cfg()) == ""
    assert dg.render_community_pulse([], cfg=_fixture_cfg()) == ""


def test_label_and_header_from_config():
    md = dg.render_community_pulse([_item("v2ex", "topic", "https://v2ex.com/t/1", FRESH,
                                          heat=12, signal="12 replies · programmer")],
                                   cfg=_fixture_cfg())
    assert "## 社区脉搏" in md
    assert "⚠️ 单源未验证 · 社区小道消息" in md          # the required label, from config
    assert md.lstrip().startswith("## 社区脉搏")          # header is the first line of the section


def test_default_label_when_config_absent():
    # A cfg with no community_pulse block must still render with the built-in default label.
    md = dg.render_community_pulse([_item("linux.do", "t", "https://linux.do/t/9", FRESH)],
                                   cfg={"scoring": {}})
    assert "⚠️ 单源未验证 · 社区小道消息" in md


def test_disabled_config_suppresses_section():
    cfg = {"community_pulse": {"enabled": False, "max_per_day": 8}, "scoring": {}}
    md = dg.render_community_pulse([_item("v2ex", "t", "https://v2ex.com/t/2", FRESH)], cfg=cfg)
    assert md == ""


# --------------------------------------------------------------------------- link-only + no score

def test_link_only_one_line_and_no_score():
    # An item carrying scored-card-shaped fields must NOT leak any score into the pulse output.
    it = _item("linux.do", "Cloudflare thing", "https://linux.do/t/123", FRESH,
               heat=40, signal="40 replies · 前沿快讯")
    it["final_score"] = 99          # must never surface
    it["grade"] = "A"
    it["score_breakdown"] = {"timing": 88}
    md = dg.render_community_pulse([it], cfg=_fixture_cfg())
    assert "https://linux.do/t/123" in md          # link present
    assert "40 replies · 前沿快讯" in md            # one-line why present
    assert "99" not in md                           # NO score number
    assert "grade" not in md.lower() and "final_score" not in md
    assert "dims:" not in md and "deep-dive" not in md and "深挖" not in md


def test_unattributable_item_skipped():
    # No url AND no title -> no stable key -> skipped, not rendered as a bare bullet.
    md = dg.render_community_pulse([{"source": "v2ex", "ts": FRESH, "signal": "x"}],
                                   cfg=_fixture_cfg())
    assert md == ""


# --------------------------------------------------------------------------- cap

def test_cap_enforced():
    cfg = {"community_pulse": {"enabled": True, "max_per_day": 3}, "scoring": {}}
    items = [_item("v2ex", f"topic {i}", f"https://v2ex.com/t/{i}", FRESH, heat=i)
             for i in range(10)]
    md = dg.render_community_pulse(items, cfg=cfg)
    # each rendered item is a top-level bullet beginning with "- **"
    assert md.count("- **") == 3


def test_cap_zero_suppresses():
    cfg = {"community_pulse": {"enabled": True, "max_per_day": 0}, "scoring": {}}
    md = dg.render_community_pulse([_item("v2ex", "t", "https://v2ex.com/t/1", FRESH)], cfg=cfg)
    assert md == ""


# --------------------------------------------------------------------------- dedup

def test_within_batch_dedup_by_url():
    # Same canonical URL (query/fragment differ) collapses to ONE bullet.
    a = _item("v2ex", "topic A", "https://v2ex.com/t/777?p=1", FRESH, heat=5)
    b = _item("v2ex", "topic A dup", "https://v2ex.com/t/777#reply", MID, heat=5)
    md = dg.render_community_pulse([a, b], cfg=_fixture_cfg())
    assert md.count("- **") == 1


def test_cross_day_dedup_via_seen_keys():
    it = _item("linux.do", "yesterday rumor", "https://linux.do/t/55", FRESH, heat=9)
    key = dg._pulse_key(it)
    md = dg.render_community_pulse([it], cfg=_fixture_cfg(), seen_keys={key})
    assert md == ""          # already surfaced on a prior day -> no re-bubble


# --------------------------------------------------------------------------- ranking

def test_ranked_by_freshness_and_heat():
    fresh_hot = _item("v2ex", "FRESH-HOT", "https://v2ex.com/t/1", FRESH, heat=80)
    stale_cold = _item("v2ex", "STALE-COLD", "https://v2ex.com/t/2", STALE, heat=1)
    md = dg.render_community_pulse([stale_cold, fresh_hot], cfg=_fixture_cfg())
    assert md.index("FRESH-HOT") < md.index("STALE-COLD")


# --------------------------------------------------------------------------- build_markdown wiring

def _card(title, score):
    return {"title": title, "final_score": score, "grade": "B", "track": "ai-agents",
            "machine_type": ["tool-saas"], "independent_source_count": 2,
            "score_breakdown": {"timing": 70}, "evidence": [{"source": "hn", "url": "u"}]}


def test_build_markdown_pulse_after_cards():
    cards = [_card("Scored Opportunity", 78)]
    pulse = [_item("v2ex", "Rumor Topic", "https://v2ex.com/t/1", FRESH, heat=10,
                   signal="10 replies · geek")]
    md = dg.build_markdown(cards, {"candidates": 5}, "2026-06-25", pulse=pulse, cfg=_fixture_cfg())
    assert "## 社区脉搏" in md
    assert "Rumor Topic" in md
    # the pulse section is rendered strictly AFTER the opportunity card
    assert md.index("Scored Opportunity") < md.index("## 社区脉搏")


def test_build_markdown_pulse_on_empty_card_day():
    pulse = [_item("linux.do", "Rumor Only", "https://linux.do/t/1", FRESH, heat=3)]
    md = dg.build_markdown([], {"candidates": 1}, "2026-06-25", pulse=pulse, cfg=_fixture_cfg())
    assert "今日无合格机会" in md          # honest empty-card line still present
    assert "## 社区脉搏" in md and "Rumor Only" in md   # ...and the rumor still surfaces


def test_build_markdown_no_pulse_is_backward_compatible():
    cards = [_card("Only Card", 80)]
    md = dg.build_markdown(cards, {"candidates": 1}, "2026-06-25")
    assert "## 社区脉搏" not in md          # no pulse arg -> unchanged output
