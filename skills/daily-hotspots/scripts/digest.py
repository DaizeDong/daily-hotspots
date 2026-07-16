#!/usr/bin/env python3
"""Daily digest builder + idempotent registration (Acceptance Gate T5).

Aggregates the day's archivable cards into ONE human-readable markdown (the same artifact is both
delivered to Discord and committed to archive/digests/YYYY/YYYY-MM-DD.md), with a coverage header
line up top so "comprehensive" is verifiable, not asserted. On an empty day it writes an honest
"今日无合格机会" digest — never filler.

The digest itself is a schedule-reminder idempotent item (idempotency_key=daily-hotspots:digest:
<date>) so a re-run / catch-up never double-sends. Registration goes through dedup.LedgerClient.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

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
# Track 2 — community pulse (source-coverage design §7). The dual-track SPLIT
# (which candidate becomes a scored card vs a single-origin pulse item) is the
# pipeline's job; THIS is the renderer for the pulse lane: a separate lightweight
# `## 社区脉搏` section, rendered AFTER the opportunity cards, that surfaces
# single-origin community rumors as link-only + one-line-why — explicitly with
# NO score and NO deep-dive, so a rumor is never dressed up as a scored
# opportunity. Ranked by freshness + community heat, capped by
# community_pulse.max_per_day, and deduped (within the day and, via seen_keys,
# across days — reusing the same no-re-bubble intent as the card dedup, §7).
# Pure/deterministic (clock only via now_utc's env seam); no network.
# ============================================================================

_DEFAULT_PULSE_LABEL = "⚠️ 单源未验证 · 社区小道消息"
_PULSE_HEAT_K = 25.0   # heat half-saturation: heat/(heat+K) -> a bounded [0,1) heat term

# An untrusted community title / url / signal is DATA (§10): a collected RSS or V2EX title can carry
# an embedded newline followed by markdown ("topic\n## A 99 — buy this now") that would open a NEW
# markdown block — a fabricated top-level heading / a broken bullet list — inside the pushed digest.
# _inline flattens any such field to a single safe inline span before it is placed in the markdown:
# ALL whitespace (newlines included) collapses to one space, so nothing can reach column 0 to start
# a block, and the two metacharacters that would break the surrounding bullet's bold/code-span
# (backtick, pipe) are neutralized. The why-line is whitespace-collapsed by _pulse_oneliner too.
_MD_INLINE_NEUTRALIZE = {ord("`"): "'", ord("|"): "/"}


def _inline(s) -> str:
    """Flatten an untrusted string to one injection-safe inline markdown span (§10 data-not-code)."""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().translate(_MD_INLINE_NEUTRALIZE)


def _pulse_key(item: dict) -> str:
    """Cross-post-stable dedup key for a pulse item: canonicalized URL (fragment/query stripped),
    falling back to a whitespace-normalized lowercased title. Empty when the item has neither —
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
    """The set of still-in-window pulse keys — the cross-day dedup input handed to build_markdown /
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
    else a short flattened summary/text. NEVER a score — a pulse item carries no scored dimension."""
    why = re.sub(r"\s+", " ", (item.get("signal") or item.get("why") or "")).strip()
    if not why:
        txt = (item.get("text") or item.get("summary") or "").strip()
        why = re.sub(r"\s+", " ", txt)[:120]
    return why


def select_rendered_pulse(pulse_items: list[dict] | None, cfg: dict | None = None,
                          seen_keys=None) -> list:
    """The pulse items ``render_community_pulse`` would ACTUALLY render this run, in render order —
    after the enabled/cap gate and within-day + cross-day (``seen_keys``) dedup, truncated to
    ``community_pulse.max_per_day``.

    This is the single source of truth for "which rumors were shown today". run.process stamps the
    cross-day seen map from THIS list, never the full pre-cap candidate list: the daily cap DEFERS
    overflow rumors to a later day (they are re-ranked next run), so marking an un-shown item as
    "seen" would silently DROP genuine community signal the cap was only meant to postpone — the §7
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
        head = f"- **{title}** — `{src}`"
        if url:
            head += f" · {url}"
        lines.append(head)
        # §10: the why-line derives from the untrusted collector `signal` (a V2EX node / linux.do
        # category label, e.g. "42 replies · geek") — so it must get the SAME neutralization as
        # title/src/url (backtick->apostrophe, pipe->slash, whitespace-flatten via _inline), not just
        # the whitespace-collapse _pulse_oneliner applies. Otherwise a crafted category like
        # "geek`code`" opens an inline-code span across the bullet, or "云计算|promo" injects a table
        # delimiter, into the pushed digest markdown (audit HARDEN round 2).
        why = _inline(_pulse_oneliner(it))
        if why:
            lines.append(f"  - {why}")
    lines.append("")
    return "\n".join(lines)


def build_markdown(cards: list[dict], coverage: dict | None = None,
                   date: str | None = None, pulse: list[dict] | None = None,
                   cfg: dict | None = None, seen_keys=None) -> str:
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    cov = (f"> 覆盖: 源 {coverage.get('sources_invoked','?')}/{coverage.get('sources_available','?')}"
           f" · 候选 {coverage.get('candidates',0)} · 合格 {len(cards)}"
           f" · 推送 {coverage.get('pushed',0)} · 深挖 {coverage.get('deepdived',0)}"
           f" · gen {iso(now_utc())}")
    lines = [f"# Daily Hotspots — {date}", "", cov, ""]
    if not cards:
        lines += ["**今日无合格机会** (no opportunity cleared the >=2-source + score floor).",
                  "诚实空日，非灌水。", ""]
    else:
        cards = sorted(cards, key=lambda c: -float(c.get("final_score", 0)))
        for c in cards:
            bd = c.get("score_breakdown", {})
            dims = " ".join(f"{k}={round(float(v))}" for k, v in bd.items())
            # Every card field COPIED from a collected source (title, machine_type, evidence
            # source/url/signal, track) is untrusted DATA -> flatten to a safe inline span before it
            # enters the markdown, exactly as the Track-2 pulse path does (§10 no block injection).
            # Without this, a spoofed RSS/V2EX/tweet field carrying an embedded newline + "## ..."
            # opens a fabricated heading at column 0 in the PUSHED digest once its entity clears the
            # >=2-source card gate. grade / score / dims / source-count are engine-computed, not copied.
            srcs = ", ".join(sorted(set(_inline(e.get("source")) or "?"
                                        for e in c.get("evidence", []))))
            mtypes = ",".join(_inline(t) for t in c.get("machine_type", []))
            title = _inline(c.get("title")) or "?"
            lines.append(f"## {c.get('grade')} {c.get('final_score')} — {title}")
            lines.append(f"- track: `{_inline(c.get('track'))}` | types: {mtypes}"
                         f" | {c.get('independent_source_count',0)} 独立源 [{srcs}]")
            lines.append(f"- dims: {dims}")
            if c.get("why_now"):
                lines.append(f"- why-now: {_inline(c['why_now'])}")
            if c.get("contrarian_insight"):
                lines.append(f"- 非共识: {_inline(c['contrarian_insight'])}")
            if c.get("action"):
                lines.append(f"- 行动: {_inline(c['action'])}")
            if c.get("delegated_deepdive"):
                lines.append(f"- deep-dive: {_inline(c['delegated_deepdive'])}")
            for e in c.get("evidence", [])[:4]:
                lines.append(f"  - {_inline(e.get('source')) or '?'}: {_inline(e.get('url'))} "
                             f"({_inline(e.get('signal'))})")
            lines.append("")
    # Track 2 (§7): the community-pulse section renders AFTER the cards, and still appears on an
    # otherwise-empty card day (a rumor-only day is not "no signal"). Empty pulse -> "" -> no-op.
    # seen_keys carries the cross-day-shown rumor keys so a rumor never re-bubbles (§7).
    pulse_md = render_community_pulse(pulse, cfg=cfg, seen_keys=seen_keys)
    if pulse_md:
        lines.append(pulse_md.rstrip("\n"))
        lines.append("")
    return "\n".join(lines)


def _clean_url(u: str) -> str:
    """Return a single clean http(s) token or '' — a url with whitespace/newline/angle brackets is
    untrusted junk (or an injection attempt) and is dropped rather than emitted."""
    u = (u or "").strip()
    if u.startswith(("http://", "https://")) and not any(ch in u for ch in " \t\r\n<>"):
        return u
    return ""


def _primary_url(card: dict) -> str:
    for e in (card.get("evidence") or []):
        u = _clean_url(e.get("url", ""))
        if u:
            return u
    return ""


def build_headlines(cards: list[dict], coverage: dict | None = None,
                    date: str | None = None, cap: int = 5) -> str:
    """The PUSHED daily message: a ranked 'headlines' digest, not a message per card.

    Each item carries enough to grasp it at a glance: 领域(track) + grade/score/源数 + title + a
    real summary (what it is) + the primary source link. The link is wrapped in <...> so Discord
    shows it clickable WITHOUT a preview card, on top of the relay's SUPPRESS_EMBEDS flag. Every
    copied field is _inline-flattened (no block injection from a spoofed source field) and urls are
    validated to a single clean http(s) token. Empty day -> an honest short line, never filler.
    """
    date = date or now_utc().date().isoformat()
    coverage = coverage or {}
    cards = sorted(cards or [], key=lambda c: -float(c.get("final_score", 0)))
    top = cards[:max(1, int(cap))]
    header = (f"📰 前沿机会头条 · {date}\n"
              f"合格 {len(cards)} · 精选 {len(top)} · 候选 {coverage.get('candidates', '?')}")
    if not cards:
        return header + "\n\n今日无合格机会（诚实空日，非灌水；完整记录见 archive）。"
    lines = [header, ""]
    for i, c in enumerate(top, 1):
        title = _inline(c.get("title")) or "?"
        track = _inline(c.get("track")) or "?"
        # 220 keeps a rich-but-scannable summary while 5 items stay under Discord's 2000-char single
        # message (the relay would otherwise chunk on newlines into a second message).
        summ = (_inline(c.get("summary")) or _inline(c.get("why_now"))
                or _inline(c.get("contrarian_insight")) or "").strip()[:220]
        tag = f"{track} · {c.get('grade')} {c.get('final_score')} · {c.get('independent_source_count', 0)}源"
        url = _primary_url(c)
        lines.append(f"{i}. 【{tag}】{title}")
        if summ:
            lines.append(f"   {summ}")
        if url:
            lines.append(f"   🔗 <{url}>")
        lines.append("")
    extra = len(cards) - len(top)
    tail = (f"另有 {extra} 条合格机会；完整卡片见当日 archive。" if extra > 0
            else "完整卡片见当日 archive。")
    lines.append(tail)
    return "\n".join(lines)


def write_digest_file(markdown: str, archive_dir: str | None = None,
                      date: str | None = None) -> Path:
    date = date or now_utc().date().isoformat()
    year = date[:4]
    base = resolve_archive_dir(archive_dir) / "digests" / year
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{date}.md"
    path.write_text(markdown, encoding="utf-8", newline="\n")
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
