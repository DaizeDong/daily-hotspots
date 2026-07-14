#!/usr/bin/env python3
"""Harden round 2 regression guards — the source-coverage slice (audit HARDEN round 2).

One (or a few) test(s) per verified finding; each FAILS on the pre-fix code. Deterministic: stdlib
only, no network, no live MCP, no live config (every call passes an explicit cfg / roster / tmp
archive-dir, and the one non-dry pipeline test neutralizes delivery via DAILY_HOTSPOTS_DRYRUN, so the
companion repo and Discord are never touched). Clock frozen by conftest (DAILY_HOTSPOTS_NOW =
2026-06-25T12:00:00Z).

Findings covered here:
  1. Cross-day pulse dedup must stamp ONLY the rumors the digest ACTUALLY rendered (the capped,
     deduped subset), never the full pre-cap candidate list — the community_pulse.max_per_day cap
     DEFERS overflow rumors, it must not bury them as "seen" forever without ever showing them (§7).
     Fix: digest.select_rendered_pulse is the shared selection; run.process stamps from it.
  2. The Track-1 opportunity-card renderer must neutralize untrusted NEW-source fields (card title,
     evidence source/url/signal, machine_type) with the same _inline block-injection guard the
     Track-2 pulse path uses — a spoofed field with an embedded newline + "## ..." must not open a
     fabricated heading at column 0 in the pushed digest once its entity clears the >=2-source gate (§10).
  3. yield.decide_suggest_filters (the one §9-bearing decision that had NO positive test) is pinned:
     propose-only / never-auto-apply, and unknown-yield exclusion (no-fabrication), plus config-driven
     thresholds — so a future refactor that auto-applied a filter or dropped the unknown guard goes red.
"""
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import digest as dg          # noqa: E402
import roster as R           # noqa: E402
from lib import parse_ts     # noqa: E402

Y = importlib.import_module("yield")   # 'yield' is a keyword -> import by name

NOW = parse_ts("2026-06-25T12:00:00Z")
YCFG = Y.yield_cfg({})                  # module defaults (noisy_pull_min 10, noisy_yield_max 0.1)
FRESH = "2026-06-25T11:00:00Z"


# ============================================================ Finding 1: cap DEFERS, never DROPS (§7)
_CAP_CFG = {"community_pulse": {"enabled": True, "max_per_day": 3}, "scoring": {}}


def _pitem(i, heat, ts=FRESH):
    return {"source": "v2ex", "origin": "v2ex", "origin_source": "v2ex",
            "title": f"rumor {i}", "url": f"https://v2ex.com/t/{5000 + i}", "ts": ts,
            "heat": heat, "signal": f"{heat} replies"}


def test_select_rendered_pulse_caps_and_reports_only_shown():
    items = [_pitem(i, heat=h) for i, h in enumerate([10, 20, 30, 40, 50, 60])]
    chosen = dg.select_rendered_pulse(items, cfg=_CAP_CFG, seen_keys=set())
    assert len(chosen) == 3                                    # cap enforced
    # the 3 highest-heat rumors are the ones rendered (deterministic rank); the rest are overflow
    assert {it["title"] for it in chosen} == {"rumor 5", "rumor 4", "rumor 3"}


def test_merge_pulse_seen_stamps_only_rendered_not_full_candidate_list():
    # THE finding-1 invariant: the daily cap DEFERS overflow rumors (they re-rank next run); stamping
    # only the shown subset leaves overflow keys UNSEEN so they can surface later. Stamping the full
    # pre-cap list (the pre-fix bug) would mark un-shown rumors "seen" and suppress them forever.
    items = [_pitem(i, heat=h) for i, h in enumerate([10, 20, 30, 40, 50, 60])]
    chosen = dg.select_rendered_pulse(items, cfg=_CAP_CFG, seen_keys=set())
    seen = dg.merge_pulse_seen({}, chosen, NOW, _CAP_CFG)
    chosen_keys = {dg._pulse_key(it) for it in chosen}
    all_keys = {dg._pulse_key(it) for it in items}
    overflow = all_keys - chosen_keys
    assert set(seen) == chosen_keys                            # ONLY the rendered rumors recorded
    assert overflow and overflow.isdisjoint(set(seen))         # overflow stays free to surface later
    # document the buried-forever bug this guards against: a naive stamp of the FULL candidate list
    naive_bug = dg.merge_pulse_seen({}, items, NOW, _CAP_CFG)
    assert set(naive_bug) == all_keys                          # ...would have recorded EVERY key...
    assert set(seen) != set(naive_bug)                         # ...and the fix diverges from it


class _RecordingLedger:
    """Minimal ledger exposing the seams run.process touches on a NON-dry run, capturing the exact
    pulse-seen map written back so the cross-day dedup wiring can be asserted."""
    def __init__(self, pulse_seen=None):
        self._pulse_seen = dict(pulse_seen or {})
        self.saved_pulse_seen = None

    def list_active(self):
        return []

    def upsert(self, cand, ext):
        pass

    def add_watermark(self, *a):
        pass

    def get_pulse_seen(self):
        return dict(self._pulse_seen)

    def set_pulse_seen(self, m):
        self.saved_pulse_seen = dict(m)

    def _run(self, action, args):
        return {}


def _community_cand(idx, heat):
    # single-origin, community-sourced, fresh, track-relevant -> run.process routes it to Track 2
    return {
        "title": f"MCP agent rumor {idx}",
        "summary": "open source llm agent tooling for builders",
        "evidence": [{"source": "v2ex", "origin": "v2ex", "origin_source": "v2ex",
                      "url": f"https://v2ex.com/t/{4000 + idx}", "ts": FRESH,
                      "signal": f"{heat} replies", "heat": heat}],
        "score_breakdown": {"track_fit": 85, "timing": 95, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


def test_process_stamps_only_rendered_pulse_not_capped_overflow(tmp_path, monkeypatch):
    # End-to-end run.process regression (the finding lives in run.py's merge call). Five single-origin
    # community rumors, cap 3: the persisted pulse-seen map must hold ONLY the 3 rendered rumors, not
    # all 5 — else the 2 overflow rumors are suppressed forever without ever being displayed.
    import run as runner
    from lib import load_config
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")           # neutralize delivery only (no network)
    cfg = load_config()
    cfg.setdefault("community_pulse", {})
    cfg["community_pulse"]["enabled"] = True
    cfg["community_pulse"]["max_per_day"] = 3
    cands = [_community_cand(i, heat=h) for i, h in enumerate([10, 20, 30, 40, 50])]
    ledger = _RecordingLedger(pulse_seen={})
    res = runner.process(cands, cfg, ledger=ledger, dry_run=False, archive_dir=str(tmp_path))

    assert not res["errors"]                                   # clean run -> the write-back ran
    assert len(res["community_pulse"]) == 5                    # all 5 rumors ROUTED to Track 2...
    saved = ledger.saved_pulse_seen
    assert saved is not None and len(saved) == 3               # ...but ONLY the capped 3 were stamped
    # the stamped keys are exactly the rendered subset (shown == recorded)
    rendered = dg.select_rendered_pulse(res["community_pulse"], cfg=cfg, seen_keys=set())
    assert set(saved) == {dg._pulse_key(it) for it in rendered}
    for i in (2, 3, 4):                                        # heat 30/40/50 -> rendered
        assert f"https://v2ex.com/t/{4000 + i}" in res["digest_markdown"]
    for i in (0, 1):                                           # heat 10/20 -> overflow
        assert f"https://v2ex.com/t/{4000 + i}" not in res["digest_markdown"]
        assert dg._pulse_key({"url": f"https://v2ex.com/t/{4000 + i}"}) not in saved


# ============================================================ Finding 2: card renderer injection (§10)
def _two_source_card(title, evidence):
    return {"grade": "A", "final_score": 82, "title": title, "track": "dev-tools",
            "machine_type": ["tool-saas"], "independent_source_count": 2,
            "score_breakdown": {"timing": 80}, "evidence": evidence}


def _card_headings(md):
    return [ln for ln in md.split("\n") if ln.startswith("## ")]


def test_card_evidence_signal_newline_cannot_inject_heading():
    # A spoofed V2EX node name in the evidence `signal` (the endpoint is keyless / MITM-able) carries
    # an embedded newline + markdown; once the entity clears the >=2-source gate the card renderer must
    # NOT let it open a fabricated heading at column 0 in the pushed digest. Flattened to inline DATA.
    card = _two_source_card("Corroborated agent tool", [
        {"source": "v2ex", "url": "https://v2ex.com/t/1", "signal": "5 replies · geek\n## Buy $XYZ now"},
        {"source": "hackernews", "url": "https://news.ycombinator.com/item?id=1", "signal": "hn thread"}])
    md = dg.build_markdown([card], {"candidates": 2}, "2026-06-25")
    assert "\n## Buy $XYZ now" not in md                       # injected heading never reaches column 0
    assert _card_headings(md) == ["## A 82 — Corroborated agent tool"]   # the ONLY heading is the card's
    assert "5 replies · geek ## Buy $XYZ now" in md            # the signal survives inline on the bullet


def test_card_title_newline_cannot_inject_heading():
    card = _two_source_card("legit\n## FAKE A 99 buy now", [
        {"source": "hn", "url": "u1"}, {"source": "rss", "url": "u2"}])
    md = dg.build_markdown([card], {"candidates": 2}, "2026-06-25")
    assert "\n## FAKE A 99 buy now" not in md
    assert _card_headings(md) == ["## A 82 — legit ## FAKE A 99 buy now"]   # injected ## is now inline


def test_card_untrusted_fields_neutralize_backtick_and_pipe():
    # A backtick in an evidence source breaks the `track` code-span shape; a pipe reads as a table
    # delimiter. Both are neutralized in the card renderer, matching the pulse path.
    card = _two_source_card("ti|tle", [
        {"source": "v2ex`x", "url": "https://v2ex.com/t/1", "signal": "5 replies | rm -rf"},
        {"source": "hn", "url": "u2"}])
    md = dg.build_markdown([card], {"candidates": 2}, "2026-06-25")
    assert "ti/tle" in md                                      # pipe in title -> '/'
    assert "v2ex'x" in md                                      # backtick in source -> '
    assert "5 replies / rm -rf" in md                         # pipe in signal -> '/'
    assert "ti|tle" not in md and "v2ex`x" not in md          # raw metacharacters gone


# ============================================================ Finding 3: suggest-filter §9 guardrails
def _noisy_roster():
    return {"schema_version": 1, "entries": [
        {"handle": "noisy", "track": "fintech-crypto", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}


def _yields(pulls, contributions, y):
    """A hand-built compute_yield-shaped stats dict for the single 'noisy' handle."""
    return {Y.okey(Y.KIND_HANDLE, "noisy"): {
        "kind": Y.KIND_HANDLE, "name": "noisy", "contributions": contributions,
        "pushed_contributions": 0, "pre_viral": 0, "pulls": pulls, "yield": y}}


def test_suggest_filter_targets_noisy_high_pull_low_yield_handle():
    sf = Y.decide_suggest_filters(_noisy_roster(), _yields(20, 1, 0.05), YCFG)
    assert len(sf) == 1
    d = sf[0]
    assert d["handle"] == "noisy" and d["track"] == "fintech-crypto"
    assert d["pulls"] == 20 and d["contributions"] == 1 and d["yield"] == 0.05


def test_suggest_filter_excludes_unknown_yield_handle():
    # pulls==0 -> yield unknown (None); a numerator without a denominator is not a real ratio, so it is
    # NEVER a suggest target -- the same §9 no-fabrication rail the prune path enforces, now pinned for
    # the suggest-filter path too (previously untested -> silently regressable).
    assert Y.decide_suggest_filters(_noisy_roster(), _yields(0, 2, None), YCFG) == []


def test_suggest_filter_requires_a_contribution_not_a_prune_target():
    # 0 contributions is a DEAD handle (a prune target), not a NOISY-but-productive one; a topic_filter
    # only makes sense to preserve real signal, so a zero-contribution handle is excluded here.
    assert Y.decide_suggest_filters(_noisy_roster(), _yields(20, 0, 0.0), YCFG) == []


def test_suggest_filter_skips_already_filtered_and_disabled_handles():
    filtered = {"schema_version": 1, "entries": [
        {"handle": "noisy", "track": "fintech-crypto", "tier": 1, "enabled": True,
         "topic_filter": "(AI OR coding)", "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    assert Y.decide_suggest_filters(filtered, _yields(20, 1, 0.05), YCFG) == []     # already filtered
    disabled = {"schema_version": 1, "entries": [
        {"handle": "noisy", "track": "fintech-crypto", "tier": 1, "enabled": False,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}
    assert Y.decide_suggest_filters(disabled, _yields(20, 1, 0.05), YCFG) == []     # disabled entry


def test_suggest_filter_thresholds_are_config_driven():
    roster, yields = _noisy_roster(), _yields(20, 1, 0.05)
    assert Y.decide_suggest_filters(roster, yields, YCFG)                            # default -> suggested
    # raise the "high pull" bar above this handle's 20 pulls -> no longer noisy enough -> dropped
    hi = Y.yield_cfg({"yield": {"noisy_pull_min": 50}})
    assert Y.decide_suggest_filters(roster, yields, hi) == []
    # tighten the "low yield" bar below this handle's 0.05 -> no longer low-yield -> dropped
    lo = Y.yield_cfg({"yield": {"noisy_yield_max": 0.01}})
    assert Y.decide_suggest_filters(roster, yields, lo) == []


def _noisy_records():
    return [{"opportunity_id": "op-noisy", "first_seen": "2026-06-24T09:00:00Z",
             "last_seen": "2026-06-24T09:00:00Z", "pushed": True, "track": "fintech-crypto",
             "evidence": [{"source": "twitter", "origin": "twitter",
                           "url": "https://x.com/noisy/status/1", "signal": "a post",
                           "ts": "2026-06-24T08:00:00Z", "origin_handle": "noisy", "faves": 50}]}]


def _noisy_pulls():
    # 12 pull events across 06-13..06-24 -> pulls 12 (>= noisy_pull_min 10), history 12d (not cold),
    # 1 contribution -> yield 1/12 ~= 0.083 (< noisy_yield_max 0.1) -> a suggest-filter target.
    return [{"run_id": f"2026-06-{d:02d}T08:00:00Z", "ts": f"2026-06-{d:02d}T08:00:00Z",
             "handle": "noisy", "pulled": 3} for d in range(13, 25)]


def test_suggest_filter_never_auto_applies_to_roster():
    # §9 propose-only rail: run_yield(apply=True) may SUGGEST a topic_filter for a noisy handle, but
    # tightening collection is add-like -> it must NEVER be written into the roster (only prune, a pure
    # reversible subtraction, is ever auto-applied). Previously there was no test pinning this.
    roster = _noisy_roster()
    rep = Y.run_yield(roster, _noisy_records(), _noisy_pulls(), cfg={}, now=NOW, apply=True)
    assert rep["cold_start"] is False                          # real history -> engine is live
    assert any(d["handle"] == "noisy" for d in rep["suggest_filters"])   # a filter WAS suggested...
    e = R.find_entry(roster, "noisy")
    assert not e.get("topic_filter")                           # ...but NEVER written into the roster
    assert e["enabled"] is True                                # and it contributes -> not pruned either
    # the suggestion surfaces in the human-gated review queue under the propose-only heading
    md = Y.render_review_md(rep)
    sect = md.split("## suggested topic_filters")[1].split("\n## ")[0]
    assert "propose only" in sect and "noisy" in sect
