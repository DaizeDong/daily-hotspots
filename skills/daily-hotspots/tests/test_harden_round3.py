#!/usr/bin/env python3
"""Harden round 3 regression guards, the source-coverage slice (audit HARDEN round 3).

One (or a few) test(s) per verified finding; each FAILS on the pre-fix code and PASSES after the fix.
Deterministic: stdlib only, no network, no live MCP, no live config (every call passes an explicit
cfg / roster / report / tmp path). Clock frozen by conftest (DAILY_HOTSPOTS_NOW = 2026-06-25T12Z).

Findings covered here:
  1. §10 DOCTYPE/XXE guard was bypassable by a processing instruction that contains a bare '>' before
     the DOCTYPE, parse_rss must still refuse the feed (-> []).
  2. Auto-prune disabled the SINGLE-ORIGIN pre-viral handles the roster exists for (0 contributions /
     0 window pre_viral); the pulls-log `kept` count now spares a handle still surfacing signals.
  3. render_review_md wrote untrusted archive-derived fields into markdown tables WITHOUT the _inline
     neutralization the digest renderer uses, a '|' forged a column and a newline+'##' opened a
     fabricated heading. Now flattened per §10.
  4. Community-source keep/drop config given as a bare string was iterated char-by-char and blinded the
     lane; the runtime now coerces string->[string] and verify_config.validate_source_filters flags it.
  5. Auto-prune apply path never persisted the prune reason/stats onto the roster entry, so the durable
     un-prune queue lost the §8 justification after the applying run; run_yield now stamps entry.notes.
"""
import copy
import importlib
import sys
from pathlib import Path

import pytest

import run as RUN          # skills/.../scripts on sys.path via conftest
import roster as RT
from lib import parse_ts

Y = importlib.import_module("yield")   # 'yield' is a keyword -> import by name

# verify_config.py lives at <repo>/scripts (not the skill scripts dir).
ROOT_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))
import verify_config as vc  # noqa: E402

NOW = parse_ts("2026-06-25T12:00:00Z")
FIX_YIELD = Path(__file__).resolve().parent / "fixtures" / "yield"


def _fixture_records():
    return Y.load_opportunities(str(FIX_YIELD))


def _fixture_pulls():
    return Y.load_pulls(str(FIX_YIELD))


# ============================================================ Finding 1: DOCTYPE hidden behind a PI
def test_parse_rss_refuses_doctype_after_pi_containing_gt():
    # A processing instruction may legitimately carry a bare '>' before its '?>' terminator. The old
    # prolog matcher ('<\?[^>]*\?>') could not consume such a PI, so a DOCTYPE placed AFTER it slipped
    # the §10 guard and reached expat (internal-entity expansion, billion-laughs on expat < 2.4.0).
    bomb = ('<?xml version="1.0"?><?e a>b ?>'
            '<!DOCTYPE r [<!ENTITY x "BOOM">]>'
            '<rss><channel><item><title>&x;</title><link>u</link></item></channel></rss>')
    assert RUN.parse_rss(bomb) == []                       # refused, never handed to expat


def test_has_prolog_doctype_detects_doctype_after_pi_with_gt():
    # The helper the guard rests on: it now consumes a PI up to its FIRST '?>' (DOTALL), '>' inside and
    # all, so a trailing DOCTYPE is still seen.
    assert RUN._has_prolog_doctype('<?xml?><?pi a>b?><!DOCTYPE x>') is True
    assert RUN._has_prolog_doctype('<?xml?><?pi a>b?><rss/>') is False   # no DOCTYPE -> no false positive


def test_parse_rss_same_feed_without_doctype_still_parses():
    # Zero false positives: the identical prolog PI (with its '>') and NO DOCTYPE parses normally, so it
    # is the DOCTYPE, not the tricky PI, that the guard blocks.
    ok = ('<?xml version="1.0"?><?e a>b ?>'
          '<rss><channel><item><title>hi</title><link>u</link></item></channel></rss>')
    items = RUN.parse_rss(ok)
    assert len(items) == 1 and items[0]["title"] == "hi"


# ============================================================ Finding 2: kept guard spares pre-viral solo handle
def _solo_roster(handle):
    return {"schema_version": 1, "entries": [
        {"handle": handle, "track": "dev-tools", "tier": 1, "enabled": True,
         "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}


def test_kept_signals_spare_single_origin_pre_viral_handle():
    # The roster's raison d'être (§1) is catching SINGLE-ORIGIN pre-viral founder posts, which route to
    # the community-pulse lane (§7) and NEVER become a >=2-origin archived card, so contributions and
    # the window pre_viral guard are both 0/blind. With an EMPTY opportunities.jsonl the pre-fix engine
    # pruned exactly these handles. The pulls-log `kept` count (fresh, on-topic, above the low rostered
    # faves floor) is the replayable proof the handle is doing its job -> it must be spared.
    roster = _solo_roster("solofounder")
    pulls = [
        {"run_id": "r0", "ts": "2026-06-11T08:00:00Z", "handle": "solofounder", "pulled": 5, "kept": 2},
        {"run_id": "r1", "ts": "2026-06-13T08:00:00Z", "handle": "solofounder", "pulled": 5, "kept": 3},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "solofounder", "pulled": 6, "kept": 4},
    ]
    rep = Y.run_yield(roster, [], pulls, cfg={}, now=NOW, apply=True)   # empty opportunities.jsonl
    assert rep["cold_start"] is False                                  # 14d history -> pruning is live
    assert [d["handle"] for d in rep["prune"]] == []                   # spared by the kept guard
    assert RT.find_entry(roster, "solofounder")["enabled"] is True     # ...and left enabled


def test_zero_kept_pulled_handle_is_still_pruned_deadweight():
    # Control for the guard above: the SAME pulled-every-week shape but kept==0 every week (all
    # stale / off-topic / below-faves) is genuine deadweight and IS pruned, proving the surfaced-kept
    # signal (not some unrelated sparing) is what saved solofounder.
    roster = _solo_roster("deadzero")
    pulls = [
        {"run_id": "r0", "ts": "2026-06-11T08:00:00Z", "handle": "deadzero", "pulled": 5, "kept": 0},
        {"run_id": "r1", "ts": "2026-06-13T08:00:00Z", "handle": "deadzero", "pulled": 5, "kept": 0},
        {"run_id": "r2", "ts": "2026-06-24T08:00:00Z", "handle": "deadzero", "pulled": 6, "kept": 0},
    ]
    rep = Y.run_yield(roster, [], pulls, cfg={}, now=NOW)
    assert rep["cold_start"] is False
    assert [d["handle"] for d in rep["prune"]] == ["deadzero"]         # no kept signal -> deadweight


def test_kept_guard_does_not_spare_the_fixture_deadweight():
    # Regression anchor: the committed fixture's `deadweight` (pulls carry NO `kept` field) still prunes
    #, a missing kept is treated as 0 (never fabricated), so the guard changes nothing for it.
    roster = RT.load_roster(path=str(FIX_YIELD / "roster.json"))
    rep = Y.run_yield(roster, _fixture_records(), _fixture_pulls(), cfg={}, now=NOW)
    assert [d["handle"] for d in rep["prune"]] == ["deadweight"]


# ============================================================ Finding 3: review-md injection (§10)
def _headings(md):
    return [ln for ln in md.split("\n") if ln.startswith("## ")]


def test_render_review_md_neutralizes_propose_add_and_flag_injection():
    # Untrusted, archive-derived fields (propose-add handle/sample_url from collected evidence; the
    # identity-sweep detail from get_user_info userName) must not forge a table column ('|') or open a
    # fabricated top-level heading (newline + '##') in roster-review.md.
    report = {
        "generated_at": "2026-06-25T12:00:00Z", "window_days": 30, "history_days": 12,
        "cold_start": False,
        "propose_add": [{"handle": "pwn | ## OWNED", "count": 3, "tracks": ["dev | tools"],
                         "sample_url": "https://x.com/a\n## INJECTED-HEADING\n"}],
        "prune": [], "disabled": [], "suggest_filters": [],
        "flags": [{"handle": "victim", "kind": "drift", "current_handle": "evil",
                   "detail": "handle renamed to 'evil'\n## FORGED-HEADING\n | col"}],
    }
    md = Y.render_review_md(report)
    lines = md.split("\n")
    # no injected newline reaches column 0 to open a heading
    assert not any(ln.startswith("## INJECTED-HEADING") for ln in lines)
    assert not any(ln.startswith("## FORGED-HEADING") for ln in lines)
    assert "\n## INJECTED-HEADING" not in md and "\n## FORGED-HEADING" not in md
    # the ONLY '## ' headings are the renderer's own section headers
    assert _headings(md) == [
        "## propose-add (human-gated; NEVER auto-added)",
        "## recently pruned (reversible: enabled=false, un-prune here)",
        "## suggested topic_filters (high-pull / low-yield; propose only)",
        "## flagged accounts (monthly identity sweep; human-resolved, never auto-removed)",
    ]
    # the pipe in the handle no longer forges a column (neutralized to '/'), content survives inline
    assert "pwn | ## OWNED" not in md and "pwn / ## OWNED" in md
    assert "dev | tools" not in md and "dev / tools" in md
    # rendering is still deterministic
    assert md == Y.render_review_md(report)


# ============================================================ Finding 4: keep/drop string not char-shredded
def test_collect_community_source_bare_string_keep_is_not_char_shredded():
    # "keep_nodes":"geek" (a plausible typo vs ["geek"]) must be treated as the single node "geek", NOT
    # iterated into {'g','e','k'} which whitelists none of the real nodes and drops every item.
    cfg = {"sources": {"v2ex": {"keep_nodes": "geek"}}}
    items = [{"title": "a", "url": "u1", "category": "geek", "heat": 3, "ts": "", "summary": ""},
             {"title": "b", "url": "u2", "category": "programmer", "heat": 1, "ts": "", "summary": ""}]
    out = RUN.collect_community_source("v2ex", items, cfg=cfg, last_run=None, now=NOW)
    assert len(out["signals"]) == 1                       # NOT 0 (the char-shredded pre-fix result)
    assert out["signals"][0]["category"] == "geek"


def test_keepdrop_set_coerces_string_and_degrades_nonlist():
    assert RUN._keepdrop_set("geek") == {"geek"}          # bare string -> single-element set
    assert RUN._keepdrop_set(["Geek", "Cloud"]) == {"geek", "cloud"}
    assert RUN._keepdrop_set(42) == set()                 # non-iterable misconfig -> empty (no whitelist)
    assert RUN._keepdrop_set(None) == set()


def test_validate_source_filters_flags_bare_string_keep_list():
    ok, errs = vc.validate_source_filters({"v2ex": {"keep_nodes": "geek"}})
    assert ok is False and any("keep_nodes" in e for e in errs)
    ok2, errs2 = vc.validate_source_filters({"v2ex": {"drop_categories": "spam"}})
    assert ok2 is False and any("drop_categories" in e for e in errs2)
    # proper arrays / absent keys / no sources block are all fine
    assert vc.validate_source_filters({"v2ex": {"keep_nodes": ["geek"], "drop_nodes": ["jobs"]}})[0] is True
    assert vc.validate_source_filters({"v2ex": {}})[0] is True
    assert vc.validate_source_filters(None)[0] is True


def test_doctor_flags_bare_string_keep_list(tmp_path, monkeypatch, capsys):
    # End-to-end: the doctor must FAIL (not print READY) over a string-shredded lane, proves the
    # verify_config main() wiring is connected, not just the validator.
    import json
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "watchlist.json").write_text(
        json.dumps({"schema_version": 1, "sources": {"v2ex": {"keep_nodes": "geek"}}}),
        encoding="utf-8")
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    monkeypatch.setenv(vc.SKILLS_DIR_ENV, str(skills))
    monkeypatch.setattr(sys, "argv", ["verify_config.py", "--config-dir", str(cfg)])
    rc = vc.main()
    out = capsys.readouterr().out
    assert "[FAIL] community source keep/drop lists are arrays (spec 6)" in out
    assert rc == 1


# ============================================================ Finding 5: prune reason persisted (§8)
def test_apply_stamps_prune_reason_and_stats_onto_entry_notes():
    roster = RT.load_roster(path=str(FIX_YIELD / "roster.json"))
    rep = Y.run_yield(roster, _fixture_records(), _fixture_pulls(), cfg={}, now=NOW, apply=True)
    assert [d["handle"] for d in rep["prune"]] == ["deadweight"]
    e = RT.find_entry(roster, "deadweight")
    assert e["enabled"] is False
    assert e["notes"].startswith("auto-pruned 2026-06-25")            # dated stamp (§8 logged)
    assert "contributions" in e["notes"] and "pulls" in e["notes"]    # reason + stats persisted
    # the stamped notes is a valid entry (save would not reject it)
    ok, errs = RT.validate_roster(roster)
    assert ok, errs


def test_durable_unprune_queue_shows_stamped_reason_next_run():
    # THE finding-5 invariant: after --apply stamps entry.notes, a LATER weekly run (deadweight now
    # disabled -> decide_prune skips it, no fresh decision) must still surface the ORIGINAL reason+stats
    # in roster-review.md via the durable `disabled` list, NOT the generic placeholder the pre-fix path
    # degraded to.
    roster = RT.load_roster(path=str(FIX_YIELD / "roster.json"))
    Y.run_yield(roster, _fixture_records(), _fixture_pulls(), cfg={}, now=NOW, apply=True)   # week 1
    rep2 = Y.run_yield(roster, _fixture_records(), _fixture_pulls(), cfg={}, now=NOW)          # week 2
    assert [d["handle"] for d in rep2["prune"]] == []                 # already disabled -> no fresh prune
    dis = {e["handle"]: e for e in rep2["disabled"]}
    assert "deadweight" in dis
    assert dis["deadweight"]["reason"] and dis["deadweight"]["reason"].startswith("auto-pruned")
    md = Y.render_review_md(rep2)
    sect = md.split("## recently pruned")[1].split("\n## ")[0]
    assert "deadweight" in sect and "auto-pruned" in sect
    assert "previously pruned (enabled=false)" not in sect            # not the bare placeholder
