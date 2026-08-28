#!/usr/bin/env python3
"""Three confirmed dedup defects, each with a negative control that goes red on a regression.

1. RATCHETING BASELINE. `decide` compared today's score against `last_score`, which `build_ext`
   rewrote on every run including a SUPPRESS. A steadily strengthening opportunity therefore raced
   its own baseline: each day's delta was the one day step, never `resurface_score_jump`, so a card
   that climbed 60 to 90 over two weeks was suppressed on every one of those days. The baseline is
   now the score at the last run where the card was actually SURFACED (pushed or archived).

2. UNBOUNDED COMPARE SET. `lookback_days` and `fading_quiet_days` shipped in the config and were
   read by nothing, so `list_active` returned every row ever written (342 permanently active rows
   on the live ledger) and a card from months ago still suppressed today's. `partition_ledger` now
   applies both bounds and REPORTS what it dropped, so a clean window and an unchecked one differ.

3. SUBJECT GUARD COLLAPSE. `_subject_agree` compared the two LEADING entities, which fails in both
   directions on ordinary headlines: a generic leading token makes two distinct opportunities agree
   (false merge), and a framing prefix makes one story disagree with itself (false split, which is
   what the operator hit: eleven archived cards holding three same story pairs). The guard now reads
   the KIND of the earliest divergence, and a character n-gram rung carries CJK prose, where token
   Jaccard and token SimHash both collapse on whole clause tokens.

The CJK fixture below is SYNTHETIC: invented companies, invented numbers, invented clauses, shaped
like the failing run but reproducing none of its records.
"""
import itertools
import json
from datetime import timedelta

import pytest

from lib import canonical_key, extract_entities, iso, load_config, now_utc, simhash
import dedup as dd

CFG = load_config()
EXT = dd.EXT_PREFIX
NOW = now_utc()          # frozen by conftest at 2026-06-25T12:00:00Z


def _days_ago(n):
    return iso(NOW - timedelta(days=n))


def _cfg(**scoring):
    cfg = json.loads(json.dumps(CFG))
    cfg["scoring"].update(scoring)
    return cfg


# =========================================================== 1. ratcheting baseline

def _cand(title, summary, track, score, sources=("hackernews", "product-hunt"),
          stage="emerging"):
    ck = canonical_key(extract_entities(title + " " + summary), track)
    return {"canonical_key": ck, "title": title, "summary": summary, "track": track,
            "final_score": score, "lifecycle_stage": stage, "source_set": list(sources),
            "evidence": [{"source": s, "url": "http://x/" + s} for s in sources]}


def _row_from(cand, ext):
    return {"idempotency_key": cand["canonical_key"], "ext": ext}


def _one_day(cand, rows, cfg=CFG, surface=True):
    """One day in the pipeline's real order: match, decide, surface, then persist the ext."""
    matched = dd.match_existing(cand, rows, cfg)
    decision = dd.decide(cand, matched, cfg)
    card = dict(cand)
    if surface and decision["branch"] in (dd.NEW, dd.RESURFACE):
        card["pushed"] = True
    prior = dd._row_ext(matched) if matched else {}
    ext = dd.build_ext(card, {"ts": iso(now_utc()), "score": card["final_score"]}, prior, cfg)
    return decision, [_row_from(card, ext)]


TITLE = "MinerU PDF extraction open source tool"
SUMMARY = "parse pdf to markdown locally"


def _walk(scores, surface=True, cfg=CFG):
    """Run consecutive days at the given scores, return the branch taken each day."""
    rows, branches, exts = [], [], []
    for s in scores:
        decision, rows = _one_day(_cand(TITLE, SUMMARY, "ai-agents", s), rows, cfg, surface)
        branches.append(decision["branch"])
        exts.append(rows[0]["ext"])
    return branches, exts


def test_improving_opportunity_resurfaces_once_the_gain_accumulates():
    """THE DEFECT: +3 a day never trips a +15 jump when the baseline moves with the score."""
    jump = float(CFG["scoring"]["resurface_score_jump"])   # 15
    scores = [60 + 3 * i for i in range(9)]               # 60, 63, ... 84
    branches, exts = _walk(scores)
    assert branches[0] == dd.NEW
    assert dd.RESURFACE in branches, (
        "a steadily strengthening opportunity must resurface; branches=%s" % branches)
    day = branches.index(dd.RESURFACE, 1)
    assert scores[day] - scores[0] >= jump          # it resurfaced on accumulated gain
    assert scores[day - 1] - scores[0] < jump       # and not one day earlier
    assert exts[day][EXT + "baseline_score"] == scores[day]   # the anchor then moves up


def test_baseline_does_not_move_while_the_card_is_suppressed():
    """NEGATIVE CONTROL for the fix: if the anchor creeps along with the score, this goes red."""
    _, exts = _walk([60 + 3 * i for i in range(5)])
    assert [e[EXT + "baseline_score"] for e in exts] == [60, 60, 60, 60, 60]
    assert [e[EXT + "last_score"] for e in exts] == [60, 63, 66, 69, 72]  # observation still moves


def test_flat_opportunity_never_resurfaces():
    """NEGATIVE CONTROL: the fix must not become 'resurface eventually no matter what'."""
    scores = [70, 71, 69, 70, 72, 68, 71, 70, 69, 71, 70, 72, 69, 70, 71]
    branches, exts = _walk(scores)
    assert branches[0] == dd.NEW
    assert set(branches[1:]) == {dd.SUPPRESS}, branches
    assert exts[-1][EXT + "baseline_score"] == 70


def test_surfacing_every_day_keeps_the_baseline_at_yesterday():
    """NEGATIVE CONTROL: the anchor is 'last surfaced', so a card shown daily accumulates nothing.

    Same +3 a day climb as the first test, but every day is forced to surface. Nothing accumulates
    and nothing resurfaces, which is the correct behavior and proves the first test measures the
    baseline semantics rather than a blanket 'resurface after N days' rule.
    """
    branches, _ = _walk([60 + 3 * i for i in range(9)], surface=True, cfg=CFG)
    forced, exts = [], []
    rows = []
    for i, s in enumerate(60 + 3 * i for i in range(9)):
        cand = _cand(TITLE, SUMMARY, "ai-agents", s)
        matched = dd.match_existing(cand, rows, CFG)
        forced.append(dd.decide(cand, matched, CFG)["branch"])
        card = dict(cand, pushed=True)                    # surfaced unconditionally
        prior = dd._row_ext(matched) if matched else {}
        ext = dd.build_ext(card, {"ts": iso(now_utc()), "score": s}, prior, CFG)
        exts.append(ext)
        rows = [_row_from(card, ext)]
    assert forced[0] == dd.NEW and set(forced[1:]) == {dd.SUPPRESS}, forced
    assert [e[EXT + "baseline_score"] for e in exts] == [60 + 3 * i for i in range(9)]


def test_blocked_resurface_does_not_consume_the_baseline():
    """A card the gate blocked was never shown, so it must not reset the comparison anchor."""
    cand = _cand(TITLE, SUMMARY, "ai-agents", 60)
    ext1 = dd.build_ext(dict(cand, pushed=True), {"ts": iso(NOW), "score": 60}, {}, CFG)
    blocked = _cand(TITLE, SUMMARY, "ai-agents", 90)     # would resurface, but gate blocked it
    blocked["_branch"] = dd.RESURFACE                    # no pushed/archived flag
    ext2 = dd.build_ext(blocked, {"ts": iso(NOW), "score": 90}, ext1, CFG)
    assert ext2[EXT + "baseline_score"] == 60
    assert ext2[EXT + "last_score"] == 90


def test_archived_card_counts_as_surfaced():
    """NEGATIVE CONTROL for the line above: archiving IS a surfacing, so the anchor advances."""
    cand = _cand(TITLE, SUMMARY, "ai-agents", 60)
    ext1 = dd.build_ext(dict(cand, pushed=True), {"ts": iso(NOW), "score": 60}, {}, CFG)
    ext2 = dd.build_ext(dict(_cand(TITLE, SUMMARY, "ai-agents", 90), archived=True),
                        {"ts": iso(NOW), "score": 90}, ext1, CFG)
    assert ext2[EXT + "baseline_score"] == 90


def test_legacy_row_without_a_baseline_falls_back_and_says_so():
    """Rows written before this key existed must keep working, and report which anchor was used."""
    cand = _cand(TITLE, SUMMARY, "ai-agents", 80)
    legacy_ext = {EXT + "canonical_key": cand["canonical_key"],
                  EXT + "text": TITLE + " " + SUMMARY,
                  EXT + "simhash": simhash(TITLE + " " + SUMMARY),
                  EXT + "last_seen": _days_ago(1), EXT + "last_score": 60,
                  EXT + "lifecycle_stage": "emerging",
                  EXT + "source_set": ["hackernews", "product-hunt"]}
    matched = dd.match_existing(cand, [_row_from(cand, legacy_ext)], CFG)
    decision = dd.decide(cand, matched, CFG)
    assert decision["delta"]["baseline_from"] == "last_score"
    assert decision["delta"]["baseline_score"] == 60
    assert decision["branch"] == dd.RESURFACE            # +20 against the legacy anchor
    healed = dd.build_ext(cand, {"ts": iso(NOW), "score": 80}, legacy_ext, CFG)
    assert healed[EXT + "baseline_score"] == 60          # seeded, NOT advanced by a suppressed run


def test_delta_reports_both_the_anchor_and_the_one_day_step():
    cand = _cand(TITLE, SUMMARY, "ai-agents", 78)
    ext = dd.build_ext(dict(_cand(TITLE, SUMMARY, "ai-agents", 60), pushed=True),
                       {"ts": iso(NOW), "score": 60}, {}, CFG)
    ext[EXT + "last_score"] = 75                         # observed yesterday, still suppressed
    decision = dd.decide(cand, dd.match_existing(cand, [_row_from(cand, ext)], CFG), CFG)
    assert decision["delta"]["baseline_score"] == 60
    assert decision["delta"]["score_delta"] == 18        # against the anchor
    assert decision["delta"]["last_observed_score"] == 75
    assert decision["delta"]["observed_delta"] == 3      # the one day step, reporting only


# =========================================================== 2. compare window expiry

def _ledger_row(key, last_seen, stage="", score=70, text="MCP gateway local llm tools"):
    return {"idempotency_key": key,
            "ext": {EXT + "canonical_key": key, EXT + "text": text,
                    EXT + "simhash": simhash(text), EXT + "last_seen": last_seen,
                    EXT + "last_score": score, EXT + "lifecycle_stage": stage,
                    EXT + "source_set": ["hackernews", "product-hunt"]}}


class _StubLedger(dd.LedgerClient):
    """LedgerClient with the subprocess replaced, so list_active's window logic is testable."""

    def __init__(self, rows, cfg=CFG):
        self._rows = rows
        self.cmd, self.db_path, self.actor, self.cfg = [], None, dd.SOURCE, cfg
        self.last_window_report = None

    def _run(self, verb, args):
        assert verb == "list", verb
        return {"items": list(self._rows), "next_cursor": None}


def test_row_quiet_beyond_the_window_leaves_the_compare_set():
    """THE DEFECT: an opportunity from months ago still suppressed today's."""
    old = _ledger_row("k::ai-agents", _days_ago(90))
    part = dd.partition_ledger([old], CFG)
    assert part["active"] == []
    assert part["report"]["expired"][0]["key"] == "k::ai-agents"
    assert part["report"]["expired"][0]["quiet_days"] == 90.0


def test_fresh_row_is_kept():
    """NEGATIVE CONTROL: the window must not become 'drop everything'."""
    fresh = _ledger_row("k::ai-agents", _days_ago(1))
    part = dd.partition_ledger([fresh], CFG)
    assert part["active"] == [fresh]
    assert part["report"]["expired"] == []
    assert part["report"]["kept"] == 1 and part["report"]["checked"] == 1


def test_expired_row_no_longer_suppresses_todays_card():
    """End to end: same canonical key, ninety quiet days, must route NEW instead of SUPPRESS."""
    cand = _cand(TITLE, SUMMARY, "ai-agents", 70)
    stale = _row_from(cand, dd.build_ext(cand, {"ts": _days_ago(90), "score": 70}, {}, CFG))
    stale["ext"][EXT + "last_seen"] = _days_ago(90)
    assert dd.match_existing(cand, [stale], CFG) is not None      # raw ledger still matches it
    active = dd.partition_ledger([stale], CFG)["active"]
    assert dd.decide(cand, dd.match_existing(cand, active, CFG), CFG)["branch"] == dd.NEW


def test_recent_duplicate_still_suppresses():
    """NEGATIVE CONTROL for the line above: yesterday's identical card must still be suppressed."""
    cand = _cand(TITLE, SUMMARY, "ai-agents", 70)
    row = _row_from(cand, dd.build_ext(dict(cand, pushed=True),
                                       {"ts": _days_ago(1), "score": 70}, {}, CFG))
    row["ext"][EXT + "last_seen"] = _days_ago(1)
    active = dd.partition_ledger([row], CFG)["active"]
    assert dd.decide(cand, dd.match_existing(cand, active, CFG), CFG)["branch"] == dd.SUPPRESS


@pytest.mark.parametrize("knob,other", [("lookback_days", "fading_quiet_days"),
                                        ("fading_quiet_days", "lookback_days")])
def test_each_bound_is_individually_honored_and_named(knob, other):
    """Both knobs do real work, and the report names the one that fired."""
    cfg = _cfg(**{knob: 2, other: 365})
    row = _ledger_row("k::ai-agents", _days_ago(3))
    report = dd.partition_ledger([row], cfg)["report"]
    assert [e["bounds"] for e in report["expired"]] == [[knob]]
    assert report["expired"][0]["window_days"] == 2
    loose = dd.partition_ledger([row], _cfg(lookback_days=365, fading_quiet_days=365))
    assert loose["report"]["expired"] == [] and loose["active"] == [row]


def test_singleton_rows_never_expire():
    """The watermark carries no last_seen and must survive any window: losing it re-collects."""
    wm = {"idempotency_key": "daily-hotspots:watermark",
          "ext": {EXT + "last_run_at": _days_ago(400)}}
    part = dd.partition_ledger([wm, _ledger_row("k::ai-agents", _days_ago(90))], CFG)
    assert part["active"] == [wm]
    assert part["report"]["singletons"] == ["daily-hotspots:watermark"]
    lc = _StubLedger([wm])
    assert lc.get_watermark() == _days_ago(400)


@pytest.mark.parametrize("bad,reason", [(None, "missing"), ("", "missing"),
                                        ("not-a-timestamp", "unparsable"),
                                        ("2026-13-45T99:99:99Z", "unparsable")])
def test_undated_row_is_kept_and_reported_never_silently_dropped(bad, reason):
    """NEGATIVE CONTROL on honesty: an undatable row is kept AND named, not quietly discarded."""
    row = _ledger_row("k::ai-agents", bad)
    part = dd.partition_ledger([row], CFG)
    assert part["active"] == [row]
    assert part["report"]["undated"] == [{"key": "k::ai-agents",
                                          "last_seen": None if not bad else dd._cut(bad, 40),
                                          "reason": reason}]
    assert reason in dd.format_window_report(part["report"])


def test_clean_window_is_distinguishable_from_an_unchecked_one():
    """'checked nothing' and 'checked ten, dropped none' must not render the same."""
    clean = dd.partition_ledger([_ledger_row("k%d::ai-agents" % i, _days_ago(1))
                                 for i in range(10)], CFG)["report"]
    empty = dd.partition_ledger([], CFG)["report"]
    assert clean["checked"] == 10 and clean["kept"] == 10 and clean["expired"] == []
    assert empty["checked"] == 0 and empty["kept"] == 0
    assert dd.format_window_report(clean) != dd.format_window_report(empty)
    assert "checked=10 kept=10 expired=0" in dd.format_window_report(clean)


def test_bounds_provenance_separates_configured_from_builtin_default():
    configured = dd.partition_ledger([], CFG)["report"]
    assert configured["bounds_from"] == {"lookback_days": "config",
                                         "fading_quiet_days": "config"}
    bare = dd.partition_ledger([], {"scoring": {}})["report"]
    assert bare["bounds_from"] == {"lookback_days": "builtin-default",
                                   "fading_quiet_days": "builtin-default"}
    assert "builtin-default" in dd.format_window_report(bare)


def test_expiry_listing_declares_what_it_did_not_list():
    """NEGATIVE CONTROL on silent truncation: the line must say how many it left out."""
    rows = [_ledger_row("k%d::ai-agents" % i, _days_ago(90)) for i in range(12)]
    line = dd.format_window_report(dd.partition_ledger(rows, CFG)["report"])
    assert "expired=12" in line
    assert "+7 more expired (not listed)" in line


def test_list_active_applies_the_window_and_reports_it(capsys):
    rows = [_ledger_row("fresh::ai-agents", _days_ago(1)),
            _ledger_row("stale::ai-agents", _days_ago(90))]
    lc = _StubLedger(rows)
    got = lc.list_active()
    assert [dd._row_key(r) for r in got] == ["fresh::ai-agents"]
    assert lc.last_window_report["kept"] == 1
    err = capsys.readouterr().err
    assert "[dedup] compare window:" in err and "stale::ai-agents" in err


def test_list_active_window_false_returns_every_row():
    """NEGATIVE CONTROL: the raw view still exists, and it is opt in."""
    rows = [_ledger_row("fresh::ai-agents", _days_ago(1)),
            _ledger_row("stale::ai-agents", _days_ago(90))]
    lc = _StubLedger(rows)
    assert len(lc.list_active(window=False)) == 2
    assert lc.last_window_report is None


def test_window_uses_the_clients_config_not_a_hidden_default():
    lc = _StubLedger([_ledger_row("k::ai-agents", _days_ago(3))],
                     cfg=_cfg(lookback_days=2, fading_quiet_days=2))
    assert lc.list_active() == []
    lc2 = _StubLedger([_ledger_row("k::ai-agents", _days_ago(3))],
                      cfg=_cfg(lookback_days=30, fading_quiet_days=30))
    assert len(lc2.list_active()) == 1


# =========================================================== 3. subject guard + CJK merges

# Same distinct-subject families as tests/test_adversarial_dedup.py::_FALSE_MERGE, in the
# UNFAVORABLE orientation: the subject brand no longer leads, a generic descriptor does. The old
# guard compared leading entities, so every one of these agreed and false merged.
_FALSE_MERGE_GENERIC_LEAD = [
    ("fintech-crypto",
     "payments platform Stripe adds fraud detection for online merchants",
     "online merchants fraud detection platform payments stripe",
     "payments platform Adyen adds fraud detection for online merchants",
     "online merchants fraud detection platform payments adyen"),
    ("dev-tools",
     "deploy platform Vercel adds edge functions to the framework",
     "deploy edge platform framework vercel",
     "deploy platform Netlify adds edge functions to the framework",
     "deploy edge platform framework netlify"),
    ("ai-agents",
     "vector database Pinecone adds hybrid search infra",
     "vector database hybrid infra pinecone",
     "vector database Weaviate adds hybrid search infra",
     "vector database hybrid infra weaviate"),
    ("ai-agents",
     "model api framework from OpenAI ships new endpoints",
     "model api framework endpoints openai",
     "model api framework from Anthropic ships new endpoints",
     "model api framework endpoints anthropic"),
    ("saas-niche",
     "workspace database automation blocks land in Notion",
     "workspace database automation blocks notion",
     "workspace database automation blocks land in Coda",
     "workspace database automation blocks coda"),
    ("ai-agents",
     "发布 全新 智能 助手 平台 的 字节跳动", "智能 助手 平台 字节跳动",
     "发布 全新 智能 助手 平台 的 阿里巴巴", "智能 助手 平台 阿里巴巴"),
]

# The favorable orientation the existing suite already covers, kept here so both orientations of
# the same claim live together and a fix that only works subject-first cannot pass.
_FALSE_MERGE_SUBJECT_LEAD = [
    ("fintech-crypto",
     "Stripe payments platform adds fraud detection online merchants",
     "stripe online merchants fraud detection platform payments",
     "Adyen payments platform adds fraud detection online merchants",
     "adyen online merchants fraud detection platform payments"),
    ("ai-agents",
     "字节跳动 发布 全新 智能 助手 平台", "字节跳动 智能 助手 平台",
     "阿里巴巴 发布 全新 智能 助手 平台", "阿里巴巴 智能 助手 平台"),
]

_BOTH_ORIENTATIONS = ([("generic-lead",) + p for p in _FALSE_MERGE_GENERIC_LEAD] +
                      [("subject-lead",) + p for p in _FALSE_MERGE_SUBJECT_LEAD])


def _ext_row(title, summary, track, score=70, sources=("hackernews", "trend-pulse")):
    ck = canonical_key(extract_entities(title + " " + summary), track)
    text = title + " " + summary
    return {"idempotency_key": ck,
            "ext": {EXT + "canonical_key": ck, EXT + "simhash": simhash(text),
                    EXT + "text": text[:400], EXT + "last_seen": _days_ago(1),
                    EXT + "last_score": score, EXT + "lifecycle_stage": "",
                    EXT + "source_set": list(sources)}}


@pytest.mark.parametrize("orient,track,rt,rs,ct,cs", _BOTH_ORIENTATIONS)
def test_distinct_subjects_do_not_merge_in_either_orientation(orient, track, rt, rs, ct, cs):
    row = _ext_row(rt, rs, track)
    cand = _cand(ct, cs, track, 72, sources=("hackernews", "trend-pulse"))
    assert dd.match_existing(cand, [row], CFG) is None
    back = _ext_row(ct, cs, track)
    fwd = _cand(rt, rs, track, 72, sources=("hackernews", "trend-pulse"))
    assert dd.match_existing(fwd, [back], CFG) is None      # and symmetrically


@pytest.mark.parametrize("orient,track,rt,rs,ct,cs", _BOTH_ORIENTATIONS)
def test_the_subject_guard_is_what_stops_those_merges(monkeypatch, orient, track,
                                                      rt, rs, ct, cs):
    """NEGATIVE CONTROL against a vacuous suite: with the guard neutered every pair DOES merge,
    so the assertions above are testing the guard and not some unrelated threshold."""
    monkeypatch.setattr(dd, "_subject_agree", lambda a, b: True)
    row = _ext_row(rt, rs, track)
    cand = _cand(ct, cs, track, 72, sources=("hackernews", "trend-pulse"))
    assert dd.match_existing(cand, [row], CFG) is not None


def test_framing_prefix_does_not_split_one_story():
    """The false SPLIT half of the same defect: a '传闻落地：' style prefix is framing, not a subject."""
    assert dd._subject_agree("传闻落地：AcmeChip 四亿美元买下 ModelHub",
                             "AcmeChip 四亿美元买下 ModelHub，在同一天") is True


def test_guard_abstains_on_clause_tokens_instead_of_guessing():
    """A CJK clause run is not a name, so the alignment cannot read a subject out of it."""
    assert dd._word_like("英伟达") and dd._word_like("stripe")
    assert not dd._word_like("某平台在同一天连发三条公告")


def test_char_similarity_is_a_real_similarity():
    assert dd.char_similarity("同一天两个开放权重模型上线", "同一天两个开放权重模型上线") == 1.0
    assert dd.char_similarity("abcdef", "uvwxyz") == 0.0


# --- fixture backed regression: the shape that archived eleven cards holding three same story pairs
# Synthetic. Invented companies (AcmeChip / ModelHub / Foo / Bar), invented numbers, invented
# clauses. `entities` are the curated slugs the pipeline puts in the canonical key.
_SYNTHETIC_RUN = json.loads(r"""
[
 {"k": "acq-a", "track": "dev-tools",
  "entities": ["acmechip", "modelhub", "open-weights", "model-registry", "vendor-neutrality"],
  "title": "传闻落地：AcmeChip 四亿美元买下 ModelHub，开放权重的默认分发点第一次有了利益相关方",
  "summary": "上周还只是有人放风说 ModelHub 在找买家，这两天变成了既成交易：AcmeChip 以约四亿美元收购 ModelHub，而这家公司去年才拒绝过 AcmeChip 在更低估值下的投资。全世界下载开放权重的那个默认入口，从此有了一个利益相关方。"},
 {"k": "acq-b", "track": "ai-agents",
  "entities": ["acmechip", "modelhub", "open-weights", "model-hub", "accelerator"],
  "title": "AcmeChip 四亿美元买下 ModelHub，在它自己的客户集体自研加速卡的那一天",
  "summary": "同一个下午发生了三件互相解释的事：AcmeChip 报出创纪录的季度营收，随后有人爆出 AcmeChip 同意以约四亿美元收购 ModelHub，而论坛首页第一名就是这条并购。去年 ModelHub 还拒绝过 AcmeChip 的投资。"},
 {"k": "price-a", "track": "ai-agents",
  "entities": ["foo-flash", "bar-flash", "open-weights", "moe", "domestic-silicon"],
  "title": "开放权重再降一个数量级，而且这次跑在自研加速卡上：Foo-Flash 每百万输入 0.19 美元、训练成本 1/9",
  "summary": "Foo 放出 125b 参数每 token 只激活一小部分的开放权重模型，接口报价每百万输入 0.19 美元、输出 0.51 美元，官方称训练成本只有原来的九分之一。同一天 Bar-Flash 也上线了。"},
 {"k": "price-b", "track": "ai-agents",
  "entities": ["foo-flash-next", "bar-flash", "open-weights", "moe", "inference-cost"],
  "title": "同一天两个开放权重模型上线，把前沿级推理价格压到每百万输入 0.19 美元",
  "summary": "Bar 发布 Bar-Flash，Foo 发布 Foo-Flash-Next，两条都冲进了公开榜单前五。Foo 这条把价格摆在明面上：每百万输入 0.19 美元，输出 0.51 美元，训练成本据称只有原来的九分之一。"},
 {"k": "other-1", "track": "dev-tools",
  "entities": ["ducklabs", "analytics", "acquisition"],
  "title": "某云厂收编 DuckLabs：分析栈最后一块本地就能跑的独立拼图被拿走",
  "summary": "分析数据库 DuckLabs 被云厂收购，独立的本地分析栈从此少了一块，自建方案的维护者开始找替代。"},
 {"k": "other-2", "track": "saas-niche",
  "entities": ["crowdsourcing", "labeling", "marketplace"],
  "title": "某平台九月底关掉众包标注市场，二十一年后把人类回路这门生意整个腾了出来",
  "summary": "运行二十一年的众包标注市场下线，人类标注供给侧出现空位，承接方要自己解决质检和结算。"}
]
""")
_SAME_STORY_PAIRS = [("acq-a", "acq-b"), ("price-a", "price-b")]


def _by_key(k):
    return next(c for c in _SYNTHETIC_RUN if c["k"] == k)


def _syn_cand(c, score=72):
    return {"canonical_key": canonical_key(c["entities"], c["track"]),
            "title": c["title"], "summary": c["summary"], "track": c["track"],
            "final_score": score, "lifecycle_stage": "emerging",
            "source_set": ["hackernews", "trend-pulse"],
            "evidence": [{"source": "hackernews", "url": "http://x/a"},
                         {"source": "trend-pulse", "url": "http://x/b"}]}


def _syn_row(c, score=70):
    cand = _syn_cand(c, score)
    ext = dd.build_ext(dict(cand, pushed=True), {"ts": _days_ago(1), "score": score}, {}, CFG)
    ext[EXT + "last_seen"] = _days_ago(1)
    return {"idempotency_key": cand["canonical_key"], "ext": ext}


@pytest.mark.parametrize("a,b", _SAME_STORY_PAIRS)
def test_same_story_written_twice_merges_in_both_directions(a, b):
    """The operator's report: two write-ups of one story, each carrying a different evidence[0],
    both archived as separate cards. They must merge."""
    assert dd.match_existing(_syn_cand(_by_key(b)), [_syn_row(_by_key(a))], CFG) is not None
    assert dd.match_existing(_syn_cand(_by_key(a)), [_syn_row(_by_key(b))], CFG) is not None


@pytest.mark.parametrize("a,b", _SAME_STORY_PAIRS)
def test_merged_same_story_does_not_route_new(a, b):
    cand = _syn_cand(_by_key(b), score=71)
    matched = dd.match_existing(cand, [_syn_row(_by_key(a), score=70)], CFG)
    assert dd.decide(cand, matched, CFG)["branch"] == dd.SUPPRESS


def test_no_cross_story_merges_in_the_fixture():
    """NEGATIVE CONTROL: everything that is NOT one of the pairs must stay distinct, both ways."""
    same = {frozenset(p) for p in _SAME_STORY_PAIRS}
    merged = []
    for a, b in itertools.permutations(_SYNTHETIC_RUN, 2):
        if frozenset((a["k"], b["k"])) in same:
            continue
        if dd.match_existing(_syn_cand(a), [_syn_row(b)], CFG) is not None:
            merged.append((a["k"], b["k"]))
    assert merged == []


@pytest.mark.parametrize("a,b", _SAME_STORY_PAIRS)
def test_the_char_ngram_rung_is_what_merges_them(a, b):
    """NEGATIVE CONTROL: raise the rung's threshold above the pair and the merge disappears, so
    this fixture is exercising the new rung rather than passing on some pre-existing signal."""
    strict = _cfg(dedup_char_ngram_threshold=0.99)
    assert dd.match_existing(_syn_cand(_by_key(b)), [_syn_row(_by_key(a))], strict) is None


@pytest.mark.parametrize("a,b", _SAME_STORY_PAIRS)
def test_char_rung_still_needs_a_second_signal(a, b):
    """Single signal matching stays forbidden: strip the shared curated entities and the same two
    texts must NOT merge on character overlap alone."""
    ca, cb = dict(_by_key(a)), dict(_by_key(b))
    ca["entities"] = ["alpha-only", "one", "two"]
    cb["entities"] = ["beta-only", "three", "four"]
    assert dd.char_similarity((ca["title"] + " " + ca["summary"])[:400],
                              (cb["title"] + " " + cb["summary"])[:400]) >= 0.10
    assert dd.match_existing(_syn_cand(cb), [_syn_row(ca)], CFG) is None


def test_recall_guards_still_hold():
    """The existing recall cases must survive the guard rewrite."""
    row = _ext_row("MinerU PDF extraction open source tool", "parse pdf to markdown", "ai-agents")
    cand = _cand("MinerU open-source PDF extraction tool", "convert pdf into markdown",
                 "ai-agents", 71, sources=("github", "hackernews"))
    assert dd.match_existing(cand, [row], CFG) is not None
    unrelated = _cand("DeFi yield aggregator", "stablecoin onchain vault", "fintech-crypto", 70)
    assert dd.match_existing(unrelated, [row], CFG) is None
