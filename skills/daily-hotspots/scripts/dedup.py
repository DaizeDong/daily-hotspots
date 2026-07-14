#!/usr/bin/env python3
"""Cross-day dedup + evolution (Acceptance Gate T3/T5/T7) over the schedule-reminder base.

Two layers, cleanly split for testability:

  * PURE matching + decision (no DB):
      - match_existing(candidate, ledger_rows, cfg) -> matched row or None
        multi-signal: exact canonical_key, else SimHash Hamming<=H, else Jaccard cosine>=T
        (single-signal matching is forbidden — anti-pattern: pure-semantic false merges).
      - decide(candidate, matched, cfg) -> {"branch": NEW|SUPPRESS|RESURFACE, "delta": {...}}

  * LedgerClient: thin subprocess wrapper around `reminder.py <verb> --json`. NEVER reads the DB
    directly, NEVER builds SQL (frozen contract api_version 1.0.0). idempotency_key = canonical_key,
    so re-adding the same opportunity UPSERTs (returns same id, ext merged) = built-in idempotency.
    ext namespace = x_daily_hotspots_* (MUST-PRESERVE round-trip).

Reminder.py is located via DAILY_HOTSPOTS_REMINDER_CMD (a JSON list or shell string) or by probing
the reminder ledger CLI — no machine-specific path baked in.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from lib import (canonical_key, hamming, jaccard, load_config, now_utc, iso,
                 simhash, extract_entities)

SOURCE = "daily-hotspots"
EXT_PREFIX = "x_daily_hotspots_"

NEW, SUPPRESS, RESURFACE = "NEW", "SUPPRESS", "RESURFACE"


# --------------------------------------------------------------------------- pure layer

def _token_set(text: str) -> set:
    return set(extract_entities(text, max_n=64))


def _subject_agree(cand_text: str, row_text: str) -> bool:
    """Subject-agreement guard (R3 precision fix).

    The weak soft-match rungs (moderate Jaccard / SimHash near-dup) catch legitimate *rewrites*
    of the SAME opportunity, but on generic-descriptor-heavy titles they also false-merge two
    DISTINCT opportunities that differ only in their subject brand (Stripe vs Adyen, Vercel vs
    Netlify): they share many generic words (high cos) yet are different events. A merge there
    silently SUPPRESSes a real distinct opportunity (ARCHITECTURE §5.2 "单一信号必失败" — generic
    word overlap is a single weak signal and must not merge on its own; + the ≥2-source red line).

    Deterministic discriminator (no NER/embeddings): the two SUBJECTS agree iff
      * one side's entity set is a subset of the other's (same opportunity, later report strictly
        richer = evolving), OR
      * the leading (subject-first) content entity is the same (alias-normalised by extract_entities).
    A true rewrite keeps the subject; a distinct opportunity introduces its own subject brand that
    the other lacks while sharing no subject anchor. Returns True when we should NOT veto.
    """
    ce = extract_entities(cand_text, max_n=64)
    re_ = extract_entities(row_text, max_n=64)
    if not ce or not re_:
        return True  # cannot determine a subject → defer to the existing overlap signals
    cset, rset = set(ce), set(re_)
    if cset <= rset or rset <= cset:
        return True  # one strictly richer than the other = same evolving opportunity
    return ce[0] == re_[0]  # shared leading subject entity


def match_existing(candidate: dict, ledger_rows: list[dict], cfg: dict | None = None):
    """Return the best matching existing row (with its x_daily_hotspots_* ext) or None.
    Multi-signal: exact key > SimHash near-dup > token Jaccard. Pure (no clock/DB)."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    ham_thr = int(sc.get("dedup_simhash_hamming", 3))
    cos_thr = float(sc.get("dedup_cosine_threshold", 0.83))

    ckey = candidate["canonical_key"]
    ctext = (candidate.get("title", "") + " " + candidate.get("summary", ""))
    csh = simhash(ctext)
    ctoks = _token_set(ctext)

    # 1) exact canonical key
    for row in ledger_rows:
        if _row_key(row) == ckey:
            return row

    # 2/3) soft: highest similarity above either threshold; require entity overlap too so a pure
    # token-overlap (same words, different event) does NOT merge.
    best, best_sim = None, 0.0
    for row in ledger_rows:
        rtext = _row_ext(row).get(EXT_PREFIX + "text", "")
        if not rtext:
            continue
        rsh = int(_row_ext(row).get(EXT_PREFIX + "simhash", 0) or 0)
        ham_ok = bool(rsh) and hamming(csh, rsh) <= ham_thr
        cos = jaccard(ctoks, _token_set(rtext))
        # entity-overlap guard (multi-signal): the candidate must share entities, NOT just words —
        # this is what prevents the "same words, different event" false merge.
        rkey_track = _row_key(row).split("::")[-1]
        ckey_track = ckey.split("::")[-1]
        ent_overlap = len(set(_row_key(row).split("::")[0].split("|")) &
                          set(ckey.split("::")[0].split("|")))
        strong = (ent_overlap >= 2) or (ent_overlap >= 1 and rkey_track == ckey_track)
        # Subject-agreement guard (R3): the weak rewrite-catch rungs only fire when the two share a
        # subject (leading entity / subset). A very-high-cos near-identical text (>=cos_thr) still
        # bypasses (genuine near-dup regardless of word order); the exact-key path is unaffected.
        subj = _subject_agree(ctext, rtext)
        # Match when: pure-semantic near-dup (cos>=cos_thr, high bar), OR SimHash near-dup with
        # subject agreement, OR strong shared-entity set + moderate overlap + subject agreement.
        match_ok = (cos >= cos_thr) or (strong and ham_ok and subj) or (strong and cos >= 0.45 and subj)
        if match_ok and cos >= best_sim:
            best, best_sim = row, cos
    return best


def decide(candidate: dict, matched: dict | None, cfg: dict | None = None) -> dict:
    """Three-branch matrix. Pure."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    jump = float(sc.get("resurface_score_jump", 15))

    if matched is None:
        return {"branch": NEW, "delta": {}}

    ext = _row_ext(matched)
    prev_score = float(ext.get(EXT_PREFIX + "last_score", 0) or 0)
    prev_stage = ext.get(EXT_PREFIX + "lifecycle_stage", "")
    prev_sources = set(ext.get(EXT_PREFIX + "source_set", []) or [])

    cur_score = float(candidate.get("final_score", 0))
    cur_stage = candidate.get("lifecycle_stage", "")
    cur_sources = set(candidate.get("source_set", []) or
                      [e.get("source") for e in candidate.get("evidence", [])])

    new_sources = cur_sources - prev_sources
    score_delta = cur_score - prev_score
    crossed_two = (len(prev_sources) < 2 <= len(cur_sources))

    material = (
        (cur_stage and cur_stage != prev_stage) or
        (abs(score_delta) >= jump) or
        (len(new_sources) >= 1 and crossed_two)
    )
    branch = RESURFACE if material else SUPPRESS
    return {
        "branch": branch,
        "delta": {
            "score_delta": round(score_delta, 4),
            "new_sources": sorted(new_sources),
            "stage_from": prev_stage, "stage_to": cur_stage,
            "crossed_two_sources": crossed_two,
        },
    }


# --------------------------------------------------------------------------- ledger glue

def _row_key(row: dict) -> str:
    return row.get("idempotency_key") or _row_ext(row).get(EXT_PREFIX + "canonical_key", "")


def _row_ext(row: dict) -> dict:
    return row.get("ext") or {}


def build_ext(candidate: dict, sample: dict, prior_ext: dict | None = None,
              cfg: dict | None = None) -> dict:
    """Construct/merge the x_daily_hotspots_* ext namespace (MUST-PRESERVE). Appends a sample,
    caps the ring buffer, tracks first/last seen + source_set + push_count."""
    cfg = cfg or load_config()
    cap = int(cfg["scoring"].get("samples_cap", 30))
    prior_ext = prior_ext or {}
    now = iso(now_utc())
    text = candidate.get("title", "") + " " + candidate.get("summary", "")
    samples = list(prior_ext.get(EXT_PREFIX + "samples", []))
    samples.append(sample)
    samples = samples[-cap:]
    sources = sorted(set(prior_ext.get(EXT_PREFIX + "source_set", []) or []) |
                     set(candidate.get("source_set", []) or
                         [e.get("source") for e in candidate.get("evidence", [])]))
    return {
        EXT_PREFIX + "canonical_key": candidate["canonical_key"],
        EXT_PREFIX + "simhash": simhash(text),
        EXT_PREFIX + "text": text[:400],
        EXT_PREFIX + "first_seen": prior_ext.get(EXT_PREFIX + "first_seen", now),
        EXT_PREFIX + "last_seen": now,
        EXT_PREFIX + "last_score": candidate.get("final_score", 0),
        EXT_PREFIX + "lifecycle_stage": candidate.get("lifecycle_stage", ""),
        EXT_PREFIX + "source_set": sources,
        EXT_PREFIX + "push_count": int(prior_ext.get(EXT_PREFIX + "push_count", 0)),
        EXT_PREFIX + "samples": samples,
    }


class LedgerClient:
    """Subprocess wrapper around reminder.py. Honors --db / SCHEDULE_DB_PATH and --now via env."""

    def __init__(self, cmd=None, db_path=None, actor=SOURCE):
        self.cmd = self._resolve_cmd(cmd)
        self.db_path = db_path or os.environ.get("SCHEDULE_DB_PATH")
        self.actor = actor

    @staticmethod
    def _resolve_cmd(cmd):
        if cmd:
            return cmd if isinstance(cmd, list) else shlex.split(cmd)
        env = os.environ.get("DAILY_HOTSPOTS_REMINDER_CMD")
        if env:
            try:
                v = json.loads(env)
                if isinstance(v, list):
                    return v
            except Exception:
                return shlex.split(env)
        probe = Path.home() / ".claude/skills/schedule-reminder/scripts/reminder.py"
        return [sys.executable, str(probe)]

    def _run(self, verb, args):
        base = list(self.cmd)
        if self.db_path:
            base += ["--db", self.db_path]
        base += ["--actor", self.actor, verb] + args
        proc = subprocess.run(base, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            err = (proc.stderr or out).strip()
            raise RuntimeError(f"reminder.py {verb} failed rc={proc.returncode}: {err[:300]}")
        return json.loads(out) if out else {}

    def init(self):
        return self._run("init", [])

    def list_active(self, limit=500):
        rows, cursor = [], None
        while True:
            args = ["--source", SOURCE, "--active", "--limit", str(limit)]
            if cursor:
                args += ["--cursor", cursor]
            res = self._run("list", args)
            rows += res.get("items", [])
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return rows

    def upsert(self, candidate, ext, title=None, state="pending"):
        key = candidate["canonical_key"]
        args = ["--title", title or candidate.get("title", key)[:120],
                "--kind", "task", "--source", SOURCE,
                "--idempotency-key", key, "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def add_watermark(self, last_run_at):
        ext = {EXT_PREFIX + "last_run_at": last_run_at}
        args = ["--title", "daily-hotspots watermark", "--kind", "task", "--source", SOURCE,
                "--idempotency-key", "daily-hotspots:watermark",
                "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def get_watermark(self):
        try:
            rows = self.list_active()
        except Exception:
            return None
        for r in rows:
            if _row_key(r) == "daily-hotspots:watermark":
                return _row_ext(r).get(EXT_PREFIX + "last_run_at")
        return None

    # ---- bandit posterior persistence (R6 loop close): a singleton item carrying the per-track
    # arm state as JSON in ext, mirroring the watermark pattern, so the explore-exploit posterior
    # survives across daily runs instead of evaporating each run.
    def set_bandit_arms(self, arms):
        import bandit as bdt
        ext = {EXT_PREFIX + "bandit_arms": json.dumps(bdt.serialize_arms(arms), ensure_ascii=False)}
        args = ["--title", "daily-hotspots bandit", "--kind", "task", "--source", SOURCE,
                "--idempotency-key", "daily-hotspots:bandit",
                "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def get_bandit_arms(self):
        import bandit as bdt
        try:
            rows = self.list_active()
        except Exception:
            return {}
        for r in rows:
            if _row_key(r) == "daily-hotspots:bandit":
                raw = _row_ext(r).get(EXT_PREFIX + "bandit_arms")
                if raw:
                    try:
                        return bdt.deserialize_arms(json.loads(raw))
                    except Exception:
                        return {}
        return {}

    # ---- cross-day community-pulse dedup persistence (§7 "no rumor re-bubbles"): a singleton item
    # carrying a bounded {pulse_key: last_shown_iso} map in ext, mirroring the watermark/bandit
    # pattern, so a single-source community rumor rendered today is remembered and suppressed on
    # later days until a 2nd independent origin escalates it to a scored card.
    def set_pulse_seen(self, seen_map):
        ext = {EXT_PREFIX + "pulse_seen": json.dumps(seen_map or {}, ensure_ascii=False)}
        args = ["--title", "daily-hotspots pulse-seen", "--kind", "task", "--source", SOURCE,
                "--idempotency-key", "daily-hotspots:pulse-seen",
                "--ext", json.dumps(ext, ensure_ascii=False)]
        return self._run("add", args)

    def get_pulse_seen(self):
        try:
            rows = self.list_active()
        except Exception:
            return {}
        for r in rows:
            if _row_key(r) == "daily-hotspots:pulse-seen":
                raw = _row_ext(r).get(EXT_PREFIX + "pulse_seen")
                if raw:
                    try:
                        v = json.loads(raw)
                        return v if isinstance(v, dict) else {}
                    except Exception:
                        return {}
        return {}


def main() -> int:
    """CLI: pipe {"candidate":{...},"ledger":[...]} → prints {branch, matched_key, delta}."""
    data = json.loads(sys.stdin.read() or "{}")
    cand = data["candidate"]
    ledger = data.get("ledger", [])
    cfg = load_config()
    matched = match_existing(cand, ledger, cfg)
    res = decide(cand, matched, cfg)
    res["matched_key"] = _row_key(matched) if matched else None
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
