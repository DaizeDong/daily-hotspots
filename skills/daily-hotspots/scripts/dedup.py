#!/usr/bin/env python3
"""Cross-day dedup + evolution (Acceptance Gate T3/T5/T7) over the schedule-reminder base.

Two layers, cleanly split for testability:

  * PURE matching + decision (no DB):
      - match_existing(candidate, ledger_rows, cfg) -> matched row or None
        multi-signal: exact canonical_key, else SimHash Hamming<=H, else Jaccard cosine>=T
        (single-signal matching is forbidden, anti-pattern: pure-semantic false merges).
      - decide(candidate, matched, cfg) -> {"branch": NEW|SUPPRESS|RESURFACE, "delta": {...}}

  * LedgerClient: thin subprocess wrapper around `reminder.py <verb> --json`. NEVER reads the DB
    directly, NEVER builds SQL (frozen contract api_version 1.0.0). idempotency_key = canonical_key,
    so re-adding the same opportunity UPSERTs (returns same id, ext merged) = built-in idempotency.
    ext namespace = x_daily_hotspots_* (MUST-PRESERVE round-trip).

Reminder.py is located via DAILY_HOTSPOTS_REMINDER_CMD (a JSON list or shell string) or by probing
a generic local default path, no machine-specific path baked in.
"""
from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from lib import (canonical_key, hamming, jaccard, load_config, now_utc, iso, parse_ts,
                 simhash, extract_entities)

SOURCE = "daily-hotspots"
EXT_PREFIX = "x_daily_hotspots_"

NEW, SUPPRESS, RESURFACE = "NEW", "SUPPRESS", "RESURFACE"

# Singleton bookkeeping rows (watermark / bandit posterior / pulse-seen map). They carry no
# opportunity text and no last_seen, they are pure state, so the compare-set window must NEVER
# expire them: dropping the watermark would silently re-collect an already covered slot.
SINGLETON_PREFIX = "daily-hotspots:"

# Fallback window bounds, used ONLY when the loaded config carries neither key. load_config()
# normally supplies both from lib.DEFAULT_CONFIG["scoring"]; when it does not, the value used is
# reported in the window report as builtin-default so "clean" and "unconfigured" stay distinct.
_WINDOW_FALLBACK = {"lookback_days": 7, "fading_quiet_days": 5}

# Character n-gram rung (see match_existing rung D). CJK text tokenises into whole-clause runs
# (lib._TOKEN_RE matches a maximal CJK run), so token Jaccard and token SimHash both collapse on
# Chinese prose: two write-ups of the SAME story score ~0.12 token Jaccard, far under the 0.45 rung.
# Character 3-grams do not care where the word boundaries are. Overridable from watchlist.json via
# scoring.dedup_char_ngram_threshold / scoring.dedup_char_ngram_n.
_CHAR_NGRAM_N = 3
_CHAR_NGRAM_THRESHOLD = 0.10

# A CJK token longer than this is a clause, not a name (brands are short: 英伟达 3, 字节跳动 4),
# so the subject guard cannot read a subject out of it and must abstain rather than guess.
_CJK_SUBJECT_MAX_CHARS = 6


# --------------------------------------------------------------------------- pure layer

def _token_set(text: str) -> set:
    return set(extract_entities(text, max_n=64))


def _char_ngrams(text: str, n: int = _CHAR_NGRAM_N) -> set:
    """Whitespace-stripped, lowercased character n-gram set. Word-boundary agnostic on purpose."""
    s = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def char_similarity(a: str, b: str, n: int = _CHAR_NGRAM_N) -> float:
    """Jaccard over character n-grams. Pure, deterministic, script agnostic."""
    return jaccard(_char_ngrams(a, n), _char_ngrams(b, n))


def _word_like(tok: str) -> bool:
    """True when a token is short enough to BE a subject (an ascii word, or a short CJK name).

    lib's tokenizer emits a maximal CJK run as ONE token, so `某平台在同一天连发三条公告` (13 chars,
    a whole clause) and `英伟达` (3 chars, a company) arrive as the same kind of object. Only the
    short one can be a subject; alignment against a clause token says nothing about subjects.
    """
    if not tok:
        return False
    if tok.isascii():
        return True
    return len(tok) <= _CJK_SUBJECT_MAX_CHARS


def _subject_agree(cand_text: str, row_text: str) -> bool:
    """Subject-agreement guard (R3 precision fix). Returns True when we should NOT veto a merge.

    The weak soft-match rungs (moderate Jaccard / SimHash near-dup / char n-gram) catch legitimate
    *rewrites* of the SAME opportunity, but on generic-descriptor-heavy titles they also false-merge
    two DISTINCT opportunities that differ only in their subject brand (Stripe vs Adyen, Vercel vs
    Netlify): they share many generic words (high cos) yet are different events. A merge there
    silently SUPPRESSes a real distinct opportunity (ARCHITECTURE §5.2 "单一信号必失败", generic
    word overlap is a single weak signal and must not merge on its own; + the ≥2-source red line).

    The previous discriminator was "the two leading entities are equal". It collapsed in BOTH
    directions on ordinary headlines:
      * leading token generic ("payments platform Stripe ..." vs "payments platform Adyen ...")
        → the leading entities agree, the veto never fires, and two distinct opportunities merge.
      * leading token is framing ("传闻落地：AcmeChip 四亿美元买下 ModelHub" vs "AcmeChip 四亿美元
        买下 ModelHub，在同一天") → the leading entities differ, the veto fires, and the SAME story
        splits into two cards. That is the shape a live run hit: eleven archived cards holding
        three same story pairs, each half carrying a different evidence[0].

    The discriminator now reads the KIND of the earliest difference instead of one fixed position.
    Align the two entity sequences (difflib, deterministic) and look at the first non-equal opcode:
      * `replace` = a substitution at the earliest divergence: same template, swapped subject
        (Stripe→Adyen), so the subjects DISAGREE and we veto, wherever in the sentence it sits.
      * `insert` / `delete` = one side merely adds framing (a "传闻落地：" prefix, a trailing clause)
        around an otherwise shared opening: the subject lives in the shared part, so we agree.
    Plus the two pre-existing shortcuts: no entities at all, or one entity set a subset of the other
    (later report strictly richer = the same evolving opportunity).

    ABSTENTION: when the substituted span contains a token that is not word-like (a long CJK clause
    run, see _word_like) the alignment cannot tell a subject swap from a rephrasing, so the guard
    returns True and defers to the other signals (curated entity overlap + char n-gram) rather than
    guessing. It abstains rather than vetoing because a wrong veto is the failure the operator saw.
    """
    ce = extract_entities(cand_text, max_n=64)
    re_ = extract_entities(row_text, max_n=64)
    if not ce or not re_:
        return True  # cannot determine a subject → defer to the existing overlap signals
    cset, rset = set(ce), set(re_)
    if cset <= rset or rset <= cset:
        return True  # one strictly richer than the other = same evolving opportunity
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, re_, ce, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace":
            return True   # earliest divergence is added/removed framing → shared subject
        swapped = re_[i1:i2] + ce[j1:j2]
        if not all(_word_like(t) for t in swapped):
            return True   # clause-level tokens: cannot read a subject → abstain, do not veto
        return False      # earliest divergence is a subject substitution → distinct subjects
    return True


def match_existing(candidate: dict, ledger_rows: list[dict], cfg: dict | None = None):
    """Return the best matching existing row (with its x_daily_hotspots_* ext) or None.
    Multi-signal: exact key > SimHash near-dup > token Jaccard. Pure (no clock/DB)."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    ham_thr = int(sc.get("dedup_simhash_hamming", 3))
    cos_thr = float(sc.get("dedup_cosine_threshold", 0.83))
    char_thr = float(sc.get("dedup_char_ngram_threshold", _CHAR_NGRAM_THRESHOLD))
    char_n = int(sc.get("dedup_char_ngram_n", _CHAR_NGRAM_N))

    ckey = candidate["canonical_key"]
    ctext = (candidate.get("title", "") + " " + candidate.get("summary", ""))
    # build_ext stores the row text truncated to 400 chars, so compare like with like: an
    # untruncated candidate against a truncated row would depress every similarity asymmetrically.
    ctext_cmp = ctext[:400]
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
        # entity-overlap guard (multi-signal): the candidate must share entities, NOT just words ,
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
        # Rung D (char n-gram): token Jaccard and token SimHash both collapse on CJK prose because
        # the tokenizer emits whole clauses as single tokens, so two write-ups of the SAME story
        # score ~0.12 cos and Hamming ~24 and no rung above fires. Character n-grams do not depend
        # on word boundaries. Still MULTI-signal: it fires only together with the curated
        # shared-entity set (`strong`) and subject agreement, never on its own.
        char_sim = char_similarity(ctext_cmp, rtext, char_n)
        match_ok = ((cos >= cos_thr)
                    or (strong and ham_ok and subj)
                    or (strong and cos >= 0.45 and subj)
                    or (strong and char_sim >= char_thr and subj))
        # Rank by the stronger of the two content similarities so a CJK match (near-zero cos, high
        # char_sim) is not always ranked last and silently beaten by a weaker token overlap.
        sim = max(cos, char_sim)
        if match_ok and sim >= best_sim:
            best, best_sim = row, sim
    return best


def _baseline_score(ext: dict) -> tuple[float, str]:
    """The score this opportunity is measured AGAINST, plus where that number came from.

    RATCHET DEFECT: the comparison used to run against `last_score`, which build_ext rewrites on
    EVERY run including a SUPPRESS. For an opportunity that strengthens a little each day the
    baseline therefore ratchets up in lockstep with the score, each day's delta is the one-day step
    (2 or 3 points), the delta never reaches resurface_score_jump, and a story that climbed 60 → 90
    over two weeks is suppressed on every one of those days. The baseline must be the score at the
    last time the opportunity was actually SURFACED, so the deltas accumulate between surfacings.

    Legacy rows written before `baseline_score` existed fall back to `last_score`, which is exactly
    the old behavior for one run and then self-heals on the next write.
    """
    raw = ext.get(EXT_PREFIX + "baseline_score")
    if raw is None:
        return float(ext.get(EXT_PREFIX + "last_score", 0) or 0), "last_score"
    return float(raw or 0), "baseline_score"


def _surfaced(candidate: dict) -> bool:
    """Did this card actually reach a human on this run? Only then does the baseline advance.

    Pushed (in the digest) or archived (in the ledger of record) are the two real outcomes. A card
    the verify gate blocked was never shown, so it must NOT consume the baseline, otherwise a
    blocked RESURFACE quietly resets the comparison and the opportunity has to climb all over again.
    """
    return bool(candidate.get("pushed")) or bool(candidate.get("archived"))


def decide(candidate: dict, matched: dict | None, cfg: dict | None = None) -> dict:
    """Three-branch matrix. Pure."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    jump = float(sc.get("resurface_score_jump", 15))

    if matched is None:
        return {"branch": NEW, "delta": {}}

    ext = _row_ext(matched)
    prev_score, baseline_from = _baseline_score(ext)
    last_observed = float(ext.get(EXT_PREFIX + "last_score", 0) or 0)
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
            "score_delta": round(score_delta, 4),        # measured against the last SURFACED score
            "baseline_score": round(prev_score, 4),
            "baseline_from": baseline_from,              # "baseline_score" | "last_score" (legacy)
            "last_observed_score": round(last_observed, 4),
            "observed_delta": round(cur_score - last_observed, 4),   # one-day step, reporting only
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


def _window_bounds(cfg: dict) -> tuple[dict, dict]:
    """(values, provenance) for the two compare-window knobs, so an unconfigured run is visible."""
    sc = (cfg or {}).get("scoring", {}) or {}
    values, source = {}, {}
    for key, fallback in _WINDOW_FALLBACK.items():
        if key in sc and sc[key] is not None:
            values[key], source[key] = int(sc[key]), "config"
        else:
            values[key], source[key] = int(fallback), "builtin-default"
    return values, source


def partition_ledger(rows: list[dict], cfg: dict | None = None, now=None) -> dict:
    """Split ledger rows into the ACTIVE compare set and what the window expired. Pure.

    Why this exists: `lookback_days` and `fading_quiet_days` shipped in the config and were read by
    nothing, so the compare set was every row ever written (342 permanently active rows on the live
    ledger). The suppression surface therefore grew without bound and a card from months ago could
    still suppress today's, which is the opposite of a daily radar.

    Window semantics, both knobs honored and both individually observable in the report:
      * `fading_quiet_days` , the reference's "5 consecutive quiet days → doing→done (fading auto
        close-out; drop from the lookback compare set)". Quiet = days since last_seen.
      * `lookback_days` , the outer bound of the compare window. Whichever bound is reached first
        expires the row, and the report names every bound that fired plus the effective window.

    Never silent: the returned report carries `checked`, the two bounds and where each value came
    from, every expired row with its quiet age, and the rows it could NOT date (kept, with a reason,
    because dropping a row of unknown age would be exactly the silent loss this fixes). Singleton
    bookkeeping rows (watermark / bandit / pulse-seen) are exempt and reported separately.
    """
    cfg = cfg or load_config()
    bounds, provenance = _window_bounds(cfg)
    ref = now or now_utc()
    report = {
        "checked": len(rows or []),
        "now": iso(ref),
        "bounds": bounds,
        "bounds_from": provenance,
        "kept": 0,
        "expired": [],
        "singletons": [],
        "undated": [],
    }
    active = []
    for row in rows or []:
        key = _row_key(row)
        if key.startswith(SINGLETON_PREFIX):
            report["singletons"].append(key)
            active.append(row)
            continue
        raw = _row_ext(row).get(EXT_PREFIX + "last_seen")
        if not raw:
            report["undated"].append({"key": key, "last_seen": None, "reason": "missing"})
            active.append(row)
            continue
        try:
            quiet_days = (ref - parse_ts(str(raw))).total_seconds() / 86400.0
        except Exception:
            report["undated"].append({"key": key, "last_seen": _cut(raw, 40),
                                      "reason": "unparsable"})
            active.append(row)
            continue
        hit = [name for name, limit in bounds.items() if quiet_days >= limit]
        if hit:
            report["expired"].append({
                "key": key, "last_seen": str(raw), "quiet_days": round(quiet_days, 3),
                "bounds": sorted(hit), "window_days": min(bounds[n] for n in hit),
            })
            continue
        active.append(row)
    report["kept"] = len(active)
    return {"active": active, "report": report}


def _cut(text: str, limit: int) -> str:
    """Shorten for one report line, but SAY that it was shortened. Never a silent truncation."""
    text = str(text or "?")
    return text if len(text) <= limit else text[:limit] + "...(cut)"


def format_window_report(report: dict, max_listed: int = 5) -> str:
    """One line describing the compare window. Names the bound and never hides a drop."""
    b = report.get("bounds", {})
    prov = report.get("bounds_from", {})
    head = ("[dedup] compare window: checked={checked} kept={kept} expired={n_exp} "
            "singletons={n_sing} undated={n_und} "
            "lookback_days={lb}({lbs}) fading_quiet_days={fq}({fqs})").format(
        checked=report.get("checked", 0), kept=report.get("kept", 0),
        n_exp=len(report.get("expired", [])), n_sing=len(report.get("singletons", [])),
        n_und=len(report.get("undated", [])),
        lb=b.get("lookback_days"), lbs=prov.get("lookback_days"),
        fq=b.get("fading_quiet_days"), fqs=prov.get("fading_quiet_days"))
    exp = report.get("expired", [])
    if exp:
        shown = exp[:max_listed]
        detail = "; ".join("{k} quiet={q}d by {b}".format(
            k=_cut(e["key"], 70), q=e["quiet_days"], b="+".join(e["bounds"])) for e in shown)
        if len(exp) > max_listed:
            detail += "; +{n} more expired (not listed)".format(n=len(exp) - max_listed)
        head += " | expired: " + detail
    und = report.get("undated", [])
    if und:
        head += " | undated kept: " + "; ".join(
            "{k} ({r})".format(k=_cut(u["key"], 70), r=u["reason"]) for u in und[:max_listed])
        if len(und) > max_listed:
            head += "; +{n} more (not listed)".format(n=len(und) - max_listed)
    return head


def build_ext(candidate: dict, sample: dict, prior_ext: dict | None = None,
              cfg: dict | None = None) -> dict:
    """Construct/merge the x_daily_hotspots_* ext namespace (MUST-PRESERVE). Appends a sample,
    caps the ring buffer, tracks first/last seen + source_set + push_count + the resurface baseline.

    `last_score` keeps its meaning: the score OBSERVED on this run, rewritten every run.
    `baseline_score` is the new comparison anchor: the score at the last run where this card was
    actually surfaced (pushed or archived). It stays frozen across SUPPRESS runs so a slowly
    strengthening opportunity accumulates its delta instead of racing its own moving baseline.
    """
    cfg = cfg or load_config()
    cap = int(cfg["scoring"].get("samples_cap", 30))
    prior_ext = prior_ext or {}
    now = iso(now_utc())
    text = candidate.get("title", "") + " " + candidate.get("summary", "")
    cur_score = candidate.get("final_score", 0)
    prior_baseline = prior_ext.get(EXT_PREFIX + "baseline_score")
    if prior_baseline is None:                       # legacy row: seed from the observed score
        prior_baseline = prior_ext.get(EXT_PREFIX + "last_score")
    if prior_baseline is None or _surfaced(candidate):
        baseline = cur_score                         # first sighting, or surfaced again today
    else:
        baseline = prior_baseline                    # suppressed: the anchor does NOT move
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
        EXT_PREFIX + "last_score": cur_score,
        EXT_PREFIX + "baseline_score": baseline,
        EXT_PREFIX + "lifecycle_stage": candidate.get("lifecycle_stage", ""),
        EXT_PREFIX + "source_set": sources,
        EXT_PREFIX + "push_count": int(prior_ext.get(EXT_PREFIX + "push_count", 0)),
        EXT_PREFIX + "samples": samples,
    }


class LedgerClient:
    """Subprocess wrapper around reminder.py. Honors --db / SCHEDULE_DB_PATH and --now via env."""

    def __init__(self, cmd=None, db_path=None, actor=SOURCE, cfg=None):
        self.cmd = self._resolve_cmd(cmd)
        self.db_path = db_path or os.environ.get("SCHEDULE_DB_PATH")
        self.actor = actor
        self.cfg = cfg
        # Last compare-window report (see partition_ledger). Present after every list_active() call,
        # so a caller can render the bound and the drops instead of them being invisible.
        self.last_window_report = None

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

    def list_active(self, limit=500, window=True):
        """The compare set: rows the base still calls active, narrowed to the lookback window.

        `window=False` returns the raw rows (maintenance/inspection). With the window on, the
        report is stored on `self.last_window_report` AND printed to stderr on every call, so a run
        that dropped nothing prints a "kept=N expired=0" line and is visibly different from a run
        where the window never ran at all.
        """
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
        if not window:
            return rows
        part = partition_ledger(rows, self.cfg or load_config())
        self.last_window_report = part["report"]
        print(format_window_report(part["report"]), file=sys.stderr)
        return part["active"]

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
    cfg = load_config()
    # Same compare window the live run uses (partition_ledger), and the window report rides along
    # in the output so a CLI probe can never mistake "expired out of the window" for "no match".
    part = partition_ledger(data.get("ledger", []), cfg)
    matched = match_existing(cand, part["active"], cfg)
    res = decide(cand, matched, cfg)
    res["matched_key"] = _row_key(matched) if matched else None
    res["window"] = part["report"]
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
