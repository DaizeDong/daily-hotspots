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

from lib import iso, load_config, now_utc
from archive import resolve_archive_dir


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
