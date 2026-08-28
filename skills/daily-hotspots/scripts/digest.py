#!/usr/bin/env python3
"""Daily digest builder + idempotent registration (Acceptance Gate T5).

Aggregates the day's archivable cards into ONE human-readable markdown (the same artifact is both
delivered to Discord and committed to archive/digests/YYYY/YYYY-MM-DD.md), with a coverage header
line up top so "comprehensive" is verifiable, not asserted. On an empty day it writes an honest
"今日无合格机会" digest, never filler.

The digest itself is a schedule-reminder idempotent item (idempotency_key=daily-hotspots:digest:
<date>) so a re-run / catch-up never double-sends. Registration goes through dedup.LedgerClient.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from datetime import timedelta

from lib import age_hours, freshness, iso, load_config, now_utc, parse_ts
from archive import resolve_archive_dir

# Bound the overslept-machine backfill so a months-asleep laptop never floods the channel with
# hundreds of digests; we emit at-least-once for the most RECENT N missed days (today always
# included). 30 mirrors the samples ring-buffer cap (lib DEFAULT_CONFIG.scoring.samples_cap).
CATCHUP_CAP = 30


def missed_digest_dates(last_run, now=None, cap: int = CATCHUP_CAP,
                        tz_offset_h: float = 0.0) -> list[str]:
    """Enumerate the local calendar dates whose digest was missed since the last watermark (R5).

    Pure function (no DB, no network): returns the dates strictly AFTER the last covered date,
    through today inclusive, in ascending order. Properties the catch-up relies on:

      * normal daily run (watermark = yesterday)   -> exactly [today]      (one slot)
      * overslept N days (watermark = today-N)      -> [today-N+1 .. today] (backfill)
      * same-day re-run (watermark on today)        -> []                   (dedupe: never re-emit)
      * cold start (last_run None/"")               -> [today]              (no epoch storm)
      * long outage / future skew                   -> bounded to the most-recent `cap` dates,
                                                       today always present, never negative.

    `tz_offset_h` shifts UTC to the configured push timezone so the date boundary follows local
    midnight rather than naive UTC slicing.
    """
    off = timedelta(hours=float(tz_offset_h))
    now_dt = (parse_ts(now) if now else now_utc()) + off
    today = now_dt.date()

    if not last_run:
        return [today.isoformat()]            # cold start: just today, bounded

    try:
        last_date = (parse_ts(last_run) + off).date()
    except Exception:
        return [today.isoformat()]

    if last_date >= today:                     # same-day re-run OR future-skew watermark
        return []

    # dates strictly after the last covered date, through today, capped to the most recent `cap`
    start = max(last_date + timedelta(days=1), today - timedelta(days=max(0, int(cap)) - 1))
    out, d = [], start
    while d <= today:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def catch_up_digests(ledger, last_run, now=None, cap: int = CATCHUP_CAP,
                     tz_offset_h: float = 0.0) -> list[str]:
    """Idempotent backfill of missed daily digests (R5: at-least-once + dedupe).

    For each missed calendar date, register the per-date idempotent digest item; the base's
    UPSERT on idempotency_key `daily-hotspots:digest:<date>` guarantees a re-run never creates a
    duplicate. Returns the list of dates ensured (observability). No missed date => no-op.
    """
    dates = missed_digest_dates(last_run, now=now, cap=cap, tz_offset_h=tz_offset_h)
    for d in dates:
        try:
            register_digest_item(ledger, date=d, summary="catch-up")
        except Exception:
            pass
    return dates


# ============================================================================
# Track 2, community pulse (source-coverage design §7). The dual-track SPLIT
# (which candidate becomes a scored card vs a single-origin pulse item) is the
# pipeline's job; THIS is the renderer for the pulse lane: a separate lightweight
# `## 社区脉搏` section, rendered AFTER the opportunity cards, that surfaces
# single-origin community rumors as link-only + one-line-why, explicitly with
# NO score and NO deep-dive, so a rumor is never dressed up as a scored
# opportunity. Ranked by freshness + community heat, capped by
# community_pulse.max_per_day, and deduped (within the day and, via seen_keys,
# across days, reusing the same no-re-bubble intent as the card dedup, §7).
# Pure/deterministic (clock only via now_utc's env seam); no network.
# ============================================================================

_DEFAULT_PULSE_LABEL = "⚠️ 单源未验证 · 社区小道消息"
_PULSE_HEAT_K = 25.0   # heat half-saturation: heat/(heat+K) -> a bounded [0,1) heat term

# An untrusted community title / url / signal is DATA (§10): a collected RSS or V2EX title can carry
# an embedded newline followed by markdown ("topic\n## A 99, buy this now") that would open a NEW
# markdown block, a fabricated top-level heading / a broken bullet list, inside the pushed digest.
# _inline flattens any such field to a single safe inline span before it is placed in the markdown:
# ALL whitespace (newlines included) collapses to one space, so nothing can reach column 0 to start
# a block, and the two metacharacters that would break the surrounding bullet's bold/code-span
# (backtick, pipe) are neutralized. The why-line is whitespace-collapsed by _pulse_oneliner too.
_MD_INLINE_NEUTRALIZE = {ord("`"): "'", ord("|"): "/"}


def _inline(s) -> str:
    """Flatten an untrusted string to one injection-safe inline markdown span (§10 data-not-code).
    Also normalizes en/em/bar dashes to a comma so an LLM-supplied field can never put a dash into
    the pushed digest (house rule: published prose carries no en/em dash). The dashes are written as
    \\u escapes so this source file itself stays dash-free."""
    s = re.sub(r"\s+", " ", str(s if s is not None else "")).strip()
    s = re.sub(r"\s*[\u2013\u2014\u2015]+\s*", ", ", s)  # en/em/bar dash -> comma
    return s.translate(_MD_INLINE_NEUTRALIZE)


def _pulse_key(item: dict) -> str:
    """Cross-post-stable dedup key for a pulse item: canonicalized URL (fragment/query stripped),
    falling back to a whitespace-normalized lowercased title. Empty when the item has neither ,
    such an unattributable item is skipped rather than rendered as a bare bullet."""
    url = (item.get("url") or "").strip().lower()
    if url:
        url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if url:
            return "u:" + url
    title = re.sub(r"\s+", " ", (item.get("title") or "").strip().lower())
    return ("t:" + title) if title else ""


# --- cross-day pulse dedup state (§7): a bounded {pulse_key: last_shown_iso} map persisted as a
# schedule-reminder singleton (dedup.LedgerClient get/set_pulse_seen) exactly like the watermark, so
# a single-source community rumor rendered today is suppressed on later days (it never re-bubbles)
# until a 2nd independent origin escalates it to a scored card. These two helpers are the PURE
# read/write transforms run.process wires around that singleton; the retention window keeps the map
# bounded (a rumor that stopped re-collecting long ago must not suppress a fresh collision forever).
_DEFAULT_PULSE_SEEN_RETENTION_DAYS = 14


def _seen_retention_days(cfg) -> float:
    cp = ((cfg or {}).get("community_pulse") or {})
    try:
        v = float(cp.get("dedup_retention_days", _DEFAULT_PULSE_SEEN_RETENTION_DAYS))
        return v if v > 0 else _DEFAULT_PULSE_SEEN_RETENTION_DAYS
    except (TypeError, ValueError):
        return _DEFAULT_PULSE_SEEN_RETENTION_DAYS


def _try_parse_ts(ts):
    try:
        return parse_ts(ts)
    except Exception:
        return None


def active_pulse_seen_keys(seen_map, now=None, cfg=None) -> set:
    """The set of still-in-window pulse keys, the cross-day dedup input handed to build_markdown /
    render_community_pulse. Anything older than the retention window has aged out. Pure."""
    now = now or now_utc()
    cutoff = now - timedelta(days=_seen_retention_days(cfg))
    out = set()
    for k, ts in (seen_map or {}).items():
        d = _try_parse_ts(ts)
        if k and d is not None and d >= cutoff:
            out.add(k)
    return out


def merge_pulse_seen(prior_map, pulse_items, now=None, cfg=None) -> dict:
    """Fold this run's rumor keys into the cross-day seen map (stamped ``now``), dropping entries
    older than the retention window so the persisted singleton stays bounded. Pure."""
    now = now or now_utc()
    cutoff = now - timedelta(days=_seen_retention_days(cfg))
    out = {}
    for k, ts in (prior_map or {}).items():
        d = _try_parse_ts(ts)
        if k and d is not None and d >= cutoff:
            out[k] = ts
    now_iso = iso(now)
    for it in (pulse_items or []):
        if isinstance(it, dict):
            k = _pulse_key(it)
            if k:
                out[k] = now_iso
    return out


def _pulse_ts_ord(item: dict) -> float:
    ts = item.get("ts") or ""
    if not ts:
        return 0.0
    try:
        return parse_ts(ts).timestamp()
    except Exception:
        return 0.0


def _pulse_rank(item: dict, half_life_h: float, gravity: float, ref) -> float:
    """Composite rank = freshness (exponential half-life on the item ts) + a bounded community-heat
    term. Both live in [0,1]-ish so neither swamps the other; a missing ts gets a neutral 0.5 so a
    fresh-but-undated item is not unfairly buried, and a missing/garbled heat contributes 0."""
    ts = item.get("ts") or ""
    try:
        fresh = freshness(age_hours(ts, ref), half_life_h, gravity) if ts else 0.5
    except Exception:
        fresh = 0.5
    heat = item.get("heat")
    try:
        heat = max(0.0, float(heat)) if heat is not None else 0.0
    except (TypeError, ValueError):
        heat = 0.0
    return fresh + heat / (heat + _PULSE_HEAT_K)


def _pulse_oneliner(item: dict) -> str:
    """The single why-interesting line: prefer the collector's `signal` (e.g. "42 replies · geek"),
    else a short flattened summary/text. NEVER a score, a pulse item carries no scored dimension."""
    why = re.sub(r"\s+", " ", (item.get("signal") or item.get("why") or "")).strip()
    if not why:
        txt = (item.get("text") or item.get("summary") or "").strip()
        why = re.sub(r"\s+", " ", txt)[:120]
    return why


def select_rendered_pulse(pulse_items: list[dict] | None, cfg: dict | None = None,
                          seen_keys=None) -> list:
    """The pulse items ``render_community_pulse`` would ACTUALLY render this run, in render order ,
    after the enabled/cap gate and within-day + cross-day (``seen_keys``) dedup, truncated to
    ``community_pulse.max_per_day``.

    This is the single source of truth for "which rumors were shown today". run.process stamps the
    cross-day seen map from THIS list, never the full pre-cap candidate list: the daily cap DEFERS
    overflow rumors to a later day (they are re-ranked next run), so marking an un-shown item as
    "seen" would silently DROP genuine community signal the cap was only meant to postpone, the §7
    "no re-bubble" rule must never become "no show". Pure/deterministic (clock only via now_utc's
    env seam); returns [] when nothing survives the gate/dedup/cap."""
    if not pulse_items:
        return []
    cfg = cfg if cfg is not None else load_config()
    cp = cfg.get("community_pulse") or {}
    if cp.get("enabled") is False:
        return []
    try:
        cap = int(cp.get("max_per_day", 8))
    except (TypeError, ValueError):
        cap = 8
    cap = max(0, cap)
    if cap == 0:
        return []

    sc = cfg.get("scoring") or {}
    half = float(sc.get("freshness_half_life_h", 72) or 72)
    grav = float(sc.get("freshness_gravity", 1.8) or 1.8)
    ref = now_utc()

    seen = set(seen_keys or ())
    ranked = []
    for it in pulse_items:
        if not isinstance(it, dict):
            continue
        k = _pulse_key(it)
        if not k or k in seen:
            continue          # unattributable, or already surfaced (within-day or cross-day)
        seen.add(k)
        ranked.append((_pulse_rank(it, half, grav, ref), _pulse_ts_ord(it), it))
    # highest rank first; break ties by fresher ts, then title (stable + deterministic)
    ranked.sort(key=lambda t: (-t[0], -t[1], (t[2].get("title") or "")))
    return [it for _rank, _ord, it in ranked[:cap]]


def render_community_pulse(pulse_items: list[dict] | None, cfg: dict | None = None,
                           seen_keys=None) -> str:
    """Render the `## 社区脉搏` section (design §7). Returns "" when there is nothing to show, so a
    caller can unconditionally append it.

    - Labeled from `community_pulse.label` (default "⚠️ 单源未验证 · 社区小道消息").
    - Each surviving item: title + source + link + one-line why. NO score, NO deep-dive.
    - Capped at `community_pulse.max_per_day` (default 8), ranked by freshness + community heat.
    - Deduped within the batch by canonical URL/title; `seen_keys` (a set from prior days) removes
      anything already surfaced so a rumor never re-bubbles across days (§7).
    - `community_pulse.enabled: false` suppresses the section entirely.

    The item SELECTION (gate + dedup + rank + cap) is delegated to ``select_rendered_pulse`` so the
    exact set of rendered rumors is one source of truth, shared with run.process's cross-day-dedup
    write-back (which must stamp ONLY the rumors actually shown, never the full pre-cap list)."""
    if not pulse_items:
        return ""
    cfg = cfg if cfg is not None else load_config()
    cp = cfg.get("community_pulse") or {}
    if cp.get("enabled") is False:
        return ""
    label = cp.get("label") or _DEFAULT_PULSE_LABEL
    chosen = select_rendered_pulse(pulse_items, cfg=cfg, seen_keys=seen_keys)
    if not chosen:
        return ""

    lines = ["## 社区脉搏", "", f"> {label}", ""]
    for it in chosen:
        # every field below is untrusted DATA -> flatten to a safe inline span (§10, no block injection)
        title = _inline(it.get("title")) or "(无标题)"
        src = _inline(it.get("origin_source") or it.get("source") or it.get("origin")) or "?"
        url = _inline(it.get("url"))
        head = f"- **{title}**, `{src}`"
        if url:
            head += f" · {url}"
        lines.append(head)
        # §10: the why-line derives from the untrusted collector `signal` (a V2EX node / linux.do
        # category label, e.g. "42 replies · geek"), so it must get the SAME neutralization as
        # title/src/url (backtick->apostrophe, pipe->slash, whitespace-flatten via _inline), not just
        # the whitespace-collapse _pulse_oneliner applies. Otherwise a crafted category like
        # "geek`code`" opens an inline-code span across the bullet, or "云计算|promo" injects a table
        # delimiter, into the pushed digest markdown (audit HARDEN round 2).
        why = _inline(_pulse_oneliner(it))
        if why:
            lines.append(f"  - {why}")
    lines.append("")
    return "\n".join(lines)


def _render_card(c: dict, pool=None) -> list:
    """The markdown lines for ONE opportunity card. Every field COPIED from a collected source
    (title, machine_type, evidence source/url/signal, track, why_now/contrarian/action) is untrusted
    DATA and is flattened to a safe inline span (no block injection, no en/em dash: see _inline).
    grade / score / dims / source-count are engine-computed, not copied.

    The card's own 链接 line comes from choose_card_links against the SHARED batch `pool`, the same
    call build_headlines makes, so the archive and the push can never point at different urls for
    the same card. Rejected urls are printed with their reason: a card that lost its link says so
    instead of quietly linking somewhere else."""
    bd = c.get("score_breakdown", {})
    dims = " ".join(f"{k}={round(float(v))}" for k, v in bd.items())
    srcs = ", ".join(sorted(set(_inline(e.get("source")) or "?" for e in c.get("evidence", []))))
    mtypes = ",".join(_inline(t) for t in c.get("machine_type", []))
    title = _inline(c.get("title")) or "?"
    crowd = c.get("crowdedness")
    meta2 = f"- track: `{_inline(c.get('track'))}` | types: {mtypes}" \
            f" | {c.get('independent_source_count', 0)} 独立源 [{srcs}]"
    if crowd is not None:
        meta2 += f" | 拥挤度 {round(float(crowd))}"
    out = [f"## {c.get('grade')} {c.get('final_score')}, {title}", meta2, f"- dims: {dims}"]
    links = choose_card_links(c, pool)
    if links["primary"]:
        line = f"- 链接: {links['primary']}"
        if links["discussion"]:
            line += f" · {_AGGREGATOR_LABEL}: {links['discussion']}"
        out.append(line)
    elif links["considered"]:
        out.append("- 链接: (无可用链接，全部候选被拒收)")
    for r in links["rejected"]:
        out.append(f"- ⚠️ 拒收链接: {r['url'] or '(无效 url，未回显)'} ({r['why']})")
    if c.get("pain_evidence"):
        out.append(f"- 痛点: {_inline(c['pain_evidence'])}")
    if c.get("why_now"):
        out.append(f"- why-now: {_inline(c['why_now'])}")
    if c.get("contrarian_insight"):
        out.append(f"- 非共识: {_inline(c['contrarian_insight'])}")
    if c.get("action"):
        out.append(f"- 行动: {_inline(c['action'])}")
    if c.get("delegated_deepdive"):
        out.append(f"- deep-dive: {_inline(c['delegated_deepdive'])}")
    for e in c.get("evidence", [])[:4]:
        # a url that is not a single clean http(s) token is junk or an injection attempt: print a
        # marker, never the payload (the flattened text still reached the archive before this).
        eu = _clean_url(e.get("url", "")) or ("(无效链接)" if str(e.get("url") or "").strip() else "")
        out.append(f"  - {_inline(e.get('source')) or '?'}: {eu} "
                   f"({_inline(e.get('signal'))})")
    out.append("")
    return out


# --- coverage rendering (contract) ------------------------------------------------------------
# The digest's top line is the run's only public claim of comprehensiveness, so "clean" and "nobody
# counted" must never look the same. A real integer renders as the integer; a missing key or the
# legacy "(see SKILL run)" placeholder (which shipped in 30 of 31 archived digests) renders as
# 未统计, which is visibly NOT a zero.
_COV_UNKNOWN = "未统计"


def _cov_unmeasured(coverage: dict) -> set:
    """The fields run.py's build_coverage says were NOT observed this run.

    The producer reports an unobserved field as a placeholder 0 PLUS its name in
    ``coverage["unmeasured"]``; without honoring that list the renderer would print 信号 0, which
    reads as "the collection layer found nothing" when the truth is "nobody counted". Absent or
    malformed list -> no claim of unmeasuredness (the numbers then stand on their own).
    """
    u = (coverage or {}).get("unmeasured")
    if isinstance(u, (list, tuple, set)):
        return {str(k) for k in u}
    return set()


def _cov_int(coverage: dict, key: str) -> str:
    if key in _cov_unmeasured(coverage):
        return _COV_UNKNOWN
    v = (coverage or {}).get(key, None)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _COV_UNKNOWN
    return str(int(v))


def _cov_len(coverage: dict, key: str) -> str:
    """A count for a key the contract carries as a LIST (below_floor, sources_failed). An int is
    accepted as the count itself; anything else is 未统计, never a silent 0. A key named in
    ``unmeasured`` is 未统计 regardless of the placeholder value the producer put there."""
    if key in _cov_unmeasured(coverage):
        return _COV_UNKNOWN
    v = (coverage or {}).get(key, None)
    if isinstance(v, (list, tuple, set, dict)):
        return str(len(v))
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return _COV_UNKNOWN


def coverage_line(coverage: dict | None, qualified: int) -> str:
    """The one-line coverage summary shared by the archive digest and the pushed headlines."""
    coverage = coverage or {}
    return (f"覆盖: 信号 {_cov_int(coverage, 'signals_collected')}"
            f" · 源 {_cov_int(coverage, 'sources_invoked')}/{_cov_int(coverage, 'sources_available')}"
            f" · 失败源 {_cov_len(coverage, 'sources_failed')}"
            f" · 候选 {_cov_int(coverage, 'candidates')} · 合格 {qualified}"
            f" · 去重抑制 {_cov_int(coverage, 'suppressed')}"
            f" · 未达门槛 {_cov_len(coverage, 'below_floor')}"
            f" · 未归因信号 {_cov_int(coverage, 'signals_unaccounted')}")


def _render_dropped(coverage: dict | None) -> list:
    """What the run BOUND or DROPPED, spelled out under the coverage line (house rule: a bound and
    the items it dropped are reported, never silent). Empty lists render nothing."""
    coverage = coverage or {}
    out = []
    failed = coverage.get("sources_failed")
    if isinstance(failed, (list, tuple)) and failed:
        out += ["## 失败源", ""]
        for f in failed:
            if isinstance(f, dict):
                out.append(f"- `{_inline(f.get('source')) or '?'}`: {_inline(f.get('error'))}")
            else:
                out.append(f"- {_inline(f)}")
        out.append("")
    floor = coverage.get("below_floor")
    if isinstance(floor, (list, tuple)) and floor:
        out += ["## 未达分数门槛 (通过 schema, 分数不够, 未归档)", ""]
        for b in floor:
            if isinstance(b, dict):
                out.append(f"- {_inline(b.get('title')) or '?'} "
                           f"({_inline(b.get('side'))}, {b.get('final_score')} < {b.get('floor')})")
            else:
                out.append(f"- {_inline(b)}")
        out.append("")
    return out


def build_markdown(cards: list[dict], coverage: dict | None = None,
                   date: str | None = None, pulse: list[dict] | None = None,
                   cfg: dict | None = None, seen_keys=None) -> str:
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    pool = url_pool(cards)
    cov = (f"> {coverage_line(coverage, len(cards or []))}"
           f" · 推送 {_cov_int(coverage, 'pushed')} · 深挖 {_cov_int(coverage, 'deepdived')}"
           f" · gen {iso(now_utc())}")
    lines = [f"# Daily Hotspots, {date}", "", cov, ""]
    if not cards:
        lines += ["**今日无合格机会** (no opportunity cleared the >=2-source + score floor).",
                  "诚实空日，非灌水。", ""]
    else:
        # Two-column model: DEMAND (quality, non-consensus) first, then SUPPLY (basic hotspots). A
        # card without an explicit side counts as supply (backward compatible). Each column is ranked
        # independently and shows its own honest-empty line so a weak demand day is never padded.
        by_side = {"demand": [], "supply": []}
        for c in cards:
            by_side["demand" if str(c.get("side", "supply")).strip().lower() == "demand"
                    else "supply"].append(c)
        for side_key, header, empty_msg in (
            ("demand", "## 🎯 需求机会 (demand, 高质量/非共识)",
             "**今日无合格需求机会** (no demand cleared the higher bar). 诚实空日，非灌水。"),
            ("supply", "## 📈 供给热点 (supply, 基础广度)",
             "**今日无供给侧热点。**"),
        ):
            scards = sorted(by_side[side_key], key=lambda c: -float(c.get("final_score", 0)))
            lines += [header, ""]
            if not scards:
                lines += [empty_msg, ""]
                continue
            for c in scards:
                lines.extend(_render_card(c, pool))
    # Track 2 (§7): the community-pulse section renders AFTER the cards, and still appears on an
    # otherwise-empty card day (a rumor-only day is not "no signal"). Empty pulse -> "" -> no-op.
    # seen_keys carries the cross-day-shown rumor keys so a rumor never re-bubbles (§7).
    pulse_md = render_community_pulse(pulse, cfg=cfg, seen_keys=seen_keys)
    if pulse_md:
        lines.append(pulse_md.rstrip("\n"))
        lines.append("")
    # what this run bound or dropped (failed sources, cards that missed their score floor)
    lines.extend(_render_dropped(coverage))
    return "\n".join(lines)


def _clean_url(u: str) -> str:
    """Return a single clean http(s) token or '', a url with whitespace/newline/angle brackets is
    untrusted junk (or an injection attempt) and is dropped rather than emitted."""
    u = (u or "").strip()
    if u.startswith(("http://", "https://")) and not any(ch in u for ch in " \t\r\n<>"):
        return u
    return ""


# ============================================================================
# LINK CHOOSER (operator bug 2026-08-27: "discord 推送的网址和标题根本对不上")
#
# `evidence` arrives ordered by COLLECTION SOURCE (hackernews first, then
# twitterapi, then feeds), never by relevance to the headline, so taking
# evidence[0] blindly linked a card titled "亚马逊 9 月 30 日关掉 Mechanical
# Turk" to https://www.mturk.com/ (the bare product homepage the HN submitter
# posted) and linked the HF incident card to the HN comments page instead of
# the report its headline describes.
#
# Two independent layers, both pure and deterministic:
#   RANK      order the candidates by how well each one can BE the headline
#             (article-shaped path > site root, primary source > aggregator
#             comments page, then token overlap with the title).
#   VALIDATE  a shape-only check cannot see a bare homepage, a path that got
#             cut off mid-slug, or an invented tweet id; validate_url can, and
#             every rejection is REPORTED (never a silent swap), so a card that
#             lost its link says so in both the archive and the push.
# ============================================================================

# an id-carrying discussion page ABOUT the news, not the news itself
_AGGREGATOR_LABEL = "讨论"
_SOCIAL_HOSTS = ("x.com", "twitter.com")
# accounts that only re-publish other outlets' headlines: their post is an aggregator too
_AGGREGATOR_ACCOUNTS = ("techmeme", "hackernewsbot", "newsycombinator")

_REJECT_REASONS = {
    "malformed": "不是单个干净的 http(s) 链接",
    "site_root": "只是站点首页/根路径，不指向这条具体事件",
    "truncated": "路径被截断，同一批证据里有更长的同源链接",
    "fabricated_id": "社交状态 id 尾部是一长串零，不像真实 id",
}


def _split_url(u: str):
    """(host, path, query) lowercased host, for a url already through _clean_url. Never raises."""
    try:
        p = urlsplit(u)
    except ValueError:
        return ("", "", "")
    host = p.netloc.lower()
    if host.startswith("www."):           # prefix removal, NOT lstrip (which eats "web.archive.org")
        host = host[4:]
    return (host, p.path or "", p.query or "")


def _segments(path: str) -> list:
    return [s for s in (path or "").split("/") if s]


def _is_site_root(u: str) -> bool:
    """A bare domain or a site root: `https://www.mturk.com/`, `https://www.livemint.com/`."""
    _h, path, query = _split_url(u)
    return not _segments(path) and not query


def _is_article_shaped(u: str) -> bool:
    """A url that can plausibly BE one specific story: a deep path, a long slug, or an id query."""
    _h, path, query = _split_url(u)
    segs = _segments(path)
    if not segs:
        return False
    return len(segs) >= 2 or len(segs[0]) >= 8 or bool(query)


def _is_social(u: str) -> bool:
    host, _p, _q = _split_url(u)
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS)


def _is_aggregator(u: str) -> bool:
    """A comments/roundup page ABOUT the story (HN item, techmeme, reddit comments, a Techmeme
    tweet). Still worth keeping as the secondary 讨论 link, never as the headline's own link."""
    host, path, _q = _split_url(u)
    p = path.lower()
    if host.endswith("news.ycombinator.com"):
        return p.startswith("/item")
    if host.endswith("techmeme.com"):
        return True
    if host.endswith("reddit.com"):
        return "/comments/" in p
    if host.endswith("lobste.rs"):
        return p.startswith("/s/")
    if _is_social(u):
        segs = _segments(path)
        return bool(segs) and segs[0].lower() in _AGGREGATOR_ACCOUNTS
    return False


_TOKEN_STOP = {"the", "and", "for", "with", "from", "that", "this", "http", "https", "www",
               "com", "html", "index", "news", "story", "item", "post", "posts", "status"}


def _tokens(s: str) -> set:
    """Latin words (>=3 chars) + CJK bigrams, lowercased. Bigrams give Chinese titles a usable
    overlap signal without pulling in a segmenter (pure, offline, deterministic)."""
    s = (s or "").lower()
    out = {w for w in re.findall(r"[a-z0-9]{3,}", s) if w not in _TOKEN_STOP}
    cjk = re.findall("[\u4e00-\u9fff]+", s)
    for run in cjk:
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
        if len(run) == 1:
            out.add(run)
    return out


def _url_text(u: str) -> str:
    """The human-readable part of a url (slug words), for token overlap with the title."""
    _h, path, query = _split_url(u)
    return re.sub(r"[-_/+.?&=]+", " ", f"{path} {query}")


def _url_tier(url: str) -> int:
    """Which KIND of link this is. Lower is better. Kind decides; wording never overrules it.

    This is deliberately a hard ordering rather than a weighted sum. The first implementation scored
    these as additive penalties (aggregator -40, social -5) plus up to +9 of title/signal token
    overlap, which quietly broke its own stated guarantee that "a tweet loses to a real article when
    both are present": the overlap term could outbid the social penalty. It did, in production. On a
    card about an official incident report, the tweet won over the vendor's own report, because the
    Chinese card text was summarized FROM that tweet, so the tweet's blurb matched the headline
    almost word for word while the English report url matched nothing. Rewarding that is backwards.
    The blurb agreeing with the headline is evidence about the summarizer, not about the source.

    Tiers, best first:
      0  a real article or report from a first-party or press source
      1  some other non-aggregator, non-social page (a section index, a blog root with a path)
      2  a social post: a comment ON the news, usable when nothing better exists
      3  an aggregator thread: it is the discussion, kept separately as `discussion`
      4  a site root: it can never be one specific story (also rejected by validate_url)
    """
    if _is_site_root(url):
        return 4
    if _is_aggregator(url):
        return 3
    if _is_social(url):
        return 2
    return 0 if _is_article_shaped(url) else 1


def _rank_url(url: str, signal: str, title_tokens: set) -> tuple:
    """Sort key for "which link should carry this headline". Lower sorts better.

    Kind first (see `_url_tier`), then token overlap as a tiebreak WITHIN a kind, where it is a fair
    signal: given two press articles, the one whose blurb and slug share more with the headline is
    more likely to be the one the headline is about. Ties fall through to the caller's stable
    original evidence order, so the whole chooser stays deterministic.
    """
    overlap = min(3, len(title_tokens & _tokens(f"{signal} {_url_text(url)}")))
    return (_url_tier(url), -overlap)


def _is_truncated(url: str, pool) -> bool:
    """True when a sibling url EXTENDS this one mid-segment, i.e. this path got cut off.

    Real case: `.../nvidia-in-talks-to-buy-hugging-face-13` while the collector held
    `.../nvidia-in-talks-to-buy-hugging-face-13-billion-dollars-2026-8`. A sibling that continues
    with "/" is a legitimately deeper page under the same directory, not a truncation, and a path
    that already ends in "/" is a complete directory url.
    """
    host, path, query = _split_url(url)
    if query or not _segments(path) or path.endswith("/"):
        return False
    for other in (pool or ()):
        if other == url:
            continue
        ohost, opath, _oq = _split_url(other)
        if ohost != host or not opath.startswith(path) or len(opath) <= len(path):
            continue
        if opath[len(path)] != "/":
            return True
    return False


def _has_fabricated_id(url: str) -> bool:
    """A social status id that is a long number ending in an improbable run of zeros.

    Real: `https://x.com/ClementDelangue/status/2092931447644442635`.
    Invented by a model: `https://x.com/adcock_brett/status/2092656000000000000` (12 trailing
    zeros). Restricted to status-shaped social urls so a numeric CMS id is never flagged.
    """
    _h, path, _q = _split_url(url)
    if not (_is_social(url) or "/status/" in path.lower()):
        return False
    for seg in _segments(path):
        if seg.isdigit() and len(seg) >= 12:
            if len(seg) - len(seg.rstrip("0")) >= 5:
                return True
    return False


def validate_url(url: str, pool=None) -> str:
    """'' when the url may carry a card, else a reason code from _REJECT_REASONS.

    `pool` is the other known-good urls of the run (this card's siblings plus every other card's),
    which is what makes truncation detectable at all.
    """
    clean = _clean_url(url)
    if not clean:
        return "malformed"
    if _is_site_root(clean):
        return "site_root"
    if _is_truncated(clean, pool):
        return "truncated"
    if _has_fabricated_id(clean):
        return "fabricated_id"
    return ""


def url_pool(cards) -> set:
    """Every clean evidence url in the batch. Shared by all cards so a bare-domain or truncated
    url on one card is caught against the full url that a SIBLING CARD carries (the real
    2026-08-27 livemint case: one card had `https://www.livemint.com/`, another had the article)."""
    pool = set()
    for c in (cards or []):
        if not isinstance(c, dict):
            continue
        for e in (c.get("evidence") or []):
            if isinstance(e, dict):
                u = _clean_url(e.get("url", ""))
                if u:
                    pool.add(u)
    return pool


def choose_card_links(card: dict, pool=None) -> dict:
    """THE one place a card's link is decided. Pure, deterministic, no clock, no network.

    Returns:
        {"primary":    str, the headline link ("" when every candidate was rejected),
         "discussion": str, the aggregator/comments link kept as a secondary 讨论 link,
         "rejected":   [{"url": str ("" for malformed junk, never echoed), "reason": code,
                         "why": human Chinese reason}],
         "considered": int, evidence entries that carried a url at all,
         "accepted":   int, candidates that passed validation}

    Both build_markdown and build_headlines call this with the same pool over the same card list,
    so the archived markdown and the pushed headline can never disagree about a card's url.
    """
    evidence = [e for e in (card.get("evidence") or []) if isinstance(e, dict)]
    pool = set(pool or ()) | {u for u in (_clean_url(e.get("url", "")) for e in evidence) if u}
    title_tokens = _tokens(card.get("title") or "")

    ranked, rejected, considered, seen = [], [], 0, set()
    for idx, e in enumerate(evidence):
        raw = e.get("url", "")
        if not str(raw or "").strip():
            continue                      # evidence with no url at all is not a rejected url
        considered += 1
        reason = validate_url(raw, pool)
        if reason:
            clean = _clean_url(raw)       # malformed junk is reported WITHOUT echoing its payload
            rejected.append({"url": clean, "reason": reason,
                             "why": _REJECT_REASONS.get(reason, reason)})
            continue
        u = _clean_url(raw)
        if u in seen:
            continue
        seen.add(u)
        ranked.append((_rank_url(u, str(e.get("signal") or ""), title_tokens), idx, u))

    ranked.sort()                          # best rank first, original evidence order breaks ties
    primary = ranked[0][2] if ranked else ""
    discussion = ""
    if primary and not _is_aggregator(primary):
        for _r, _i, u in ranked:
            if u != primary and _is_aggregator(u):
                discussion = u
                break
    return {"primary": primary, "discussion": discussion, "rejected": rejected,
            "considered": considered, "accepted": len(ranked)}


def card_links(cards) -> list:
    """choose_card_links for a whole batch against ONE shared pool (same order as `cards`)."""
    pool = url_pool(cards)
    return [choose_card_links(c, pool) for c in (cards or [])]


def _primary_url(card: dict) -> str:
    """Back-compat single-card shim; prefer choose_card_links/card_links (they see the batch pool)."""
    return choose_card_links(card).get("primary", "")


def digest_github_url(digest_path: str | None) -> str:
    """Best-effort GitHub blob URL for a written digest file, derived from the repo's `origin`.

    Read-only (git config reads, no network) so it is safe in the deterministic run; returns '' if
    anything is missing and the caller simply omits the link. Handles both https and ssh-alias
    remotes (`https://github.com/o/r.git`, `git@daizedong:o/r.git`) -> `https://github.com/o/r`.
    """
    if not digest_path:
        return ""
    import re
    import subprocess
    try:
        p = Path(digest_path).resolve()

        def _git(*a):
            r = subprocess.run(["git", "-C", str(p.parent), *a],
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ""
        root = _git("rev-parse", "--show-toplevel")
        remote = _git("remote", "get-url", "origin")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "master"
        if not root or not remote:
            return ""
        m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", remote)
        if not m:
            return ""
        rel = p.relative_to(Path(root)).as_posix()
        return f"https://github.com/{m.group(1)}/blob/{branch}/{rel}"
    except Exception:
        return ""


# track (a supply-side tool category) -> a clean human DOMAIN label for the 【】 tag. A track not in
# the map falls back to its own inline-safe slug so nothing renders blank.
_TRACK_DOMAIN = {
    "ai-agents": "AI",
    "fintech-crypto": "金融/加密",
    "dev-tools": "开发工具",
    "saas-niche": "SaaS",
    "consumer-social": "消费社交",
    "hardware-iot": "硬件",
}


def _domain_label(track) -> str:
    return _TRACK_DOMAIN.get((track or "").strip().lower()) or (_inline(track) or "其他")


def _truncate_prose(s: str, cap: int) -> str:
    """Trim to <=cap chars at a SENTENCE boundary so a prose summary never ends mid-sentence."""
    s = (s or "").strip()
    if len(s) <= cap:
        return s
    cut = s[:cap]
    ends = [cut.rfind(p) + len(p) for p in ("。", "！", "？", "；", ". ", "! ", "? ") if p in cut]
    ends = [j for j in ends if j >= cap * 0.5]
    return cut[:max(ends)].strip() if ends else cut.rstrip() + "…"


def build_headlines(cards: list[dict], coverage: dict | None = None,
                    date: str | None = None, cap: int = 5, digest_url: str | None = None) -> str:
    """The PUSHED daily message: a ranked 'headlines' digest, not a message per card.

    Layout per item (bold headline line so the parts are easy to tell apart):
        **N.【领域】标题**
        <一段人话摘要, what it is + why it matters, sentence-boundary trimmed>
        🔗 <link>　·　grade score · N源
    The 领域 is the mapped human DOMAIN (AI / 金融/加密 / …), not the raw tool track. Links are
    wrapped in <...> so Discord shows them clickable WITHOUT a preview card (plus the relay's
    SUPPRESS_EMBEDS). `digest_url` (the day's full digest on GitHub, every field + all evidence
    links) is appended as a 完整版 footer. Every copied field is _inline-flattened (no block
    injection) and urls are validated to a single clean http(s) token. Empty -> honest short line.
    """
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    allc = cards or []
    demand = sorted([c for c in allc if str(c.get("side", "supply")).strip().lower() == "demand"],
                    key=lambda c: -float(c.get("final_score", 0)))
    supply = sorted([c for c in allc if str(c.get("side", "supply")).strip().lower() != "demand"],
                    key=lambda c: -float(c.get("final_score", 0)))
    cap = max(1, int(cap))
    dtop, stop = demand[:cap], supply[:cap]
    pool = url_pool(allc)
    header = (f"📰 **前沿机会头条** · {date}\n"
              f"需求机会 {len(demand)} · 供给热点 {len(supply)}\n"
              f"{coverage_line(coverage, len(allc))}")
    if not allc:
        return header + "\n\n今日无合格机会（诚实空日，非灌水；完整记录见 archive）。"
    lines = [header, ""]

    # DEMAND: the high-value column, full treatment (domain, prose, evidence link, crowdedness).
    lines.append("🎯 **需求机会**（高质量 / 非共识）")
    if not dtop:
        lines.append("今日需求侧无合格机会（诚实空日，非灌水）。")
    else:
        for i, c in enumerate(dtop, 1):
            title = _inline(c.get("title")) or "?"
            domain = _domain_label(c.get("track"))
            summ = _truncate_prose(_inline(c.get("pain_evidence")) or _inline(c.get("summary"))
                                   or _inline(c.get("why_now")) or "", 280)
            crowd = c.get("crowdedness")
            meta = f"{c.get('grade')} {c.get('final_score')} · {c.get('independent_source_count', 0)}源"
            if crowd is not None:
                meta += f" · 拥挤度 {round(float(crowd))}"
            links = choose_card_links(c, pool)
            url, disc = links["primary"], links["discussion"]
            lines.append(f"**{i}.【{domain}】{title}**")
            if summ:
                lines.append(summ)
            tail = f"🔗 <{url}>　·　{meta}" if url else meta
            if disc:
                tail += f"　·　{_AGGREGATOR_LABEL} <{disc}>"
            if not url and links["considered"]:
                # the card LOST its link; say so rather than shipping a headline that links nowhere
                tail += f"　·　⚠️ 链接被拒收({links['rejected'][0]['why']})"
            lines.append(tail)
            lines.append("")

    # SUPPLY: basic hotspots, a compact terse tail (breadth, awareness, not a pitch per item).
    if stop:
        lines.append("📈 **供给热点**（基础广度）")
        for c in stop:
            title = _inline(c.get("title")) or "?"
            domain = _domain_label(c.get("track"))
            links = choose_card_links(c, pool)
            url = links["primary"]
            tail = f"　<{url}>" if url else ""
            if not url and links["considered"]:
                tail = f"　⚠️ 链接被拒收({links['rejected'][0]['why']})"
            lines.append(f"· 【{domain}】{title}　({c.get('grade')} {c.get('final_score')}){tail}")
        lines.append("")

    # link hygiene is part of the push's honesty budget: if any evidence url was refused this run
    # (bare homepage / truncated path / invented status id) the pushed message says how many, and
    # the archive digest lists each one with its reason. Never a silent swap.
    n_rejected = sum(len(choose_card_links(c, pool)["rejected"]) for c in allc)
    if n_rejected:
        lines.append(f"⚠️ 本次有 {n_rejected} 条证据链接被拒收（首页/截断/疑似伪造），逐条原因见完整版。")
        lines.append("")

    du = _clean_url(digest_url or "")
    extra = (len(demand) - len(dtop)) + (len(supply) - len(stop))
    if du:
        note = f"　·　另有 {extra} 条见完整版" if extra > 0 else ""
        lines.append(f"📄 完整版（全部字段 + 证据链接）: <{du}>{note}")
    else:
        lines.append(f"另有 {extra} 条见当日 archive。" if extra > 0 else "完整卡片见当日 archive。")
    return "\n".join(lines)


EMPTY_DAY_MARK = "**今日无合格机会**"


class DigestClobberError(RuntimeError):
    """A same-day re-run tried to overwrite a real digest with the empty-day text."""


def digest_is_empty_day(markdown: str) -> bool:
    """True for the honest "今日无合格机会" digest build_markdown emits when nothing qualified."""
    return EMPTY_DAY_MARK in (markdown or "")


def write_digest_file(markdown: str, archive_dir: str | None = None,
                      date: str | None = None) -> Path:
    """Write the day's digest ATOMICALLY, refusing a same-day-rerun clobber. WRITER: hard-fails.

    Two guarantees, both learned from real damage:

      * ATOMIC. The content goes to a temp file in the SAME directory and is os.replace'd onto the
        final path, so a crash or a concurrent reader never sees a half-written digest, and the
        replace is a single rename on the archive filesystem.
      * NO SILENT CLOBBER. On 2026-08-27 the scheduled digest was overwritten by a manual re-run.
        If a digest for this date already exists with real content and the new content is the
        empty-day text, that is a re-run that collected nothing overwriting a run that collected
        something: raise DigestClobberError and keep the existing file. The caller must surface the
        failure, never swallow it, and never write the empty digest somewhere else instead.

    Every other failure (permission, disk, encoding) propagates too: this is the artifact, so a
    write that did not happen must not look like a write that did.
    """
    date = date or now_utc().date().isoformat()
    year = date[:4]
    base = resolve_archive_dir(archive_dir) / "digests" / year
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{date}.md"

    if path.exists():
        existing = path.read_text(encoding="utf-8")   # unreadable existing digest -> hard fail
        if digest_is_empty_day(markdown) and existing.strip() and not digest_is_empty_day(existing):
            raise DigestClobberError(
                f"refusing to overwrite {path} ({len(existing)} chars of real digest) with the "
                f"empty-day text: a same-day re-run must not erase a run that found cards")

    tmp = base / f".{date}.md.{os.getpid()}.tmp"
    try:
        tmp.write_text(markdown, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def register_digest_item(ledger, date: str | None = None, summary: str = "") -> dict:
    """Idempotent digest item via the base. Re-run with same date => same id (no double send)."""
    date = date or now_utc().date().isoformat()
    key = f"daily-hotspots:digest:{date}"
    ext = {"x_daily_hotspots_digest_date": date, "x_daily_hotspots_digest_summary": summary[:200]}
    args = ["--title", f"daily-hotspots digest {date}", "--kind", "task",
            "--source", "daily-hotspots", "--idempotency-key", key,
            "--ext", json.dumps(ext, ensure_ascii=False)]
    return ledger._run("add", args)


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    cards = data.get("cards", data if isinstance(data, list) else [])
    pulse = data.get("pulse") or data.get("community_pulse") or None
    md = build_markdown(cards, data.get("coverage"), data.get("date"), pulse=pulse)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
