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


# --------------------------------------------------------------------------- HARDEN: injection safety (§10)

def test_untrusted_title_newline_cannot_inject_a_heading():
    # An embedded newline + markdown in a collected (untrusted) title must NOT open a new block — a
    # fabricated top-level heading / a fake scored-card section — inside the pushed digest. It is
    # DATA, flattened to a single inline span on the bullet.
    it = _item("linux.do", "topic\n## A 99 Buy this now", "https://linux.do/t/1", FRESH, heat=5)
    md = dg.render_community_pulse([it], cfg=_fixture_cfg())
    assert "\n## A 99" not in md                    # the injected heading never reaches column 0
    # the ONLY line that starts a heading is the section's own header (the `## ` in the title is now
    # inline text on the bullet, structurally inert)
    assert [ln for ln in md.split("\n") if ln.startswith("## ")] == ["## 社区脉搏"]
    assert "topic ## A 99 Buy this now" in md        # the title survives inline on the bullet


def test_untrusted_signal_newline_is_flattened():
    # The one-line "why" is built from the collector's `signal`, which embeds an untrusted node/
    # category label — it too must be whitespace-collapsed so it cannot inject a block.
    it = _item("v2ex", "t", "https://v2ex.com/t/2", FRESH, signal="5 replies\n## FAKE HEADING")
    md = dg.render_community_pulse([it], cfg=_fixture_cfg())
    assert "\n## FAKE HEADING" not in md
    assert [ln for ln in md.split("\n") if ln.startswith("## ")] == ["## 社区脉搏"]


def test_backtick_and_pipe_in_fields_are_neutralized():
    # A backtick in the source label would break the `src` code-span; a pipe could be read as a table
    # delimiter. Both are neutralized so the bullet structure stays intact.
    it = _item("v2ex`x", "ti|tle", "https://v2ex.com/t/3", FRESH, heat=1)
    md = dg.render_community_pulse([it], cfg=_fixture_cfg())
    assert "`v2ex'x`" in md and "ti/tle" in md
    assert "`" not in md.replace("`v2ex'x`", "")     # no stray/unbalanced backtick remains


# --------------------------------------------------------------------------- HARDEN: cross-day dedup wiring (§7)

def test_build_markdown_forwards_seen_keys():
    # build_markdown must forward seen_keys to the renderer (previously it never did, so the cross-day
    # dedup was production dead-code and a single-source rumor re-bubbled every day).
    it = _item("v2ex", "yesterday rumor", "https://v2ex.com/t/55", FRESH, heat=9)
    k = dg._pulse_key(it)
    md = dg.build_markdown([], {"candidates": 1}, "2026-06-25", pulse=[it],
                           cfg=_fixture_cfg(), seen_keys={k})
    assert "## 社区脉搏" not in md          # a cross-day-seen rumor is suppressed in the digest


def test_pulse_seen_merge_and_active_window_are_bounded():
    from lib import parse_ts
    now = parse_ts("2026-06-25T12:00:00Z")
    it = _item("linux.do", "rumor", "https://linux.do/t/1", FRESH, heat=3)
    k = dg._pulse_key(it)
    m = dg.merge_pulse_seen({}, [it], now, _fixture_cfg())
    assert k in m                                              # this run's key is recorded...
    assert k in dg.active_pulse_seen_keys(m, now, _fixture_cfg())   # ...and is in-window
    aged = {k: "2026-05-01T00:00:00Z"}                        # far outside the 14d retention
    assert dg.active_pulse_seen_keys(aged, now, _fixture_cfg()) == set()   # aged out of the dedup set
    assert dg.merge_pulse_seen(aged, [], now, _fixture_cfg()) == {}        # ...and dropped on write


def _community_single_origin_cand():
    # single-origin community candidate -> run.process routes it to Track 2 (community pulse)
    return {
        "title": "MCP agent framework rumor",
        "summary": "open source llm agent tooling for builders",
        "evidence": [{"source": "v2ex", "origin": "v2ex", "origin_source": "v2ex",
                      "url": "https://v2ex.com/t/4242", "ts": "2026-06-25T11:00:00Z",
                      "signal": "42 replies", "heat": 42}],
        "score_breakdown": {"track_fit": 85, "timing": 95, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


class _FakeLedger:
    """Minimal ledger exposing just the seams run.process touches on a dry run, plus the pulse-seen
    singleton (get/set) the cross-day dedup wiring reads."""
    def __init__(self, pulse_seen=None):
        self._pulse_seen = dict(pulse_seen or {})
        self.saved = None

    def list_active(self):
        return []

    def upsert(self, cand, ext):
        pass

    def add_watermark(self, *a):
        pass

    def get_pulse_seen(self):
        return dict(self._pulse_seen)

    def set_pulse_seen(self, m):
        self.saved = dict(m)


def test_process_suppresses_cross_day_seen_rumor():
    import run as runner
    from lib import load_config, parse_ts
    cfg = load_config()
    key = dg._pulse_key({"url": "https://v2ex.com/t/4242"})
    # day-2: the ledger already remembers this rumor was shown yesterday (within the retention window)
    ledger = _FakeLedger(pulse_seen={key: "2026-06-24T12:00:00Z"})
    res = runner.process([_community_single_origin_cand()], cfg, ledger=ledger, dry_run=True)
    assert len(res["community_pulse"]) == 1                     # still routed to Track 2...
    assert "## 社区脉搏" not in res["digest_markdown"]           # ...but process fed the prior key -> suppressed


def test_process_renders_a_fresh_unseen_rumor():
    import run as runner
    from lib import load_config
    res = runner.process([_community_single_origin_cand()], load_config(),
                         ledger=_FakeLedger(pulse_seen={}), dry_run=True)
    assert "## 社区脉搏" in res["digest_markdown"]               # an unseen rumor DOES render
