"""Presentation of source health, of an archived card's score, and of the daily push (2026-08-29).

Three defects, all reproduced from real measurements, all about the SAME thing: an artifact that
looks fine while saying nothing.

  (a) A source can die QUIETLY. brightdata answered a control fetch of a page it must always be able
      to reach with a well formed EMPTY body, and a control search with an empty organic list, no
      error either time, while being the first hop of the retrieval chain and the only route for one
      whole lane. arctic-shift 500'd on half of twelve identical calls while still being named as the
      reddit fetcher. Neither day's digest said one word about it, because the coverage line had no
      place to say it. Now it does, and when something is down or fail-open the line SHOUTS and NAMES
      the source.
  (b) An ARCHIVED record stores its score under `score`; every renderer read `final_score` only, so
      replaying history printed "(B None)" and sorted every historical card to zero.
  (c) The Discord splitter decided "a chunk is open" by testing whether the accumulator was truthy,
      so a message whose pieces are all empty strings fell through to a fallback that hard truncated
      the message and reported zero dropped characters.

Every gate below carries its NEGATIVE CONTROL, because each of these is one line away from
degenerating into its useless twin: "always shout", "never print a number", "never print a zero".
All names and content here are SYNTHETIC.
"""
import re

import digest as dg
import push_card as pc


# --------------------------------------------------------------------------------------- helpers
def _health(**kw):
    """A source_health block in the contract's shape, with every key present."""
    b = {"ok": 0, "degraded": 0, "down": 0, "fail_open_suspected": 0, "unknown": 0,
         "names_down": [], "names_fail_open": []}
    b.update(kw)
    return b


def _cov(**kw):
    c = {"candidates": 3, "suppressed": 0}
    c.update(kw)
    return c


def _card(title="一张卡", score=80.0, side="demand", **kw):
    d = {"title": title, "final_score": score, "grade": "B+", "track": "saas-niche",
         "independent_source_count": 3, "side": side,
         "score_breakdown": {"timing": 80},
         "evidence": [{"source": "web", "url": "https://www.example-news.com/2026/08/a-real-story",
                       "signal": "官方公告"}]}
    d.update(kw)
    return d


ALL_OK = _health(ok=5)
DOWN = _health(ok=3, down=2, names_down=["arctic-shift", "google-news"])
FAIL_OPEN = _health(ok=4, fail_open_suspected=1, names_fail_open=["brightdata"])


# ============================================================ 1. a dying source cannot stay quiet
def test_all_ok_renders_a_quiet_tick_with_counts():
    seg = dg.source_health_segment(_cov(source_health=ALL_OK))
    assert dg._HEALTH_OK in seg and "5" in seg
    assert dg._HEALTH_ALARM not in seg and dg._COV_UNKNOWN not in seg


def test_a_down_source_is_loud_and_named_on_the_coverage_line():
    seg = dg.source_health_segment(_cov(source_health=DOWN))
    assert dg._HEALTH_ALARM in seg                     # loud
    assert "arctic-shift" in seg and "google-news" in seg   # NAMED, which is the whole point
    assert "断源 2" in seg


def test_a_fail_open_source_is_named_and_kept_distinct_from_a_down_source():
    # the fail-open case is the one that fooled the pipeline for weeks; collapsing it into "down"
    # would erase exactly the distinction this round exists to draw.
    seg = dg.source_health_segment(_cov(source_health=FAIL_OPEN))
    assert dg._HEALTH_ALARM in seg and "brightdata" in seg
    assert "疑似假成功" in seg and "断源" not in seg


def test_down_and_fail_open_are_reported_side_by_side():
    seg = dg.source_health_segment(_cov(source_health=_health(
        ok=2, down=1, fail_open_suspected=1,
        names_down=["arctic-shift"], names_fail_open=["brightdata"])))
    assert "断源 1 [arctic-shift]" in seg and "疑似假成功 1 [brightdata]" in seg
    assert "正常 2" in seg


def test_degraded_warns_but_does_not_shout():
    # NEGATIVE CONTROL for the loud path: if everything shouted, nothing would.
    seg = dg.source_health_segment(_cov(source_health=_health(ok=4, degraded=1)))
    assert dg._HEALTH_WARN in seg and "降级 1" in seg
    assert dg._HEALTH_ALARM not in seg and dg._HEALTH_OK not in seg
    assert dg._COV_UNKNOWN not in seg


def test_unknown_state_warns_and_is_not_counted_as_ok():
    seg = dg.source_health_segment(_cov(source_health=_health(ok=2, unknown=1)))
    assert dg._HEALTH_WARN in seg and "状态未知 1" in seg and "正常 2" in seg
    assert dg._HEALTH_OK not in seg


# --------------------------------------------------- 2. unprobed is not "clean" and is not a zero
def test_unmeasured_health_renders_the_marker_never_a_zero_and_never_a_tick():
    # run.py reports an unobserved field as a placeholder value PLUS its name in `unmeasured`.
    cov = _cov(source_health=_health(), unmeasured=["source_health"])
    seg = dg.source_health_segment(cov)
    assert dg._COV_UNKNOWN in seg
    assert "正常 0" not in seg and "断源" not in seg
    assert dg._HEALTH_OK not in seg and dg._HEALTH_ALARM not in seg
    assert dg.source_health(cov) is None


def test_missing_health_key_renders_the_marker_not_a_clean_bill():
    seg = dg.source_health_segment(_cov())
    assert seg == f"源健康 {dg._COV_UNKNOWN}"


def test_malformed_health_block_renders_the_marker():
    for bad in ("(see SKILL run)", 0, [], None, True):
        assert dg._COV_UNKNOWN in dg.source_health_segment(_cov(source_health=bad))


def test_probed_nothing_is_a_third_output_distinct_from_clean_and_from_unmeasured():
    # sourcehealth's CLI exit 2 ("nothing could be checked at all") is neither health nor silence.
    nothing = dg.source_health_segment(_cov(source_health=_health()))
    clean = dg.source_health_segment(_cov(source_health=ALL_OK))
    unknown = dg.source_health_segment(_cov())
    assert nothing != clean and nothing != unknown and clean != unknown
    assert dg._HEALTH_WARN in nothing and dg._HEALTH_OK not in nothing
    assert "0" not in nothing                       # not "正常 0", not "0/0"


def test_a_measured_zero_down_still_reads_as_healthy():
    # NEGATIVE CONTROL for the line above: "honor unmeasured" must not become "never trust zeros".
    seg = dg.source_health_segment(_cov(source_health=_health(ok=7)))
    assert dg._HEALTH_OK in seg and "7/7" in seg


# ------------------------------------------- 3. the count and the names are reconciled, not capped
def test_a_count_without_names_says_so_instead_of_implying_names():
    seg = dg.source_health_segment(_cov(source_health=_health(ok=1, down=3, names_down=["a-src"])))
    assert "3 [a-src, +2 未具名]" in seg


def test_names_beyond_the_count_are_still_printed():
    seg = dg.source_health_segment(_cov(source_health=_health(
        down=0, names_down=["a-src", "b-src"])))
    assert "a-src" in seg and "b-src" in seg and dg._HEALTH_ALARM in seg


def test_nine_dead_sources_print_nine_names_no_silent_cap():
    names = [f"src-{i}" for i in range(9)]
    seg = dg.source_health_segment(_cov(source_health=_health(down=9, names_down=names)))
    for n in names:
        assert n in seg


def test_a_source_name_is_untrusted_data_and_cannot_inject_a_block():
    md = dg.build_markdown([_card()], _cov(source_health=_health(
        down=1, names_down=["evil\n## FAKE HEADING"])), "2026-08-29")
    assert "\n## FAKE HEADING" not in md
    heads = [ln for ln in md.split("\n") if ln.startswith("## ")]
    assert heads == ["## 🎯 需求机会 (demand, 高质量/非共识)", "## B+ 80, 一张卡",
                     "## 📈 供给热点 (supply, 基础广度)"]
    assert "FAKE HEADING" in md                  # the text survives, inline and harmless


# ------------------------------------------------- 4. the alarm reaches BOTH delivered artifacts
def test_the_archive_digest_shouts_above_the_first_card():
    md = dg.build_markdown([_card(title="卡片标题")], _cov(source_health=DOWN), "2026-08-29")
    assert "源健康告警" in md and "arctic-shift" in md and "google-news" in md
    assert md.index("源健康告警") < md.index("卡片标题")     # above every card, not buried


def test_the_pushed_message_shouts_and_names_the_source():
    out = dg.build_headlines([_card()], _cov(source_health=DOWN), date="2026-08-29")
    assert dg._HEALTH_ALARM in out and "arctic-shift" in out and "google-news" in out


def _alert_block(text, quote=False):
    """The standalone alert lines, i.e. the ones that are NOT the dense coverage line."""
    return [ln for ln in text.split("\n")
            if "源健康告警" in ln or ln.startswith(("- 断源", "> - 断源", "- 疑似", "> - 疑似"))]


def test_the_alarm_gets_its_own_block_not_only_the_dense_coverage_line():
    # a one-liner is skimmed past; the standalone block is what survives skimming. Both artifacts
    # must carry it, and it must break the buckets out on their own lines.
    cov = _cov(source_health=_health(ok=1, down=1, fail_open_suspected=1,
                                     names_down=["arctic-shift"], names_fail_open=["brightdata"]))
    lines = dg.source_health_alert(cov)
    assert len(lines) == 3
    assert lines[0].startswith(dg._HEALTH_ALARM) and "覆盖不完整" in lines[0]
    assert "断源" in lines[1] and "arctic-shift" in lines[1]
    assert "疑似假成功" in lines[2] and "brightdata" in lines[2]

    head = dg.build_headlines([_card()], cov, date="2026-08-29")
    md = dg.build_markdown([_card()], cov, "2026-08-29")
    assert len(_alert_block(head)) >= 3           # coverage line + the three alert lines
    assert all(ln in head for ln in lines)
    for ln in dg.source_health_alert(cov, quote=True):
        assert ln.startswith("> ") and ln in md   # rendered as a markdown blockquote in the archive


def test_the_alarm_survives_an_empty_day_which_is_when_it_matters_most():
    # "no cards" and "no data reached the scorer" are the two readings of a thin day.
    out = dg.build_headlines([], _cov(source_health=FAIL_OPEN), date="2026-08-29")
    assert "无合格机会" in out                       # still the honest empty-day text
    assert dg._HEALTH_ALARM in out and "brightdata" in out
    md = dg.build_markdown([], _cov(source_health=FAIL_OPEN), "2026-08-29")
    assert "brightdata" in md and "疑似假成功" in md


def test_a_healthy_day_carries_no_alarm_block_in_either_artifact():
    # NEGATIVE CONTROL: the alarm must be absent when there is nothing to alarm about.
    cov = _cov(source_health=ALL_OK)
    assert dg.source_health_alert(cov) == []
    assert "源健康告警" not in dg.build_markdown([_card()], cov, "2026-08-29")
    assert "源健康告警" not in dg.build_headlines([_card()], cov, date="2026-08-29")


def test_both_artifacts_carry_the_health_segment_on_the_coverage_line():
    cov = _cov(source_health=ALL_OK)
    md = dg.build_markdown([], cov, "2026-08-29")
    head = dg.build_headlines([], cov, date="2026-08-29")
    seg = dg.source_health_segment(cov)
    cov_line_md = [ln for ln in md.split("\n") if ln.startswith("> 覆盖:")][0]
    cov_line_head = [ln for ln in head.split("\n") if ln.startswith("覆盖:")][0]
    assert seg in cov_line_md and seg in cov_line_head


def test_an_unprobed_run_says_so_on_both_coverage_lines():
    md = dg.build_markdown([], _cov(), "2026-08-29")
    head = dg.build_headlines([], _cov(), date="2026-08-29")
    assert f"源健康 {dg._COV_UNKNOWN}" in md and f"源健康 {dg._COV_UNKNOWN}" in head


def test_the_real_seam_run_build_coverage_to_the_rendered_line():
    # the cross-group seam, exercised against the real producers rather than a hand written dict:
    # sourcehealth.probe_all's shape -> run.build_coverage -> the rendered coverage line.
    import run
    import sourcehealth as sh

    unprobed = run.build_coverage([], [], [], [], [], {}, [], None)
    assert "source_health" in unprobed["unmeasured"]
    assert f"源健康 {dg._COV_UNKNOWN}" in dg.source_health_segment(unprobed)

    summary = {"results": [{"name": "brightdata", "state": sh.FAIL_OPEN},
                           {"name": "arctic-shift", "state": sh.DOWN},
                           {"name": "muse", "state": sh.OK}]}
    for st in sh.STATES:
        summary[st] = sum(1 for r in summary["results"] if r["state"] == st)
    probed = run.build_coverage([], [], [], [], [], {}, [], {"source_health": summary})
    assert "source_health" not in probed["unmeasured"]
    seg = dg.source_health_segment(probed)
    assert dg._HEALTH_ALARM in seg and "brightdata" in seg and "arctic-shift" in seg
    # and the same block renders through sourcehealth's own coverage_block helper
    assert dg._HEALTH_ALARM in dg.source_health_segment(
        _cov(source_health=sh.coverage_block(summary)))


# ================================================== 5. an archived record has a score, not a None
ARCHIVED = {"title": "归档记录", "score": 77.45, "grade": "B+", "track": "saas-niche",
            "side": "demand", "independent_source_count": 3, "score_breakdown": {"timing": 80},
            "source_set": ["news", "reddit"],
            "evidence": [{"source": "news", "url": "https://www.example-news.com/2026/08/story",
                          "signal": "报道"}]}


def test_archived_record_renders_its_real_score_not_none():
    assert dg.card_score(ARCHIVED) == 77.45
    md = dg.build_markdown([ARCHIVED], _cov(), "2026-08-29")
    head = dg.build_headlines([ARCHIVED], _cov(), date="2026-08-29")
    text = pc.render_text(ARCHIVED)
    for art in (md, head, text):
        assert "77.45" in art
        assert "None" not in art
    assert "score None" not in pc.build_embed(ARCHIVED)["footer"]["text"]
    assert "77.45" in pc.build_embed(ARCHIVED)["footer"]["text"]


def test_archived_records_rank_by_their_real_score():
    lo = dict(ARCHIVED, title="低分归档", score=60.0)
    hi = dict(ARCHIVED, title="高分归档", score=90.0)
    head = dg.build_headlines([lo, hi], _cov(), date="2026-08-29")
    assert head.index("高分归档") < head.index("低分归档")
    md = dg.build_markdown([lo, hi], _cov(), "2026-08-29")
    assert md.index("高分归档") < md.index("低分归档")


def test_final_score_wins_when_both_keys_are_present():
    both = dict(ARCHIVED, score=1.0, final_score=99.0)
    assert dg.card_score(both) == 99.0
    assert dg.score_text(both) == "99"


def test_a_card_with_no_score_at_all_renders_the_marker_never_the_literal_none():
    naked = {k: v for k, v in ARCHIVED.items() if k != "score"}
    assert dg.card_score(naked) is None
    assert dg.score_text(naked) == dg._COV_UNKNOWN
    for art in (dg.build_markdown([naked], _cov(), "2026-08-29"),
                dg.build_headlines([naked], _cov(), date="2026-08-29"),
                pc.render_text(naked)):
        assert "None" not in art and dg._COV_UNKNOWN in art


def test_a_real_zero_score_prints_as_zero():
    # NEGATIVE CONTROL: reading two keys must not degrade into "anything falsy is unknown".
    assert dg.score_text({"final_score": 0}) == "0"
    assert dg.score_text({"score": 0.0}) == "0"
    assert dg._COV_UNKNOWN not in dg.score_text({"final_score": 0})


def test_long_scores_are_trimmed_but_never_rounded_away():
    assert dg.score_text({"final_score": 83.7812}) == "83.78"
    assert dg.score_text({"final_score": 79.7}) == "79.7"
    assert dg.score_text({"final_score": 60.0}) == "60"


def test_a_garbled_score_does_not_crash_the_render():
    weird = dict(ARCHIVED, score="不是数字")
    assert dg.card_score(weird) is None
    assert "None" not in dg.build_headlines([weird], _cov(), date="2026-08-29")


# =========================================================== 6. the day reads faster than it did
def test_an_empty_demand_column_is_loud_and_the_supply_tail_is_disclaimed():
    out = dg.build_headlines([_card(title="供给热点一条", side="supply")], _cov(),
                             date="2026-08-29")
    assert "🈳" in out and "今日需求侧 0 条" in out
    assert "不要当需求读" in out                       # supply is never read as the answer
    assert out.index("需求机会") < out.index("供给热点")


def test_an_empty_supply_column_still_prints_its_header():
    out = dg.build_headlines([_card()], _cov(), date="2026-08-29")
    assert "📈 **供给热点**" in out and "今日无供给侧热点" in out


def test_a_demand_pain_quote_reaches_the_pushed_message_labeled_as_a_quote():
    pain = "「每个月手工誊 400 张单子，错一张就得整批重做。」某从业者 2026-08-01 发帖"
    out = dg.build_headlines([_card(pain_evidence=pain, summary="一段概述")], _cov(),
                             date="2026-08-29")
    assert "💬 痛点:" in out and "手工誊 400 张单子" in out


def test_without_a_quote_the_label_is_absent_so_the_label_always_means_quoted():
    # NEGATIVE CONTROL: if the label were printed unconditionally it would stop carrying meaning.
    out = dg.build_headlines([_card(summary="这是一段概述，不是引语。")], _cov(),
                             date="2026-08-29")
    assert "💬 痛点:" not in out
    assert "这是一段概述，不是引语。" in out           # the prose fallback still ships


def test_the_ranking_meta_shows_the_lanes_behind_the_source_count():
    # "4 独立源" cannot tell four outlets from one lane repeating itself; the lanes can.
    one_lane = _card(evidence=[{"source": "web", "url": "https://www.example-news.com/a/b"},
                               {"source": "web", "url": "https://www.example-news.com/c/d"}],
                     independent_source_count=4)
    many = _card(title="多源卡", evidence=[
        {"source": "reddit", "url": "https://www.example-news.com/a/b"},
        {"source": "news", "url": "https://www.example-news.com/c/d"}], independent_source_count=3)
    assert "4 独立源 [web]" in dg.build_headlines([one_lane], _cov(), date="2026-08-29")
    assert "3 独立源 [news, reddit]" in dg.build_headlines([many], _cov(), date="2026-08-29")


def test_lanes_fall_back_to_source_set_for_an_archived_record_without_evidence():
    rec = {k: v for k, v in ARCHIVED.items() if k != "evidence"}
    assert "[news, reddit]" in dg.build_headlines([rec], _cov(), date="2026-08-29")


def test_crowdedness_is_printed_with_the_band_it_falls_in():
    assert dg._crowd_text({"crowdedness": 15}) == "拥挤度 15 蓝海"
    assert dg._crowd_text({"crowdedness": 30}) == "拥挤度 30 偏蓝"
    assert dg._crowd_text({"crowdedness": 55}) == "拥挤度 55 偏挤"
    assert dg._crowd_text({"crowdedness": 80}) == "拥挤度 80 红海"
    assert dg._crowd_text({}) == "" and dg._crowd_text({"crowdedness": "x"}) == ""
    out = dg.build_headlines([_card(crowdedness=30)], _cov(), date="2026-08-29")
    assert "拥挤度 30 偏蓝" in out


def test_a_supply_card_shows_no_crowdedness():
    # NEGATIVE CONTROL for the band renderer: crowdedness is demand only, an invented one would lie.
    out = dg.build_headlines([_card(side="supply")], _cov(), date="2026-08-29")
    assert "拥挤度" not in out


# ======================================================= 7. the 2000 char limit never eats a tail
def _nonspace(s):
    return re.sub(r"\s+", "", s)


def _real_shaped_message():
    cards = []
    for i in range(6):
        cards.append(_card(
            title=f"需求卡片 {i}：一个足够长的中文标题，用来把这条消息推过 Discord 的两千字上限",
            score=90 - i, crowdedness=20 + i * 7,
            pain_evidence="「" + f"第 {i} 条痛点引语，" * 12 + "」某从业者 2026-08-01 发帖",
            evidence=[{"source": "web", "url": f"https://www.example-news.com/2026/08/story-{i}",
                       "signal": "官方公告"}]))
        cards.append(_card(title=f"供给热点 {i}：另一条同样不短的标题", side="supply", score=70 - i))
    return dg.build_headlines(cards, _cov(source_health=DOWN), date="2026-08-29", cap=6)


def test_a_real_shaped_day_exceeds_the_limit_and_splits_without_losing_a_title():
    msg = _real_shaped_message()
    assert len(msg) > pc.CONTENT_MAX                      # the premise: this day really is too long
    chunks, hard_cut = pc.split_for_discord(msg, pc.CONTENT_MAX)
    assert len(chunks) > 1 and hard_cut == 0
    assert all(len(c) <= pc.CONTENT_MAX - 40 for c in chunks)   # room left for the 第 i/n 段 marker
    joined = "".join(chunks)
    for i in range(6):
        assert f"需求卡片 {i}" in joined and f"供给热点 {i}" in joined
    assert _nonspace(joined) == _nonspace(msg)           # not one visible character was dropped
    assert "arctic-shift" in joined                      # and the alarm rode along


def test_an_all_blank_over_length_message_keeps_its_tail():
    # the splitter used to decide "a chunk is open" by the accumulator being truthy, so a message
    # whose pieces are all empty strings produced NO chunks and fell through to a fallback that
    # hard truncated it while reporting zero dropped characters.
    msg = "\n\n" * 2000
    chunks, hard_cut = pc.split_for_discord(msg, pc.CONTENT_MAX)
    kept = sum(len(c) for c in chunks)
    assert kept >= len(msg) - 2 * (len(chunks) - 1)      # only the split separators are consumed
    assert kept > pc.CONTENT_MAX                          # i.e. NOT one truncated copy
    assert hard_cut == 0


def test_an_over_long_single_line_is_cut_visibly_never_silently():
    msg = "x" * 5000
    chunks, hard_cut = pc.split_for_discord(msg, pc.CONTENT_MAX)
    assert hard_cut > 0                                   # reported to the caller
    assert "已截断" in "".join(chunks)                    # and reported inside the delivered text


def test_deliver_dry_run_reports_the_split_instead_of_hiding_it():
    ok, detail = pc.deliver(_real_shaped_message(), dry_run=True)
    assert ok and "分" in detail and "段" in detail
