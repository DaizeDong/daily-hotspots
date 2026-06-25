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
import sys
from pathlib import Path

from datetime import timedelta

from lib import iso, load_config, now_utc, parse_ts
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


def build_markdown(cards: list[dict], coverage: dict | None = None,
                   date: str | None = None) -> str:
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
        return "\n".join(lines)
    cards = sorted(cards, key=lambda c: -float(c.get("final_score", 0)))
    for c in cards:
        bd = c.get("score_breakdown", {})
        dims = " ".join(f"{k}={round(float(v))}" for k, v in bd.items())
        srcs = ", ".join(sorted(set(e.get("source", "?") for e in c.get("evidence", []))))
        lines.append(f"## {c.get('grade')} {c.get('final_score')} — {c.get('title','?')}")
        lines.append(f"- track: `{c.get('track')}` | types: {','.join(c.get('machine_type', []))}"
                     f" | {c.get('independent_source_count',0)} 独立源 [{srcs}]")
        lines.append(f"- dims: {dims}")
        if c.get("why_now"):
            lines.append(f"- why-now: {c['why_now']}")
        if c.get("contrarian_insight"):
            lines.append(f"- 非共识: {c['contrarian_insight']}")
        if c.get("action"):
            lines.append(f"- 行动: {c['action']}")
        if c.get("delegated_deepdive"):
            lines.append(f"- deep-dive: {c['delegated_deepdive']}")
        for e in c.get("evidence", [])[:4]:
            lines.append(f"  - {e.get('source','?')}: {e.get('url','')} ({e.get('signal','')})")
        lines.append("")
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
    md = build_markdown(cards, data.get("coverage"), data.get("date"))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
