"""URL integrity: the card's link must match the card's headline (operator bug 2026-08-27).

Reported as "discord 推送的网址和标题根本对不上" and reproduced against a real run. Four distinct
defects, all covered here:

  (a) evidence[0] is ordered by COLLECTION SOURCE, not relevance, so the headline linked whatever
      the HN submitter posted (a bare product homepage) instead of the story.
  (b) some archived urls were TRUNCATED mid-slug while a sibling carried the full path.
  (c) at least one url was FABRICATED by a model (a status id ending in a long run of zeros).
  (d) the old _clean_url validated SHAPE ONLY, so it could not see (a), (b) or (c).

Every gate below gets a NEGATIVE CONTROL: a bad input that must be rejected AND a good input that
must be accepted, so a gate that has quietly turned into "reject everything" (or "accept
everything") goes red. All urls here are SYNTHETIC (example.com / example-news.com); the real run's
urls are DATA and stay in the private companion repo.
"""
import io
import os

import pytest

import digest as dg
import push_card as pc


# --------------------------------------------------------------------------- helpers
def _ev(source, url, signal=""):
    return {"source": source, "url": url, "signal": signal}


def _card(title, evidence, side="demand", score=90.0, **kw):
    d = {"title": title, "evidence": evidence, "side": side, "final_score": score,
         "grade": "A", "track": "ai-agents", "independent_source_count": len(evidence),
         "score_breakdown": {"timing": 90}}
    d.update(kw)
    return d


ARTICLE = "https://www.example-news.com/2026/08/acme-shuts-down-widget-service"
ROOT = "https://www.example-widget.com/"
HN = "https://news.ycombinator.com/item?id=49458161"


# =========================================================================== RANKING (defect a)
def test_article_beats_bare_domain_even_when_the_root_is_evidence_zero():
    # the exact 2026-08-27 shape: the HN submitter linked the product homepage, so it sorted first
    c = _card("AcmeCorp 9 月 30 日关掉 Widget Service",
              [_ev("hackernews", ROOT, "HN 408 分, Widget Service shutting down September 30"),
               _ev("web", ARTICLE, "原始报道")])
    links = dg.choose_card_links(c)
    assert links["primary"] == ARTICLE
    assert links["primary"] != c["evidence"][0]["url"]      # red again if evidence[0] comes back


def test_primary_source_beats_the_aggregator_comments_page():
    c = _card("AcmeCorp 事故报告",
              [_ev("hackernews", HN, "HN 272 分 / 364 评论"),
               _ev("web", "https://www.example-lab.com/index/incident-and-the-road-ahead/",
                   "官方事故报告")])
    assert dg.choose_card_links(c)["primary"] == \
        "https://www.example-lab.com/index/incident-and-the-road-ahead/"


def test_aggregator_is_kept_as_a_secondary_discussion_link_not_thrown_away():
    c = _card("AcmeCorp 事故报告", [_ev("hackernews", HN, "HN 272 分"), _ev("web", ARTICLE, "报道")])
    links = dg.choose_card_links(c)
    assert links["primary"] == ARTICLE and links["discussion"] == HN


def test_aggregator_only_card_uses_it_as_primary_and_has_no_duplicate_discussion():
    c = _card("只有 HN 讨论的一条", [_ev("hackernews", HN, "HN 272 分")])
    links = dg.choose_card_links(c)
    assert links["primary"] == HN and links["discussion"] == ""   # never the same link twice


def test_title_token_overlap_decides_between_two_articles():
    on_topic = "https://www.example-news.com/tech/acme-buys-widgetlab-for-12-9-billion"
    off_topic = "https://www.example-news.com/tech/unrelated-quarterly-earnings-roundup"
    c = _card("AcmeCorp 129 亿美元收购 WidgetLab",
              [_ev("gdelt", off_topic, "财经综述"),
               _ev("cn-feeds", on_topic, "极客公园: 129 亿美元, AcmeCorp 收购 WidgetLab")])
    assert dg.choose_card_links(c)["primary"] == on_topic


def test_chooser_is_pure_and_deterministic():
    c = _card("t", [_ev("hackernews", HN, "s"), _ev("web", ARTICLE, "s")])
    snapshot = repr(c)
    a, b = dg.choose_card_links(c), dg.choose_card_links(c)
    assert a == b                       # deterministic
    assert repr(c) == snapshot          # pure: the card is not mutated


def test_card_with_no_evidence_reports_nothing_considered():
    links = dg.choose_card_links(_card("t", []))
    assert links == {"primary": "", "discussion": "", "rejected": [],
                     "considered": 0, "accepted": 0}


# =========================================================================== VALIDATION (b, c, d)
def test_validate_accepts_a_normal_article_url():
    # NEGATIVE CONTROL for the whole validator: a gate that rejects everything is not a gate
    assert dg.validate_url(ARTICLE) == ""


def test_validate_rejects_a_bare_domain_and_a_site_root():
    assert dg.validate_url(ROOT) == "site_root"
    assert dg.validate_url("https://www.example-widget.com") == "site_root"


def test_validate_rejects_a_path_truncated_mid_slug_by_a_sibling():
    short = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13"
    full = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13-billion-dollars-2026-8"
    assert dg.validate_url(short, {full}) == "truncated"
    assert dg.validate_url(full, {short, full}) == ""       # the longer one is fine


def test_truncation_check_does_not_fire_on_a_deeper_page_under_the_same_path():
    # NEGATIVE CONTROL against over-rejection: /a/b and /a/b/c are two different pages, not a cut
    parent = "https://www.example-news.com/topics/agents"
    child = "https://www.example-news.com/topics/agents/2026-outlook"
    assert dg.validate_url(parent, {child}) == ""
    assert dg.validate_url(child, {parent}) == ""


def test_truncation_check_needs_a_sibling_to_fire():
    short = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13"
    assert dg.validate_url(short, set()) == ""      # nothing to compare against -> not a claim


def test_validate_rejects_a_fabricated_looking_status_id():
    assert dg.validate_url("https://x.com/someone/status/1234567890000000000") == "fabricated_id"


def test_validate_accepts_a_real_shaped_status_id():
    # NEGATIVE CONTROL: real snowflake ids are not round numbers, and must not be rejected
    assert dg.validate_url("https://x.com/someone/status/2092931447644442635") == ""
    assert dg.validate_url("https://x.com/someone/status/1234567890123456780") == ""


def test_validate_rejects_malformed_and_non_http():
    assert dg.validate_url("https://x/ a\n> ## HACK") == "malformed"
    assert dg.validate_url("ftp://example.com/a/b") == "malformed"
    assert dg.validate_url("") == "malformed"


def test_every_rejection_has_a_human_reason():
    for code in ("malformed", "site_root", "truncated", "fabricated_id"):
        assert dg._REJECT_REASONS[code].strip()


# ================================================== REPORTING: a lost link is never a silent swap
def test_card_whose_only_url_is_a_root_loses_its_link_and_says_so():
    c = _card("AcmeCorp 9 月 30 日关掉 Widget Service", [_ev("hackernews", ROOT, "HN 408 分")])
    links = dg.choose_card_links(c)
    assert links["primary"] == "" and links["accepted"] == 0
    assert links["considered"] == 1
    assert [r["reason"] for r in links["rejected"]] == ["site_root"]

    md = dg.build_markdown([c], {"candidates": 1}, "2026-08-27")
    head = dg.build_headlines([c], {"candidates": 1}, date="2026-08-27")
    assert "拒收" in md and "拒收" in head          # the run SHOWS that the card lost its link
    assert ROOT not in head                        # and never ships the rejected url as the link


def test_bare_domain_is_caught_against_the_pool_of_the_whole_batch():
    # the real livemint case: one card carried the bare domain, a SIBLING CARD carried the article
    bare = _card("卡片 A", [_ev("gdelt", "https://www.example-mint.com/", "印度财经媒体报道")])
    full = _card("卡片 B", [_ev("gdelt",
                              "https://www.example-mint.com/companies/news/acme-to-buy-widgetlab",
                              "同一家媒体的正文")])
    a, b = dg.card_links([bare, full])
    assert a["primary"] == "" and a["rejected"][0]["reason"] == "site_root"
    assert b["primary"].endswith("/acme-to-buy-widgetlab")


def test_truncated_url_is_caught_against_a_sibling_card():
    short = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13"
    full = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13-billion-dollars-2026-8"
    c1 = _card("卡片 A", [_ev("hackernews", short, "HN 头版")])
    c2 = _card("卡片 B", [_ev("web", full, "原始报道")])
    a, _b = dg.card_links([c1, c2])
    assert a["primary"] == "" and a["rejected"][0]["reason"] == "truncated"


def test_the_rendering_path_itself_carries_the_batch_pool_not_just_card_links():
    """The two tests above prove card_links() shares a pool. This one proves the RENDERERS do.

    A pool-less single-card call (`choose_card_links(card)` with no pool) still returns a link and
    still looks correct on its own card, so it degrades the truncation guard SILENTLY: only the
    batch pool can see that a sibling card carries the longer url. digest._primary_url was exactly
    that shape and was deleted; this drives build_markdown/build_headlines, the real dispatch sites,
    so reintroducing a pool-less call anywhere on the render path goes red here rather than passing
    because card_links() is still correct in isolation.
    """
    short = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13"
    full = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13-billion-dollars-2026-8"
    cards = [_card("卡片 A", [_ev("hackernews", short, "HN 头版")]),
             _card("卡片 B", [_ev("web", full, "原始报道")])]

    md = dg.build_markdown(cards, {"candidates": 2}, "2026-08-27")
    # the truncated url is never the card's link. It still appears in the evidence detail list,
    # which is the archive's raw record, so the assertion is on the 链接 LINE, matched whole:
    # `short` is a prefix of `full`, so a substring test would pass on card B's own good line.
    lines = md.splitlines()
    assert f"- 链接: {short}" not in lines
    assert "拒收" in md and dg._REJECT_REASONS["truncated"] in md
    assert f"- 链接: {full}" in lines            # the good sibling is untouched

    head = dg.build_headlines(cards, {"candidates": 2}, date="2026-08-27")
    assert f"<{short}>" not in head              # the push never ships the truncated url at all
    assert f"<{full}>" in head
    assert "链接被拒收" in head


def test_a_url_with_no_longer_sibling_still_renders_on_both_paths():
    """NEGATIVE CONTROL for the test above: without it, "the renderers see the pool" could be
    satisfied by a renderer that refuses every url. The SAME short url, alone in its batch, has
    nothing extending it and must render as a normal link on both artifacts."""
    short = "https://www.example-news.com/acme-in-talks-to-buy-widgetlab-13"
    cards = [_card("卡片 A", [_ev("hackernews", short, "HN 头版")])]

    md = dg.build_markdown(cards, {"candidates": 1}, "2026-08-27")
    assert f"- 链接: {short}" in md.splitlines()
    assert "拒收" not in md

    head = dg.build_headlines(cards, {"candidates": 1}, date="2026-08-27")
    assert f"🔗 <{short}>" in head
    assert "链接被拒收" not in head


def test_fabricated_id_is_rejected_and_the_card_falls_back_with_a_report():
    fake = "https://x.com/someone/status/1234567890000000000"
    c = _card("机器人视频成本下降", [_ev("twitterapi", fake, "4210 faves"),
                                      _ev("cn-feeds", ARTICLE, "量子位报道")])
    links = dg.choose_card_links(c)
    assert links["primary"] == ARTICLE                       # swapped...
    assert links["rejected"][0]["reason"] == "fabricated_id"  # ...but NOT silently
    md = dg.build_markdown([c], {}, "2026-08-27")
    assert fake in md and "拒收" in md                       # the archive records what was thrown out


def test_malformed_url_is_reported_without_echoing_its_payload():
    c = _card("t", [_ev("web", "https://x/ a\n> ## HACK", "s")])
    links = dg.choose_card_links(c)
    assert links["rejected"][0]["reason"] == "malformed"
    assert links["rejected"][0]["url"] == ""                 # injection payload never re-emitted
    md = dg.build_markdown([c], {}, "2026-08-27")
    assert "HACK" not in md and "\n## " in md                # only the real headings survive


# =========================================================== ONE chooser: archive == pushed link
def test_markdown_and_headlines_choose_the_same_url_for_the_same_card():
    cards = [
        _card("AcmeCorp 关掉 Widget Service",
              [_ev("hackernews", ROOT, "HN 408 分"), _ev("web", ARTICLE, "CNBC 原始报道")]),
        _card("事故报告", [_ev("hackernews", HN, "HN 272 分"),
                          _ev("web", "https://www.example-lab.com/index/incident/", "官方")],
              side="supply"),
    ]
    md = dg.build_markdown(cards, {"candidates": 2}, "2026-08-27")
    head = dg.build_headlines(cards, {"candidates": 2}, date="2026-08-27")
    for links in dg.card_links(cards):
        assert links["primary"] in md and f"<{links['primary']}>" in head
    assert ROOT not in head                       # the wrong link is in neither artifact
    assert f"- 链接: {ARTICLE}" in md


def test_embed_url_uses_the_same_chooser():
    c = _card("AcmeCorp 关掉 Widget Service",
              [_ev("hackernews", ROOT, "HN 408 分"), _ev("web", ARTICLE, "原始报道")])
    assert pc.build_embed(c)["url"] == ARTICLE


# =========================================================================== COVERAGE LINE (5)
_FULL_COV = {"signals_collected": 812, "sources_invoked": 11, "sources_available": 13,
             "sources_failed": [{"source": "reddit", "error": "429"}],
             "candidates": 12, "signals_unaccounted": 47, "suppressed": 3,
             "below_floor": [{"title": "弱卡", "side": "demand", "final_score": 57.0, "floor": 60}],
             "below_sources": [], "community_pulse": [], "pushed": 4, "deepdived": 0}


def test_coverage_line_renders_the_contract_numbers():
    line = dg.coverage_line(_FULL_COV, qualified=6)
    for frag in ("信号 812", "源 11/13", "失败源 1", "候选 12", "合格 6",
                 "去重抑制 3", "未达门槛 1", "未归因信号 47"):
        assert frag in line
    assert dg._COV_UNKNOWN not in line


def test_missing_coverage_reads_as_not_counted_never_as_zero():
    # "clean" and "nobody checked" must be different outputs
    line = dg.coverage_line({}, qualified=0)
    assert line.count(dg._COV_UNKNOWN) >= 6
    assert "信号 0" not in line and "候选 0" not in line


def test_the_old_placeholder_never_renders_as_a_number():
    # 30 of 31 archived digests shipped "(see SKILL run)" where the source count belonged
    line = dg.coverage_line({"sources_invoked": "(see SKILL run)",
                             "sources_available": "(see SKILL run)"}, qualified=1)
    assert "(see SKILL run)" not in line
    assert f"源 {dg._COV_UNKNOWN}/{dg._COV_UNKNOWN}" in line


def test_unmeasured_fields_read_as_not_counted_even_though_the_value_is_zero():
    # run.py's build_coverage reports an UNOBSERVED field as a placeholder 0 plus its name in
    # coverage["unmeasured"]. Printing that 0 would claim "the collection layer found nothing",
    # which is a different statement from "nobody counted". This is the seam where they diverge.
    cov = {"signals_collected": 0, "signals_unaccounted": 0, "sources_invoked": 0,
           "sources_available": 0, "sources_failed": [], "candidates": 7,
           "suppressed": 2, "below_floor": [],
           "unmeasured": ["signals_collected", "signals_unaccounted", "sources_invoked",
                          "sources_available", "sources_failed", "below_floor"]}
    line = dg.coverage_line(cov, qualified=3)
    assert "信号 0" not in line and "未归因信号 0" not in line
    assert "失败源 0" not in line and "未达门槛 0" not in line
    assert f"源 {dg._COV_UNKNOWN}/{dg._COV_UNKNOWN}" in line
    # the fields that WERE measured still print their real numbers, including a real zero
    assert "候选 7" in line and "合格 3" in line and "去重抑制 2" in line


def test_a_measured_zero_still_prints_as_zero():
    # NEGATIVE CONTROL for the line above: without this, "honor unmeasured" could degenerate into
    # "never print a zero", which would hide a genuinely empty collection run.
    cov = dict(_FULL_COV, signals_collected=0, suppressed=0, sources_failed=[], below_floor=[],
               unmeasured=[])
    line = dg.coverage_line(cov, qualified=0)
    assert "信号 0" in line and "去重抑制 0" in line
    assert "失败源 0" in line and "未达门槛 0" in line


def test_a_malformed_unmeasured_field_does_not_erase_measured_numbers():
    # unmeasured is engine-computed; a garbled one must not silently blank the whole line
    line = dg.coverage_line(dict(_FULL_COV, unmeasured="signals_collected"), qualified=6)
    assert "信号 812" in line


def test_digest_header_and_headlines_both_carry_the_coverage_line():
    md = dg.build_markdown([], _FULL_COV, "2026-08-27")
    head = dg.build_headlines([], _FULL_COV, date="2026-08-27")
    assert "信号 812" in md and "未归因信号 47" in md
    assert "信号 812" in head and "未归因信号 47" in head


def test_dropped_items_are_listed_not_just_counted():
    md = dg.build_markdown([], _FULL_COV, "2026-08-27")
    assert "未达分数门槛" in md and "弱卡" in md and "57" in md and "60" in md
    assert "失败源" in md and "reddit" in md and "429" in md


def test_no_dropped_sections_when_nothing_was_dropped():
    md = dg.build_markdown([], {"signals_collected": 1, "below_floor": [], "sources_failed": []},
                           "2026-08-27")
    assert "未达分数门槛" not in md and "## 失败源" not in md


# =========================================================================== write_digest_file (3)
_REAL = "# Daily Hotspots, 2026-08-27\n\n## A 90, 真卡片\n"
_EMPTY = "# Daily Hotspots, 2026-08-27\n\n**今日无合格机会** (no opportunity cleared).\n"


def _digest_path(tmp_path, date="2026-08-27"):
    return tmp_path / "digests" / date[:4] / f"{date}.md"


def test_write_digest_file_writes_atomically_and_leaves_no_temp_file(tmp_path):
    p = dg.write_digest_file(_REAL, str(tmp_path), "2026-08-27")
    assert p.read_text(encoding="utf-8") == _REAL
    leftovers = [f for f in p.parent.iterdir() if f.name != p.name]
    assert leftovers == []


def test_write_digest_file_refuses_the_same_day_empty_rerun_clobber(tmp_path):
    dg.write_digest_file(_REAL, str(tmp_path), "2026-08-27")
    with pytest.raises(dg.DigestClobberError):
        dg.write_digest_file(_EMPTY, str(tmp_path), "2026-08-27")
    # the real digest survived intact: refusing means keeping, not truncating
    assert _digest_path(tmp_path).read_text(encoding="utf-8") == _REAL


def test_write_digest_file_allows_a_real_rerun_and_an_empty_over_empty(tmp_path):
    # NEGATIVE CONTROL for the clobber guard: it must not block legitimate rewrites
    dg.write_digest_file(_REAL, str(tmp_path), "2026-08-27")
    dg.write_digest_file(_REAL + "\n## A 80, 第二张\n", str(tmp_path), "2026-08-27")
    assert "第二张" in _digest_path(tmp_path).read_text(encoding="utf-8")

    dg.write_digest_file(_EMPTY, str(tmp_path), "2026-08-28")
    dg.write_digest_file(_EMPTY, str(tmp_path), "2026-08-28")
    assert dg.digest_is_empty_day(_digest_path(tmp_path, "2026-08-28").read_text(encoding="utf-8"))


def test_write_digest_file_hard_fails_and_keeps_the_old_file_when_the_rename_fails(tmp_path,
                                                                                   monkeypatch):
    dg.write_digest_file(_REAL, str(tmp_path), "2026-08-27")

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(dg.os, "replace", boom)
    with pytest.raises(OSError):                       # WRITER: hard fail, never a shrug
        dg.write_digest_file(_REAL + "新内容", str(tmp_path), "2026-08-27")
    p = _digest_path(tmp_path)
    assert p.read_text(encoding="utf-8") == _REAL      # old content untouched
    assert [f for f in p.parent.iterdir() if f.name != p.name] == []   # temp cleaned up


def test_empty_day_detection_distinguishes_an_empty_demand_column(tmp_path):
    # a day with supply cards but no demand card is NOT an empty day
    md = dg.build_markdown([_card("供给卡", [_ev("web", ARTICLE)], side="supply")],
                           {"candidates": 1}, "2026-08-27")
    assert "今日无合格需求机会" in md and dg.digest_is_empty_day(md) is False
    assert dg.digest_is_empty_day(dg.build_markdown([], {}, "2026-08-27")) is True


# =========================================================================== deliver() length (4)
def _patch_relay(monkeypatch):
    sent = []

    class _Proc:
        returncode = 0

    def fake_run(cmd, *a, **k):
        sent.append(cmd[-1])
        return _Proc()

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    monkeypatch.delenv("DAILY_HOTSPOTS_DRYRUN", raising=False)
    return sent


def test_short_message_is_sent_once_untouched(monkeypatch, capsys):
    sent = _patch_relay(monkeypatch)
    msg = "短消息\n第二行"
    ok, detail = pc.deliver(msg)
    assert ok and sent == [msg]
    assert "分段" not in capsys.readouterr().err and "分 " not in detail


def test_over_limit_message_warns_splits_and_says_so_in_the_text(monkeypatch, capsys):
    sent = _patch_relay(monkeypatch)
    blocks = [f"**{i}.【AI】标题 {i}**\n摘要 {i}" + "内容" * 60 for i in range(30)]
    msg = "\n\n".join(blocks)
    assert len(msg) > pc.CONTENT_MAX
    ok, detail = pc.deliver(msg)

    err = capsys.readouterr().err
    assert ok and len(sent) > 1
    assert "discord content limit" in err and str(pc.CONTENT_MAX) in err   # the promised warning
    assert all(len(chunk) <= pc.CONTENT_MAX for chunk in sent)             # under the hard limit
    assert all("已分段发送" in chunk for chunk in sent)                     # the TEXT says so
    assert f"分 {len(sent)} 段" in detail
    # nothing dropped: every original line survives somewhere in the delivered chunks
    joined = "\n".join(sent)
    for line in msg.split("\n"):
        assert line in joined


def test_a_single_over_long_line_is_cut_visibly_and_reported(monkeypatch, capsys):
    sent = _patch_relay(monkeypatch)
    msg = "头" + "长" * (pc.CONTENT_MAX * 2)
    ok, detail = pc.deliver(msg)
    err = capsys.readouterr().err
    assert ok and len(sent) == 1
    assert "本行超长，已截断" in sent[0]          # the reader is told, in the message itself
    assert len(sent[0]) <= pc.CONTENT_MAX
    assert "硬截断" in detail and "hard cut" in err


def test_split_is_pure_and_never_loses_a_boundary():
    msg = "\n\n".join(f"块 {i}\n行 {i}" for i in range(400))
    chunks, cut = pc.split_for_discord(msg, 500)
    assert cut == 0
    assert all(len(c) <= 500 for c in chunks)
    rejoined = "\n".join(chunks)
    for line in msg.split("\n"):
        if line:
            assert line in rejoined
    assert pc.split_for_discord(msg, 500) == (chunks, cut)      # deterministic


def test_split_returns_one_chunk_when_it_fits():
    # NEGATIVE CONTROL: the splitter must not fire on a message that is already legal
    assert pc.split_for_discord("abc", pc.CONTENT_MAX) == (["abc"], 0)


def test_dry_run_reports_the_split_it_would_make():
    msg = "\n\n".join(f"块 {i}" + "内容" * 60 for i in range(30))
    assert len(msg) > pc.CONTENT_MAX
    ok, detail = pc.deliver(msg, dry_run=True)
    assert ok and f"{len(msg)} chars" in detail and "分 " in detail and "段" in detail


def test_failed_chunk_is_reported_as_failure(monkeypatch):
    calls = {"n": 0}

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        return _Proc(0 if calls["n"] == 1 else 1)

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    monkeypatch.delenv("DAILY_HOTSPOTS_DRYRUN", raising=False)
    msg = "\n\n".join(f"块 {i}" + "内容" * 60 for i in range(30))
    assert len(msg) > pc.CONTENT_MAX
    ok, detail = pc.deliver(msg)
    assert ok is False and "rc=1" in detail       # a partial send is not a success


def test_source_files_carry_no_en_or_em_dash():
    # house rule: published prose (and these files' comments) carry no en/em dash
    for name in ("scripts/digest.py", "scripts/push_card.py", "tests/test_url_integrity.py"):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
        src = io.open(p, encoding="utf-8").read()
        for ch in (chr(0x2013), chr(0x2014), chr(0x2015)):
            assert ch not in src, f"{name} contains {ch!r}"


# --------------------------------------------------------------------------- kind beats wording
# Added 2026-08-28 after the first implementation broke its OWN stated guarantee in production.
# `_rank_url` scored kind as additive penalties (aggregator -40, social -5) plus up to +9 of
# title/signal token overlap, so the overlap term could outbid the social penalty, and it did: on a
# card about an official incident report the tweet beat the vendor's own report, because the card's
# Chinese text had been summarized FROM that tweet and so matched its blurb almost word for word
# while the English report url matched nothing. A blurb agreeing with the headline is evidence about
# the summarizer, not about the source. Kind is now a hard ordering and wording only breaks ties
# inside a kind. These tests pin that, because a weighted sum will silently drift back.
def _tier_card(evidence, title="官方事故报告发布，四个方向同时在补同一个洞"):
    return {"title": title, "evidence": evidence}


def _tier_ev(url, signal=""):
    return {"url": url, "signal": signal, "source": "x", "ts": "2026-08-27T00:00:00Z"}


def test_a_real_article_beats_a_tweet_whose_blurb_matches_the_title_word_for_word():
    """The exact production failure. The tweet's signal text is the headline; the article's is not.
    The article must still win, because kind outranks wording."""
    title = "官方事故报告发布，四个方向同时在补同一个洞"
    card = _tier_card([
        _tier_ev("https://x.com/someone/status/2092692685224325542", signal=title),
        _tier_ev("https://example-vendor.com/index/incident-and-the-road-ahead/", signal="incident report"),
    ], title=title)
    links = dg.choose_card_links(card, dg.url_pool([card]))
    assert links["primary"] == "https://example-vendor.com/index/incident-and-the-road-ahead/", \
        f"a tweet outbid a real article on wording again: {links['primary']}"


def test_a_tweet_is_still_used_when_it_is_the_only_thing_there():
    """Over-rejection control: the ordering demotes a social post, it does not ban one."""
    card = _tier_card([_tier_ev("https://x.com/someone/status/2092692685224325542", signal="爆料")])
    links = dg.choose_card_links(card, dg.url_pool([card]))
    assert links["primary"] == "https://x.com/someone/status/2092692685224325542"


def test_an_article_beats_an_aggregator_thread_and_the_thread_is_kept_as_discussion():
    card = _tier_card([
        _tier_ev("https://news.ycombinator.com/item?id=49454314", signal="HN 首页 272 分"),
        _tier_ev("https://example-press.com/2026/08/27/the-actual-story", signal="原始报道"),
    ])
    links = dg.choose_card_links(card, dg.url_pool([card]))
    assert links["primary"] == "https://example-press.com/2026/08/27/the-actual-story"
    assert links["discussion"] == "https://news.ycombinator.com/item?id=49454314"


def test_kind_ordering_is_strict_across_every_pair():
    """No amount of wording overlap may reorder two different kinds."""
    title = "完全一致的标题文字用来制造最大重叠"
    ladder = [
        "https://example-press.com/2026/08/27/a-real-article",   # tier 0
        "https://example-press.com/section",                     # tier 1
        "https://x.com/someone/status/2092692685224325542",      # tier 2
        "https://news.ycombinator.com/item?id=1",                # tier 3
    ]
    for better in range(len(ladder)):
        for worse in range(better + 1, len(ladder)):
            # give the WORSE url the perfect blurb and the better url nothing at all
            card = _tier_card([_tier_ev(ladder[worse], signal=title), _tier_ev(ladder[better], signal="")],
                              title=title)
            links = dg.choose_card_links(card, dg.url_pool([card]))
            assert links["primary"] == ladder[better], (
                f"{ladder[worse]} (worse kind, perfect wording) beat {ladder[better]}")
