"""Round-2 audit regression guards (deterministic, stdlib only).

Each test here pins a fix from the round-2 security audit and FAILS on the pre-fix code:
  * MEDIUM#1 — watermark only advances after every ledger/digest write succeeds (Hard-rule #4).
  * MEDIUM#2 — the >=2-ORIGIN red line collapses exact-URL transload (same wire, many labels).
  * LOW#1   — user watchlist.json can only TIGHTEN guardrails, never loosen them.
"""
import json

import pytest

import run as runner
from run import count_independent_sources
from lib import load_config, DEFAULT_CONFIG


def _cand(title, sources, urls=None, timing=95):
    urls = urls or {s: "http://x/" + s for s in sources}
    return {
        "title": title, "summary": "open source llm agent tooling", "track": "ai-agents",
        "entities": (title + " open source llm agent").lower().split(),
        "evidence": [{"source": s, "origin": s + ".com", "url": urls[s],
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in sources],
        "score_breakdown": {"track_fit": 85, "timing": timing, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


# =================================================================== MEDIUM#2 transload
def test_count_independent_sources_collapses_exact_url_transload():
    # three outlet labels but ALL backed by the SAME exact URL = one syndicated wire item
    ev = [{"source": a, "origin": a, "url": "http://wire/story-1"} for a in ("reuters", "ap", "yahoo")]
    assert count_independent_sources(ev) == 1
    # genuinely distinct outlets, each its own URL = three independent sources
    ev2 = [{"source": a, "origin": a, "url": "http://" + a + "/x"} for a in ("hn", "ph", "github")]
    assert count_independent_sources(ev2) == 3
    # missing URLs -> fall back to distinct origin labels (do not over-penalize)
    ev3 = [{"source": a, "origin": a} for a in ("hn", "ph")]
    assert count_independent_sources(ev3) == 2


def test_pipeline_transload_fails_two_source_red_line():
    """Five labels, one wire (same URL) must NOT clear the >=2-independent-source red line."""
    same = "http://wire.example/exact-story"
    cand = _cand("Big launch", ["reuters", "ap", "yahoo", "bing", "msn"],
                 urls={s: same for s in ("reuters", "ap", "yahoo", "bing", "msn")})
    res = runner.process([cand], load_config(), ledger=None, dry_run=True)
    assert res["below_sources"], "exact-URL transload masquerading as 5 sources must be a gap"
    assert res["pushed"] == []
    # contrast: same five labels with five distinct URLs survives the red line
    cand2 = _cand("Big launch", ["reuters", "ap", "yahoo", "bing", "msn"])
    res2 = runner.process([cand2], load_config(), ledger=None, dry_run=True)
    assert res2["built"] == 1 and not res2["below_sources"]


# =================================================================== MEDIUM#1 watermark atomicity
class _FakeLedger:
    def __init__(self, fail_upsert=False):
        self.fail_upsert = fail_upsert
        self.watermark_calls = 0
        self.upserts = 0

    def list_active(self, limit=500):
        return []

    def upsert(self, candidate, ext, title=None, state="pending"):
        if self.fail_upsert:
            raise RuntimeError("simulated ledger write failure")
        self.upserts += 1
        return {"item": {"id": "x"}}

    def add_watermark(self, last_run_at):
        self.watermark_calls += 1

    def _run(self, verb, args):  # used by digest.register_digest_item
        return {}


def test_watermark_held_when_a_ledger_write_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")  # suppress real Discord delivery
    led = _FakeLedger(fail_upsert=True)
    cand = _cand("MCP agent framework launch", ["hackernews", "product-hunt"])
    res = runner.process([cand], load_config(), ledger=led, dry_run=False,
                         archive_dir=str(tmp_path))
    assert res["watermark_advanced"] is False, "watermark must NOT advance after a swallowed failure"
    assert res["errors"], "the failed side-effect must be surfaced, not silently swallowed"
    assert led.watermark_calls == 0, "add_watermark must not be called on a partial-failure run"


def test_watermark_advances_on_clean_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    led = _FakeLedger(fail_upsert=False)
    cand = _cand("MCP agent framework launch", ["hackernews", "product-hunt"])
    res = runner.process([cand], load_config(), ledger=led, dry_run=False,
                         archive_dir=str(tmp_path))
    assert res["watermark_advanced"] is True
    assert res["errors"] == []
    assert led.watermark_calls == 1


# =================================================================== LOW#1 guardrail floor
def test_user_config_cannot_loosen_safety_rails(tmp_path):
    """A malicious/careless watchlist.json that tries to drop the rails must be clamped to the
    built-in floor — guardrails only tighten, never loosen."""
    bad = {"scoring": {"min_independent_sources": 0, "min_score_to_push": 10,
                       "min_score_to_archive": 5},
           "exclude": []}  # tries to wipe the built-in exclude list
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    cfg = load_config(explicit_path=str(p))
    d = DEFAULT_CONFIG["scoring"]
    assert cfg["scoring"]["min_independent_sources"] == d["min_independent_sources"]  # floored to 2
    assert cfg["scoring"]["min_score_to_push"] >= d["min_score_to_push"]
    assert cfg["scoring"]["min_score_to_archive"] >= d["min_score_to_archive"]
    # built-in exclusions survive a user attempt to blank the list
    assert set(DEFAULT_CONFIG["exclude"]).issubset(set(cfg["exclude"]))


def test_user_config_may_still_tighten_and_extend(tmp_path):
    good = {"scoring": {"min_independent_sources": 3, "min_score_to_push": 85},
            "exclude": ["my-extra-bad-topic"]}
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps(good), encoding="utf-8")
    cfg = load_config(explicit_path=str(p))
    assert cfg["scoring"]["min_independent_sources"] == 3      # stricter is honored
    assert cfg["scoring"]["min_score_to_push"] == 85
    assert "my-extra-bad-topic" in cfg["exclude"]              # user additions kept (union)
    assert set(DEFAULT_CONFIG["exclude"]).issubset(set(cfg["exclude"]))
