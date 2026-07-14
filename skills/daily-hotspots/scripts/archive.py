#!/usr/bin/env python3
"""Append-only opportunity archive (Acceptance Gate T6 mechanical 宁缺毋滥).

Writes to the companion config repo's archive/ (resolved via the config-dir probe, or --archive-dir):
  * opportunities.jsonl   — canonical append-only store (git history = backup)
  * dedup-state.json      — fingerprint -> {first_seen,last_seen,push_count,cluster_id}
  * digests/YYYY/YYYY-MM-DD.md is written by digest.py, not here.

archive_card() re-asserts the quality gate (distinct ORIGIN >= 2 AND score >= min_score_to_archive)
before any write — a low-quality card is mechanically refused, returning ("refused", reason). This
is the deterministic backstop to the verify gate; nothing low-quality reaches disk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import find_config_dir, iso, load_config, now_utc, opportunity_id


def resolve_archive_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    d = find_config_dir()
    if d:
        return d / "archive"
    return Path.home() / ".daily-hotspots-config" / "archive"


def _jsonl_record(card: dict) -> dict:
    ck = card["canonical_key"]
    return {
        "opportunity_id": card.get("opportunity_id") or opportunity_id(ck),
        "canonical_key": ck,
        "cluster_id": card.get("cluster_id", ""),
        "first_seen": card.get("first_seen") or iso(now_utc()),
        "last_seen": iso(now_utc()),
        "status": card.get("status", "new"),
        "title": card.get("title", ""),
        "summary": card.get("summary", ""),
        "track": card.get("track"),
        "focus_tags": card.get("focus_tags", []),
        "machine_type": card.get("machine_type", []),
        "score": card.get("final_score"),
        "grade": card.get("grade"),
        "score_breakdown": card.get("score_breakdown", {}),
        "why_now": card.get("why_now", ""),
        "contrarian_insight": card.get("contrarian_insight", ""),
        "action": card.get("action", ""),
        "evidence": card.get("evidence", []),
        "independent_source_count": int(card.get("independent_source_count", 0)),
        "pushed": bool(card.get("pushed", False)),
        "push_count": int(card.get("push_count", 0)),
        "delegated_deepdive": card.get("delegated_deepdive"),
        "lifecycle_stage": card.get("lifecycle_stage", ""),
        "run_id": card.get("run_id", ""),
        "schema_version": 1,
    }


def archive_card(card: dict, archive_dir: str | None = None,
                 cfg: dict | None = None, dry_run: bool = False) -> tuple[str, str]:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    isc = int(card.get("independent_source_count", 0) or 0)
    score = float(card.get("final_score", 0) or 0)
    if isc < int(sc.get("min_independent_sources", 2)):
        return ("refused", f"distinct ORIGIN {isc} < {sc['min_independent_sources']}")
    if score < float(sc.get("min_score_to_archive", 55)):
        return ("refused", f"score {score} < min_score_to_archive {sc['min_score_to_archive']}")

    # dry_run re-asserts the quality gate above (so preview surfaces exactly what WOULD persist)
    # but writes nothing — mirrors push/ledger/digest dry_run semantics. Critical: a test or preview
    # run that has $DAILY_HOTSPOTS_CONFIG set must NOT leak fake cards into the real archive.
    if dry_run:
        return ("would-archive", card.get("opportunity_id") or opportunity_id(card.get("canonical_key", "")))

    base = resolve_archive_dir(archive_dir)
    base.mkdir(parents=True, exist_ok=True)
    rec = _jsonl_record(card)

    # append to jsonl (line-level append only)
    with open(base / "opportunities.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # upsert dedup-state.json
    state_path = base / "dedup-state.json"
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    ck = rec["canonical_key"]
    entry = state.get(ck, {})
    entry.setdefault("first_seen", rec["first_seen"])
    entry["last_seen"] = rec["last_seen"]
    entry["push_count"] = int(entry.get("push_count", 0)) + (1 if card.get("pushed") else 0)
    entry["cluster_id"] = rec["cluster_id"] or entry.get("cluster_id", "")
    entry["opportunity_id"] = rec["opportunity_id"]
    state[ck] = entry
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8", newline="\n")
    return ("archived", rec["opportunity_id"])


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    cards = data if isinstance(data, list) else [data]
    out = []
    for c in cards:
        status, detail = archive_card(c)
        out.append({"title": c.get("title", "?"), "status": status, "detail": detail})
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
