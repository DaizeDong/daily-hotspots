#!/usr/bin/env python3
"""Deterministic pipeline orchestrator, the gate that disposes what the LLM proposes.

INPUT (stdin or --in): a JSON list of *candidate clusters* the SKILL.md orchestration layer
already produced from the live MCP fan-out, each already cross-source de-duplicated into one
opportunity with its evidence[] and a temperature-0 per-dimension score_breakdown proposal:

  {
    "title","summary","entities":[...],
    "evidence":[{"source","origin","url","signal","ts"}, ...],   # >=1 raw; distinct ORIGIN gated here
    "score_breakdown":{track_fit,timing,feasibility,competition,executability},  # 0-100 each
    "age_hours": <float>, "velocity": <float|null>, "lifecycle_stage": "...",
    "why_now","contrarian_insight","action",
    "track": <optional; classify fills if absent>
  }

This module runs the DETERMINISTIC remainder: classify → canonical_key → distinct-ORIGIN gate
(>=2) → score → cross-day dedup (NEW/SUPPRESS/RESURFACE) → verify gate → tiered push → archive →
idempotent digest → atomic watermark. No network here except the relay/ledger subprocess seams,
both injectable + dry-runnable. Returns a structured result for the SKILL to report.
"""
from __future__ import annotations

import argparse
import html as _html
import importlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from lib import (canonical_key, extract_entities, iso, load_config, now_utc,
                 opportunity_id, parse_ts, safe_url)
# The url gate and the invisible-character sanitizer live in lib.py and are imported,
# never re-implemented: run.py carried its own copy of both until 2026-08-29, and the
# copy was the WEAKER one (it accepted the two markup characters the digest's shape
# check refused). Two implementations of one refusal is how the weaker one ends up
# guarding the push. _clean_text keeps its private name here: ~40 call sites read it.
from lib import clean_text as _clean_text
# Moved to lib.py when the collection leg became collect.py: both legs need them, neither owns them.
from lib import _handle_origin, failed_pull, is_failed_pull, split_pulls
from classify import classify, check_excluded, keyword_hit
import collect as co
from collect import collect_sources
from score import score_opportunity
import dedup as dd
from verify_gate import gate_batch, route_below_gate, COMMUNITY_PULSE
from lib import community_source_set
import push_card as pc
import archive as ar
import digest as dg
import roster as rt


def _distinct_origins(evidence: list[dict]) -> list[str]:
    return sorted(set((e.get("origin") or e.get("source") or "").lower()
                      for e in evidence if (e.get("origin") or e.get("source"))))


def _ev_origin(e: dict) -> str:
    return (e.get("origin") or e.get("source") or "").strip().lower()


def _quote_parent_origin(e: dict) -> str | None:
    """The origin an evidence item was QUOTE-DERIVED from, or None.

    A roster quote-signal (collect_roster) carries ``via_handle=<amplifying roster member>``: the item
    surfaces that member's amplification of a non-roster voice, not an independent collection of that
    voice. Its parent origin is the member's own ``x.com/<member>`` label, the form _handle_origin
    emits and that _distinct_origins lowercases, so the two align for the guard below."""
    vh = e.get("via_handle")
    if isinstance(vh, str) and vh.strip():
        return _handle_origin(vh)
    return None


def _platform_of(origin: str) -> str:
    """The CHANNEL an origin belongs to, so a platform agreeing with itself counts once per cap.

    Origins are emitted in two shapes: a host-plus-path account label (`x.com/karpathy`, the roster's
    per-account form) and a bare host or lane name (`news.ycombinator.com`, `hackernews`). Both
    reduce to their leading host segment, and a handful of lane aliases that the collection layer has
    emitted over time are folded onto the host they really are, because `twitterapi`, `x`, `twitter`,
    `x-roster` and `x-broad` all appear in the live archive for the same platform.
    """
    o = (origin or "").strip().lower()
    if o.startswith("http://"):
        o = o[7:]
    elif o.startswith("https://"):
        o = o[8:]
    host = o.split("/", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return _PLATFORM_ALIASES.get(host, host)


_PLATFORM_ALIASES = {
    "twitterapi": "x.com", "x": "x.com", "twitter": "x.com",
    "x-roster": "x.com", "x-broad": "x.com", "twitter.com": "x.com",
    "hackernews": "news.ycombinator.com", "hn": "news.ycombinator.com",
    "product-hunt": "producthunt.com", "producthunt": "producthunt.com",
    "github": "github.com", "official-github": "github.com",
    "reddit": "reddit.com", "arxiv": "arxiv.org",
}


def cfg_max_origins_per_platform(cfg: dict | None = None) -> int:
    """How many origins ONE platform may contribute to the independent count. 0 disables the cap."""
    try:
        sc = (cfg or load_config())["scoring"]
    except Exception:
        return _DEFAULT_MAX_ORIGINS_PER_PLATFORM
    v = sc.get("max_origins_per_platform", _DEFAULT_MAX_ORIGINS_PER_PLATFORM)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ORIGINS_PER_PLATFORM


_DEFAULT_MAX_ORIGINS_PER_PLATFORM = 2


def count_independent_sources(evidence: list[dict], cfg: dict | None = None) -> int:
    """Independent-source count for the >=2-ORIGIN red line, with two DETERMINISTIC anti-crowd guards.

    The naive count is "distinct origin labels", but two failure modes fake a crowd from non-independent
    material:

    1. TRANSLOAD (audit MEDIUM#2): a single wire story republished verbatim under several outlet labels
       (same exact URL listed N times) is NOT N independent sources. When every evidence item carries a
       URL, cap the independent count at the number of DISTINCT URLs: identical-URL republications
       collapse to one; genuinely distinct outlets each with their own write-up are unaffected.

    2. QUOTE-DERIVATIVE (audit HARDEN r4): a roster member QUOTING a non-roster voice emits TWO signals
       from a SINGLE pull, the member (``origin=x.com/<member>``) and the quoted voice
       (``origin=x.com/<quoted>``, ``via_handle=<member>``). A quote is by construction ABOUT the post
       it quotes, so if those two signals co-cluster into one candidate they carry two distinct origins
       and would clear the >=2-independent-origin red line, but the quoted voice is DERIVED from the
       member's amplification, not independent corroboration (the quoted signal's real job is the §8
       propose-add feed). So an origin whose ONLY appearances are quote-derivatives of ANOTHER origin
       PRESENT in the same evidence is not counted independently. Crucially, if that same origin ALSO
       appears as an independently-collected (non-quote) signal, e.g. the keyword search surfaced it
       too, it is NOT discounted and genuinely corroborates: the discount is precise, mirroring the
       transload guard.

    Scope (explicitly recorded, the SKILL/LLM normalization layer owns the rest): guard 1 catches
    exact-URL double-counting; guard 2 catches quote-derivatives that still carry ``via_handle`` after
    normalization. Semantic syndication (the SAME copy rehosted at DIFFERENT URLs) and a normalization
    pass that strips ``via_handle`` remain the LLM layer's job; the "deterministic gate disposes"
    guarantee does not extend to those.
    """
    origin_set = set(_distinct_origins(evidence))

    def _purely_quote_derived(e: dict, o: str) -> bool:
        p = _quote_parent_origin(e)                 # the origin e was quote-derived from, if any
        return p is not None and p != o and p in origin_set

    # QUOTE-DERIVATIVE discount: drop an origin whose EVERY appearance is a quote-derivative of a
    # co-present, different parent origin (a single roster pull's member+quoted pair). An origin with
    # even one independently-collected (non-quote) item survives and genuinely corroborates.
    for o in list(origin_set):
        items = [e for e in evidence if isinstance(e, dict) and _ev_origin(e) == o]
        if items and all(_purely_quote_derived(e, o) for e in items):
            origin_set.discard(o)
    # PLATFORM CONCENTRATION cap (guard 3). Measured on 197 archived cards: 8 of them cleared the
    # red line with independent_source_count between 2 and 6 while EVERY piece of evidence came from
    # x.com alone, several of them crypto narratives echoing across six accounts in a day. Per-handle
    # origins are deliberate and correct (the roster exists to catch a founder's post by identity, so
    # two different founders must not collapse into one), but a whole platform agreeing with itself
    # is one channel of information, not six, and it was buying the top confidence multiplier.
    # So each platform contributes at most `max_origins_per_platform` toward the count. This is a
    # PROPORTIONATE penalty, not a rejection: six x.com handles still clear the >=2 red line, they
    # just stop reading as better corroborated than a story carried by three unrelated outlets.
    cap = cfg_max_origins_per_platform(cfg)
    if cap > 0:
        per_platform: dict[str, int] = {}
        for o in origin_set:
            p = _platform_of(o)
            per_platform[p] = per_platform.get(p, 0) + 1
        n_origins = sum(min(c, cap) for c in per_platform.values())
    else:
        n_origins = len(origin_set)
    urls = [(e.get("url") or "").strip().lower() for e in evidence]
    if urls and all(urls):  # only cap when every item is URL-attributed
        return min(n_origins, len(set(urls)))
    return n_origins


# ============================================================================
# Source collection (source-coverage design §6): the roster loop + community
# lanes that feed the pipeline ALONGSIDE the existing broad keyword search.
#
# run.py stays the deterministic core: the LIVE MCP fan-out (twitterapi
# get_user_last_tweets, brightdata/webfetch) runs in the SKILL orchestration
# layer, which hands the RAW responses here. These functions do the
# deterministic remainder, filter, tag every evidence item with its origin
# (origin_handle for an X account, origin_source for a community lane, §6), and
# emit the per-run per-handle/source pulled-count line for the pulls-log (the
# yield DENOMINATOR, §5.1/§8). PURE (clock only via the `now` seam, no network);
# the single I/O edge is append_pulls().
#
# The broad keyword search (twitterapi search_tweets, collect.md) is UNCHANGED
# and additive: its candidate clusters still arrive via process()'s stdin/--in
# input. The roster is a COMPLEMENT for open discovery, never a replacement.
# ============================================================================



def pulls_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded pulls-log path: ``archive/pulls-YYYY-MM.jsonl`` (§5.1). One file per month keeps
    the append-only denominator ledger bounded; yield.load_pulls globs ``pulls-*.jsonl`` across
    months. Resolves via the same config-dir probe archive.py uses (or an explicit archive_dir)."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"pulls-{now.year:04d}-{now.month:02d}.jsonl"


def pull_errors_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded FAILED-pull ledger: ``archive/pull-errors-YYYY-MM.jsonl``.

    Deliberately a SEPARATE file from ``pulls-YYYY-MM.jsonl``: yield.load_pulls globs ``pulls-*.jsonl``
    and counts every line it finds as one observation of the origin, so a failure recorded in that
    file would be read as "we looked and there was nothing", which is exactly how an unreachable
    source becomes an auto-prune recommendation. The failure is still WRITTEN, in full, next to the
    denominator it is deliberately kept out of."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"pull-errors-{now.year:04d}-{now.month:02d}.jsonl"


def pull_identity(rec) -> tuple | None:
    """``(run_id, kind, unit)`` identity of one pulls-log record, or None when it names no unit.

    This is the IDEMPOTENCY key: one run pulls one handle (or one community source) once, so a
    second record for the same pair is a re-run of the same work, not new evidence."""
    if not isinstance(rec, dict):
        return None
    handle = rec.get("handle")
    unit = handle if handle else rec.get("source")
    if not unit or not str(unit).strip():
        return None
    return (str(rec.get("run_id") or ""), "handle" if handle else "source", str(unit).strip().lower())


def _ledger_identities(base, prefix: str) -> set:
    """Every pull identity already recorded in ``<base>/<prefix>*.jsonl``, across month shards.

    Reads ALL shards, not just the current month, because a re-run that crosses midnight on the last
    day of a month would otherwise write today's shard, not see yesterday's, and duplicate."""
    keys: set = set()
    if not base.is_dir():
        return keys
    for f in sorted(base.glob(prefix + "*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue          # an unparseable historical line cannot claim an identity
            k = pull_identity(rec)
            if k is not None:
                keys.add(k)
    return keys


def _append_jsonl(path, records) -> None:
    """Append records to a jsonl ledger. WRITER: no try/except, an IO failure propagates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_pulls_report(records, archive_dir: str | None = None, now=None,
                        dry_run: bool = False) -> dict:
    """Append the pulls-log (the yield DENOMINATOR, §5.1/§8) IDEMPOTENTLY, and report what it did.

    Two invariants, both of which the previous plain append broke:

    1. IDEMPOTENT per ``(run_id, handle|source)``. A re-run of the same day used to append a SECOND
       line for every handle, doubling the yield denominator while the deduped numerator (archived
       cards, counted once per opportunity_id) stayed flat, so every handle's measured yield silently
       halved and auto-prune read a healthy roster as deadweight. A record whose identity is already
       in the ledger is skipped and REPORTED, never written twice and never silently dropped.
    2. A FAILED pull never lands in the denominator. split_pulls routes it to the pull-errors ledger
       instead (see pull_errors_log_path), where it is still permanently recorded.

    ``dry_run`` writes nothing (mirrors archive/push/digest dry_run) so a preview/test run can never
    inflate the denominator. WRITER: hard-fails; an IO error propagates to the caller.

    Returns ``{"path", "written", "skipped_duplicate", "duplicates", "unkeyed", "unusable",
    "errors_path", "errors_written", "errors_skipped_duplicate", "dry_run"}``."""
    rep = {"path": None, "written": 0, "skipped_duplicate": 0, "duplicates": [],
           "unkeyed": 0, "unusable": 0, "errors_path": None, "errors_written": 0,
           "errors_skipped_duplicate": 0, "dry_run": bool(dry_run)}
    records = list(records or [])
    rep["unusable"] = sum(1 for r in records if not isinstance(r, dict))
    if dry_run or not records:
        return rep
    observed, failed = split_pulls(records)
    base = ar.resolve_archive_dir(archive_dir)

    def _dedup(batch, prefix):
        seen = _ledger_identities(base, prefix)
        keep, dupes, unkeyed = [], [], 0
        for rec in batch:
            k = pull_identity(rec)
            if k is None:
                unkeyed += 1     # cannot be deduped; kept, and counted so it is never invisible
                keep.append(rec)
                continue
            if k in seen:
                dupes.append({"run_id": k[0], k[1]: rec.get("handle") or rec.get("source")})
                continue
            seen.add(k)          # also collapses duplicates WITHIN one batch
            keep.append(rec)
        return keep, dupes, unkeyed

    if observed:
        keep, dupes, unkeyed = _dedup(observed, "pulls-")
        path = pulls_log_path(archive_dir, now)
        if keep:
            _append_jsonl(path, keep)
        rep["path"] = path
        rep["written"] = len(keep)
        rep["skipped_duplicate"] = len(dupes)
        rep["duplicates"] = dupes
        rep["unkeyed"] = unkeyed
    if failed:
        keep, dupes, _unkeyed = _dedup(failed, "pull-errors-")
        epath = pull_errors_log_path(archive_dir, now)
        if keep:
            _append_jsonl(epath, keep)
        rep["errors_path"] = epath
        rep["errors_written"] = len(keep)
        rep["errors_skipped_duplicate"] = len(dupes)
    return rep


def append_pulls(records, archive_dir: str | None = None, now=None, dry_run: bool = False):
    """Idempotent pulls-log append; returns the DENOMINATOR path, or None when nothing was
    denominator-bound (dry-run, empty input, or a batch of failed pulls only). The full accounting,
    including how many records were skipped as duplicates, is append_pulls_report."""
    return append_pulls_report(records, archive_dir, now, dry_run)["path"]


# --------------------------------------------------------------------------- collection ledger
# The two legs of a run are two separate processes: ``--sources`` collects and origin-tags the raw
# signals, then the SKILL clusters them and hands ``--in`` a much smaller list of candidates. Nothing
# carried the FIRST leg's count into the second, so process() had no idea how much signal it started
# from and the digest reported the funnel as if the candidate list were the whole input. On
# 2026-08-27 that hid a 625 -> 12 collapse behind an "everything is fine" coverage line (08-23
# 276 -> 12, 08-24 460 -> 18, 08-25 530 -> 29 are the same shape). This ledger is the seam: the
# --sources leg persists what it collected, keyed by run_id, and the --in leg reads it back and
# reports the difference as an explicit GAP.

def collection_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded collection ledger: ``archive/collection-YYYY-MM.jsonl``. The name deliberately
    does NOT start with ``pulls-``, so yield.load_pulls cannot mistake it for a denominator line."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"collection-{now.year:04d}-{now.month:02d}.jsonl"


def signal_key(item) -> str:
    """Stable identity of ONE collected signal or ONE candidate evidence item, so the two can be
    reconciled across the process boundary.

    The URL is the join key when present (normalized: lowercased, scheme and ``www.`` and a trailing
    slash removed), because that is the one field that survives the SKILL's clustering verbatim. With
    no URL we fall back to ``origin|ts|title[:80]``, which is weaker; a signal that carries neither a
    URL nor an origin has no identity at all and returns "", counted as collected but never
    reconcilable, which is itself an honest gap rather than a fake match."""
    if not isinstance(item, dict):
        return ""
    u = str(item.get("url") or "").strip().lower().rstrip("/")
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
    if u.startswith("www."):
        u = u[4:]
    if u:
        return u
    origin = str(item.get("origin") or item.get("origin_source") or item.get("origin_handle")
                 or item.get("source") or "").strip().lower()
    if not origin:
        return ""
    return f"{origin}|{str(item.get('ts') or '').strip()}|{str(item.get('title') or '')[:80]}"


def build_collection_record(out: dict, cfg: dict | None = None, now=None,
                            run_id: str | None = None, health=None) -> dict:
    """The per-run collection summary the --in leg reads back (see collection_log_path).

    ``sources_available`` = the lanes the CONFIG says should run (enabled entries under
    ``sources``). ``sources_invoked`` = the lanes that actually delivered a payload this run, derived
    from the pull records themselves (the roster counts as the single ``twitterapi`` lane, each
    community source as its own). ``sources_failed`` = one entry per unit that failed, a community
    lane by its source name and a roster handle by its ``x.com/<handle>`` origin label, so a
    half-failed roster is not rounded down to "twitterapi worked"."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or out.get("run_id") or f"daily-{now.date().isoformat()}"
    signals = [s for s in (out.get("signals") or []) if isinstance(s, dict)]
    observed, failed = split_pulls(out.get("pulls"))

    lanes: set = set()
    for rec in observed + failed:
        lanes.add("twitterapi" if rec.get("handle") else str(rec.get("source") or "?"))
    declared = [n for n, sc in ((cfg.get("sources") or {}).items())
                if (sc or {}).get("enabled", True)]
    sources_failed = []
    for rec in failed:
        h = rec.get("handle")
        # attempts + outcome ride along: "arctic-shift 500, 1 attempt" and "arctic-shift 500, 3
        # attempts with backoff" are different operational facts and the digest must be able to say
        # which one happened. A record written before those fields existed still reports honestly,
        # as 1 attempt and an unknown outcome, rather than claiming a retry that never ran.
        sources_failed.append({"source": _handle_origin(h) if h else str(rec.get("source") or "?"),
                               "error": rec.get("error", ""),
                               "attempts": int(rec.get("attempts") or 1),
                               "outcome": rec.get("outcome") or "unknown"})
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "ts": iso(now),
        "signals_collected": len(signals),
        "signal_keys": [signal_key(s) for s in signals],
        "sources_invoked": len(lanes),
        "sources_available": len(declared),
        "sources_failed": sources_failed,
        "pulls_observed": len(observed),
        "filtered": out.get("filtered") or {},
    }
    # The health probe (scripts/sourcehealth.py) runs in the fetch layer, so its result travels with
    # the collection record rather than being recomputed here. Absent = NOT PROBED, and the key is
    # left off entirely so build_coverage reports it as unmeasured instead of as a healthy zero.
    hs = normalize_source_health(health if health is not None else out.get("source_health"))
    if hs is not None:
        record["source_health"] = hs
    return record


def append_collection(record: dict, archive_dir: str | None = None, now=None,
                      dry_run: bool = False):
    """Append ONE collection record. WRITER: no try/except, an IO failure propagates.

    Append-only with LAST-WINS on read (load_collection): a re-run of the same day supersedes its
    earlier record instead of rewriting history."""
    if dry_run or not record:
        return None
    path = collection_log_path(archive_dir, now)
    _append_jsonl(path, [record])
    return path


def load_collection(run_id: str, archive_dir: str | None = None) -> dict | None:
    """The collection record for ``run_id``, or None when no --sources leg recorded one.

    READER: degrades. None is NOT "zero signals collected"; build_coverage reports it as an
    UNMEASURED field, so a missing collection leg and a genuinely empty one never print the same."""
    base = ar.resolve_archive_dir(archive_dir)
    if not base.is_dir():
        return None
    found = None
    for f in sorted(base.glob("collection-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("run_id") == run_id:
                found = rec          # last one wins: a re-run supersedes
    return found


def _track_weight(track: str, cfg: dict) -> float:
    for t in cfg.get("tracks", []):
        if t.get("id") == track:
            return float(t.get("weight", 1.0))
    return 1.0


def effective_track_weight(track: str, cfg: dict, arms: dict | None = None,
                           seed: int = 0, memo: dict | None = None) -> float:
    """Track weight fed into scoring. Without bandit arms this is the STATIC config weight (R6
    wiring is opt-in, byte-identical default). With arms, the static weight is multiplicatively
    nudged by a deterministic Thompson draw centered at 1.0 (explore_weight in [lo,hi]=[0.5,1.5]
    by default): a well-performing track gets lifted, an under-performing one dampened, and
    score.py re-folds track_weight at HALF strength + clamps, so the bandit nudges ranking toward
    promising-but-under-sampled tracks without ever overriding the evidence-driven score.

    ``memo`` is a per-run {track: entry} cache the caller owns. The weight depends only on
    (track, cfg, arms, seed), all fixed for the whole run, so recomputing it once per CANDIDATE was
    pure waste: a linear scan of cfg["tracks"] plus, under the bandit, an md5 + Beta draw + two
    config reads. It is also the run's decision record: each entry keeps the static weight, the
    explore multiplier and the product, which is what process() reports back to the operator."""
    if memo is not None and track in memo:
        return memo[track]["effective"]
    static = _track_weight(track, cfg)
    if not arms:
        entry = {"static": static, "explore": None, "effective": static}
    else:
        import bandit as bdt
        ew = bdt.explore_weight(arms, track, int(seed), cfg)
        entry = {"static": static, "explore": ew, "effective": round(static * ew, 6)}
    if memo is not None:
        memo[track] = entry
    return entry["effective"]


def bandit_report(arms_before: dict | None, arms_after: dict | None, track_weights: dict,
                  seed: int, persist_state: str) -> dict:
    """The run's account of what the R6 bandit DID, one row per track.

    An adaptive component nobody can observe is worse than none: before this, the only trace a run
    left of the bandit was ``bandit_arms_next``, a posterior with no record of which draw produced
    it or how the ranking moved. Each row now carries the static ARCHITECTURE 8.3 weight, the
    Thompson multiplier drawn this run, their product (what scoring actually used), and the
    posterior before and after, so the lift is reconstructible and the reward that caused it is
    visible. ``scored`` separates a track this run ranked from one that only came back from the
    ledger, and ``persist_state`` separates "saved" from every reason nothing was written, so a
    silent no-op can never read as a clean save. A null ``explore_multiplier`` on a scored track is
    the cold start: there was no posterior yet, so the run ranked on the static weight and the
    reward it collected is what the NEXT run will draw against."""
    before, after = arms_before or {}, arms_after or {}

    def _f(arm, key, default=0.0):
        try:
            return round(float((arm or {}).get(key, default)), 6)
        except (TypeError, ValueError):
            return default

    rows = []
    for track in sorted(set(track_weights) | set(after) | set(before)):
        w = track_weights.get(track) or {}
        b, a = before.get(track), after.get(track)
        n_before, n_after = int((b or {}).get("n", 0)), int((a or {}).get("n", 0))
        rows.append({
            "track": track,
            "scored": track in track_weights,
            "static_weight": w.get("static"),
            "explore_multiplier": w.get("explore"),
            "effective_weight": w.get("effective"),
            "alpha_before": _f(b, "alpha", 1.0), "beta_before": _f(b, "beta", 1.0),
            "alpha_after": _f(a, "alpha", 1.0), "beta_after": _f(a, "beta", 1.0),
            "n_before": n_before, "n_after": n_after,
            "pulls_this_run": n_after - n_before,
            "reward_this_run": round(_f(a, "alpha", 1.0) - _f(b, "alpha", 1.0), 6),
        })
    return {"seed": int(seed), "tracks": rows, "persist_state": persist_state,
            "persisted": persist_state == "saved"}


def build_card(cand: dict, cfg: dict, run_id: str, arms: dict | None = None,
               seed: int = 0, weight_memo: dict | None = None) -> dict | None:
    title = cand.get("title", "")
    summary = cand.get("summary", "")
    body = summary + " " + " ".join(cand.get("entities", []))
    if not cand.get("track"):
        cls = classify(title, body, cfg)
    else:
        # A preset track (roster identity, §6) carries the TRACK, so classify() (track selection) is
        # skipped, but it is NEVER a license to bypass the exclude content gate. Run the SAME mute
        # check classify() would have run, so excluded content (memecoin / giveaway airdrop / crypto
        # pump / nsfw / mlm) can't slip through the X-roster lane just because it arrived with a track.
        reason = check_excluded(title, body, cfg)
        cls = {"track": cand["track"], "excluded": bool(reason), "exclude_reason": reason,
               "track_matched": True,  # a preset track (roster identity) IS a hit
               "machine_type": cand.get("machine_type", ["tool-saas"]),
               "focus_tags": cand.get("focus_tags", [])}
    if cls.get("excluded"):
        return {"_excluded": True, "title": title, "reason": cls.get("exclude_reason")}

    track = cls["track"]
    entities = cand.get("entities") or extract_entities(title + " " + summary)
    ck = canonical_key(entities, track)
    evidence = cand.get("evidence", [])
    origins = _distinct_origins(evidence)
    isc = count_independent_sources(evidence, cfg)  # transload + quote + platform-cap aware

    _side = "demand" if str(cand.get("side", "supply")).strip().lower() == "demand" else "supply"
    sc = score_opportunity(
        cand.get("score_breakdown", {}),
        isc,
        float(cand.get("age_hours", 0.0)),
        cand.get("velocity"),
        effective_track_weight(track, cfg, arms, seed, weight_memo),
        cfg,
        lifecycle_stage=cand.get("lifecycle_stage"),  # R4: feed lifecycle downweight into live scoring
        side=_side,                                    # two-column model: demand vs supply scoring
        crowdedness=cand.get("crowdedness"),           # demand-only red-ocean penalty
    )
    card = {
        "opportunity_id": opportunity_id(ck),
        "canonical_key": ck,
        "cluster_id": cand.get("cluster_id", f"cl-{now_utc().date().isoformat()}-{ck[:8]}"),
        "title": title, "summary": summary,
        "track": track, "machine_type": cls.get("machine_type", []),
        "focus_tags": cls.get("focus_tags", []),
        "evidence": evidence, "independent_source_count": isc,
        "source_set": [e.get("source") for e in evidence if e.get("source")],
        "score_breakdown": sc["score_breakdown"], "raw_score": sc["raw_score"],
        "confidence": sc["confidence"], "freshness": sc["freshness"],
        "final_score": sc["final_score"], "grade": sc["grade"],
        "why_now": cand.get("why_now", ""), "contrarian_insight": cand.get("contrarian_insight", ""),
        "action": cand.get("action", ""), "lifecycle_stage": cand.get("lifecycle_stage", ""),
        # two-column model: side routes the card to the demand/supply section; crowdedness + pain are
        # demand-side fields (pain_evidence is the concrete unmet-need quote that leads a demand card).
        "side": _side, "crowdedness": sc.get("crowdedness"), "pain_evidence": cand.get("pain_evidence", ""),
        "velocity": cand.get("velocity"),
        "delegated_deepdive": cand.get("delegated_deepdive"),
        "run_id": run_id, "schema_version": 1,
        # dual-track routing inputs (§7): track_matched distinguishes a real keyword hit from the
        # classifier's default fallback; age_hours makes the freshness gate self-contained on the
        # card. Harmless to the score/verify/archive path (extra fields are ignored downstream).
        "track_matched": bool(cls.get("track_matched", True)),
        "age_hours": float(cand.get("age_hours", 0.0) or 0.0),
    }
    return card


def _pulse_item(card: dict, cfg: dict) -> dict:
    """Shape a single-origin community card into the lightweight pulse item the digest renderer
    (§7, digest.render_community_pulse) consumes: title + source + link + one-line why, and NOTHING
    scored (no final_score/grade/score_breakdown, a rumor is never dressed as an opportunity).

    Picks the representative COMMUNITY evidence item (the origin the pulse is attributed to), so the
    origin_source tag + url + ts + heat come straight from the collected signal (the yield numerator
    carries through)."""
    comm = community_source_set(cfg)
    ev = [e for e in (card.get("evidence") or []) if isinstance(e, dict)]
    pick = next((e for e in ev
                 if (str(e.get("origin_source") or e.get("source") or e.get("origin") or "")
                     .strip().lower()) in comm),
                (ev[0] if ev else {}))
    src = pick.get("origin_source") or pick.get("source") or pick.get("origin") or card.get("track")
    return {
        "title": card.get("title") or pick.get("title") or "",
        "url": pick.get("url") or "",
        "source": pick.get("source") or src,
        "origin": pick.get("origin") or src,
        "origin_source": pick.get("origin_source") or src,
        "signal": pick.get("signal") or "",
        "ts": pick.get("ts") or "",
        "heat": pick.get("heat"),
        "text": card.get("summary") or pick.get("text") or "",
        "track": card.get("track"),
    }


# --------------------------------------------------------------------------- coverage accounting
# Every number on the digest's coverage line is either MEASURED or listed in ``unmeasured``. There is
# no third state and no placeholder: the line used to print ``源 (see SKILL run)/(see SKILL run)``,
# which reads like a value and asserts nothing, and it reported ``候选 12`` on a day the collection
# layer had handed over 625 signals.

# The keys a caller may find missing, listed in ``coverage["unmeasured"]`` when they are. A field in
# that list was NOT observed this run; it is never quietly reported as 0.
COVERAGE_FIELDS = ("signals_collected", "signals_unaccounted", "sources_invoked",
                   "sources_available", "sources_failed", "below_floor", "source_health")

# The five states scripts/sourcehealth.py can put a source in. ``fail_open_suspected`` is the one
# that exists because of brightdata: scrape_as_markdown on https://example.com returned a completely
# empty content block and search_engine for "weather today" returned {"organic":[],"current_page":1},
# both well formed, both with no error, both with zero data. A tool that cannot fetch example.com
# and still reports success is not degraded, it is LYING, and it needs a state of its own so the
# digest can shout about it. ``unknown`` is the fifth: probed and inconclusive is not the same as ok.
SOURCE_HEALTH_STATES = ("ok", "degraded", "down", "fail_open_suspected", "unknown")


def normalize_source_health(health) -> dict | None:
    """A sourcehealth.probe_all result to the coverage contract, or None when nothing was probed.

    Returns ``{"ok","degraded","down","fail_open_suspected","unknown","names_down",
    "names_fail_open"}``. Counts come from the per-source results when they are present, because the
    results are the evidence and the summary counters are a claim about them; the caller's counters
    are used only when the result list is missing. READER, so a malformed probe degrades to None
    (unmeasured) rather than to a clean bill of health."""
    if not isinstance(health, dict):
        return None
    counts = {k: 0 for k in SOURCE_HEALTH_STATES}
    names_down: list[str] = []
    names_fail_open: list[str] = []
    results = health.get("results")
    if isinstance(results, list):
        for r in results:
            if not isinstance(r, dict):
                counts["unknown"] += 1
                continue
            state = str(r.get("state") or "unknown").strip().lower()
            if state not in counts:
                state = "unknown"
            counts[state] += 1
            name = str(r.get("name") or "?")
            if state == "down":
                names_down.append(name)
            elif state == "fail_open_suspected":
                names_fail_open.append(name)
    else:
        got = False
        for k in SOURCE_HEALTH_STATES:
            v = health.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            counts[k] = int(v)
            got = True
        if not got:
            return None
        for key, sink in (("names_down", names_down), ("names_fail_open", names_fail_open)):
            v = health.get(key)
            if isinstance(v, list):
                sink += [str(x) for x in v]
    out = dict(counts)
    # Sorted, to agree with sourcehealth.coverage_block, which sorts. Both functions are documented
    # handoffs into coverage["source_health"] and run.py accepts either, so if only one sorted, the
    # SAME probe result would render two different coverage lines depending on which side flattened
    # it. Sorting here also makes this function a fixed point over its own output.
    out["names_down"] = sorted(names_down)
    out["names_fail_open"] = sorted(names_fail_open)
    return out


def candidate_signal_keys(candidates) -> set:
    """Every signal identity reachable from the candidate clusters (their evidence items)."""
    keys: set = set()
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        for e in c.get("evidence") or []:
            k = signal_key(e)
            if k:
                keys.add(k)
    return keys


def build_coverage(candidates, cards, below_sources, community_pulse, suppressed, gate, pushed,
                   collection: dict | None, health=None) -> dict:
    """The run's funnel accounting, from what the collection layer reported down to what shipped.

    ``signals_unaccounted`` is the headline number: how many origin-tagged signals the --sources leg
    collected that no candidate cluster's evidence can be traced back to. It is a REPORTED GAP, not
    an error; the clustering layer legitimately merges and discards. What is NOT acceptable, and was
    the defect, is 95% of the collected signal evaporating with the run reporting zero dropped items.

    A missing collection record (no --sources leg ran, or it ran against a different archive dir)
    yields 0 for those fields AND names them in ``unmeasured``, so "nothing was collected" and
    "nobody measured what was collected" are different outputs."""
    unmeasured: list[str] = []
    if isinstance(collection, dict):
        collected_keys = [k for k in (collection.get("signal_keys") or [])]
        signals_collected = int(collection.get("signals_collected") or 0)
        cand_keys = candidate_signal_keys(candidates)
        accounted = sum(1 for k in collected_keys if k and k in cand_keys)
        # a record that carries a count but no keys can state the count and nothing more
        if signals_collected and not collected_keys:
            unmeasured.append("signals_unaccounted")
            signals_unaccounted = 0
        else:
            signals_unaccounted = max(0, signals_collected - accounted)
        sources_invoked = int(collection.get("sources_invoked") or 0)
        sources_available = int(collection.get("sources_available") or 0)
        sources_failed = [f for f in (collection.get("sources_failed") or []) if isinstance(f, dict)]
        if not sources_available and sources_invoked:
            # the config declared no source list, so "how many SHOULD have run" is unknown; the
            # invoked count is still real.
            unmeasured.append("sources_available")
    else:
        signals_collected = 0
        signals_unaccounted = 0
        sources_invoked = 0
        sources_available = 0
        sources_failed = []
        unmeasured += ["signals_collected", "signals_unaccounted", "sources_invoked",
                       "sources_available", "sources_failed"]

    # SOURCE HEALTH. A probe that never ran is UNMEASURED, never a row of zeros: five zeros and a
    # green line is exactly what a fleet of dead sources looks like from the inside, and telling those
    # two apart is the entire point of the probe. So the value is None and the field is named in
    # ``unmeasured``.
    source_health = normalize_source_health(
        health if health is not None else
        (collection.get("source_health") if isinstance(collection, dict) else None))
    if source_health is None:
        unmeasured.append("source_health")

    gate = gate or {}
    if "below_floor" in gate:
        below_floor = list(gate.get("below_floor") or [])
    else:
        # verify_gate.gate_batch is expected to return the cards it dropped for missing their score
        # floor. An older gate that does not is reported as unmeasured, never as "none were dropped".
        below_floor = []
        unmeasured.append("below_floor")

    return {
        "signals_collected": signals_collected,
        "sources_invoked": sources_invoked,
        "sources_available": sources_available,
        "sources_failed": sources_failed,
        "source_health": source_health,
        "candidates": len(candidates or []),
        "signals_unaccounted": signals_unaccounted,
        "below_sources": list(below_sources or []),
        "community_pulse": list(community_pulse or []),
        "suppressed": len(suppressed or []),
        "below_floor": below_floor,
        "pushed": len(pushed or []),
        "deepdived": sum(1 for c in (cards or []) if c.get("delegated_deepdive")),
        # kept for the existing renderer, the count form of community_pulse
        "pulse": len(community_pulse or []),
        "unmeasured": unmeasured,
    }


def process(candidates: list[dict], cfg: dict | None = None, ledger=None,
            dry_run: bool = False, run_id: str | None = None,
            archive_dir: str | None = None, bandit_arms: dict | None = None,
            bandit_seed: int = 0, persist_bandit: bool = False,
            collection: dict | None = None, health=None) -> dict:
    cfg = cfg or load_config()
    run_id = run_id or f"daily-{now_utc().date().isoformat()}"
    min_src = int(cfg["scoring"].get("min_independent_sources", 2))

    # ---- bandit posterior load (R6 loop close): in persist mode, when arms are not passed
    # explicitly, hydrate them from the ledger so the explore-exploit posterior carries across runs.
    # Default (persist_bandit=False, no arms) stays byte-identical to the static path. ----
    if persist_bandit and bandit_arms is None and ledger is not None:
        try:
            bandit_arms = ledger.get_bandit_arms()
        except Exception:
            bandit_arms = {}
    # A constant seed makes the draw a pure function of the posterior, so two runs whose posterior
    # did not move would explore in exactly the same direction forever. Derive it from run_id when
    # the caller pinned none: re-running the same run_id redraws identically (what the byte-compare
    # suite needs), while successive days sample different corners. Bandit mode only, so the static
    # path never sees it.
    if persist_bandit and not bandit_seed:
        import bandit as bdt
        bandit_seed = bdt.run_seed(run_id)

    # ---- build + distinct-ORIGIN red line + DUAL-TRACK SPLIT (§7) ----
    # Track 1 (>=2 origins) flows on to scoring/dedup/gate as an opportunity card, unchanged. A
    # single-origin candidate is NOT dropped: route_below_gate diverts a fresh, track-relevant,
    # community-sourced rumor to the community_pulse lane (Track 2), and everything else stays a
    # reported below_sources gap.
    cards, excluded, below_sources, community_pulse = [], [], [], []
    # Per-run track-weight cache AND the bandit's decision record: the weight is a function of
    # (track, cfg, arms, seed), all constant for the run, so it is computed once per TRACK instead
    # of once per candidate, and what it computed is what bandit_report() hands back to the operator.
    track_weights: dict = {}
    for cand in candidates:
        card = build_card(cand, cfg, run_id, arms=bandit_arms, seed=bandit_seed,
                          weight_memo=track_weights)
        if card is None:
            continue
        if card.get("_excluded"):
            excluded.append(card)
            continue
        if card["independent_source_count"] < min_src:
            if route_below_gate(card, cfg) == COMMUNITY_PULSE:
                community_pulse.append(_pulse_item(card, cfg))
            else:
                below_sources.append({"title": card["title"],
                                      "isc": card["independent_source_count"]})
            continue
        cards.append(card)

    # ---- cross-day dedup against the base ledger ----
    ledger_rows = []
    if ledger is not None:
        try:
            ledger_rows = ledger.list_active()
        except Exception:
            ledger_rows = []
    new_cards, resurface_cards, suppressed = [], [], []
    # match_existing is a full scan of every ledger row (simhash + Jaccard + char n-grams) per card,
    # and the upsert loop below needs the SAME row again to carry first_seen/push_count forward. It
    # is pure in (candidate.canonical_key, title, summary) and none of those change between here and
    # there, so the second scan was a guaranteed-identical recomputation of the most expensive step
    # in process(). Keep the row instead. Identity-keyed, NOT stashed on the card: two cards can
    # share a canonical_key, and a ledger row hung on a card would ride along into every JSON the
    # card is serialized into.
    matched_rows: dict[int, dict] = {}
    for c in cards:
        matched = dd.match_existing(c, ledger_rows, cfg)
        if matched is not None:
            matched_rows[id(c)] = matched
        d = dd.decide(c, matched, cfg)
        c["_branch"] = d["branch"]
        c["_dedup_delta"] = d["delta"]
        if matched is not None:
            c["first_seen"] = dd._row_ext(matched).get(dd.EXT_PREFIX + "first_seen")
            c["push_count"] = int(dd._row_ext(matched).get(dd.EXT_PREFIX + "push_count", 0))
        if d["branch"] == dd.SUPPRESS:
            suppressed.append(c)
        elif d["branch"] == dd.RESURFACE:
            resurface_cards.append(c)
        else:
            new_cards.append(c)

    actionable = new_cards + resurface_cards

    # ---- verify gate (fail-closed) + bucketing ----
    g = gate_batch(actionable, cfg)
    pushable = g["pushable"]
    archivable = g["archivable"]

    # ---- delivery model (2026-07): ONE consolidated 'headlines' digest per day, not a message per
    # card. The old per-card push (a Discord message per pushable card, each a raw multi-line block
    # with urls) was noisy and spawned link-embed cards. We now just MARK the pushable cards as shown
    # here and render them as a single ranked headline list at the digest-deliver step below; the full
    # cards + links stay in the archived digest file. No per-card network call.
    pushed = []
    for c in pushable:
        c["pushed"] = True
        c["push_count"] = int(c.get("push_count", 0)) + 1
        c["push_ts"] = iso(now_utc())
        pushed.append(c)

    # ---- archive (quality-gated) ----
    # dry_run threads through: preview re-asserts the archive quality gate but writes nothing, so a
    # test/preview run with $DAILY_HOTSPOTS_CONFIG set can't leak fake cards into the real archive.
    archived = []
    for c in archivable:
        status, detail = ar.archive_card(c, archive_dir, cfg, dry_run=dry_run)
        if status in ("archived", "would-archive"):
            c["archived"] = True
            archived.append(c["title"])

    # ---- bandit reward feedback (R6 run.py wiring): close the explore-exploit loop. Each track's
    # Beta-Bernoulli arm learns from this run's REALIZED outcome (pushed > archived > blocked/score),
    # so a track that keeps producing pushable opportunities earns more lift next run and a cold one
    # decays. PURE: the input arms are never mutated; we emit the NEXT arms for the orchestration
    # layer to persist (ledger persistence kept out of this deterministic core, like catch_up_digests).
    # Only ACTIONABLE cards (real gate outcomes) update an arm, suppressed/below-source/excluded
    # candidates never had an outcome and must not teach the bandit anything.
    bandit_arms_next = None
    if bandit_arms is not None:
        import bandit as bdt
        bandit_arms_next = {k: dict(v) for k, v in (bandit_arms or {}).items()}
        blocked_titles = {b.get("title") for b in g["blocked"]}
        for c in actionable:
            track = c.get("track")
            if not track:
                continue
            if c.get("title") in blocked_titles:
                c["blocked"] = True
            r = bdt.outcome_reward(c, cfg)
            arm = bandit_arms_next.get(track) or bdt.init_arm(cfg)
            bandit_arms_next[track] = bdt.update_arm(arm, r, cfg)

    # ---- side-effect error accumulator: the watermark only advances after EVERY ledger/digest
    # write on this run succeeded (SKILL Hard-rule #4 atomicity / audit MEDIUM#1). A swallowed
    # exception must NOT let the watermark move past a slot that was never actually covered, or the
    # next run would treat the failed item as "already done" and silently drop it.
    errors: list[dict] = []

    # ---- ledger upsert (NEW + RESURFACE + SUPPRESS get a sample; idempotent UPSERT) ----
    if ledger is not None and not dry_run:
        for c in actionable + suppressed:
            matched = matched_rows.get(id(c))
            prior = dd._row_ext(matched) if matched else {}
            sample = {"ts": iso(now_utc()), "score": c.get("final_score"),
                      "n_sources": c.get("independent_source_count"),
                      "velocity": c.get("velocity"), "stage": c.get("lifecycle_stage", "")}
            ext = dd.build_ext(c, sample, prior, cfg)
            if c.get("pushed"):
                ext[dd.EXT_PREFIX + "push_count"] = int(c.get("push_count", 0))
            try:
                ledger.upsert(c, ext)
            except Exception as e:  # recorded, not swallowed, gates the watermark below
                errors.append({"stage": "upsert", "key": c.get("canonical_key"), "err": repr(e)[:200]})

    # ---- cross-day community-pulse dedup (§7 "no rumor re-bubbles"): load the prior-shown rumor
    # keys (a bounded {pulse_key: last_shown_iso} singleton on the base ledger, mirroring the
    # watermark) so a single-source rumor rendered on an earlier day is SUPPRESSED in today's digest
    # rather than re-rendered as fresh every day until a 2nd origin escalates it to a scored card.
    # Read-only + defensive here (an absent/partial ledger degrades to no suppression), so it runs on
    # dry-run previews too; the write-back is gated on a clean, non-dry run below.
    pulse_seen_prior = {}
    if ledger is not None:
        try:
            pulse_seen_prior = ledger.get_pulse_seen() or {}
        except Exception:
            pulse_seen_prior = {}
    pulse_seen_keys = dg.active_pulse_seen_keys(pulse_seen_prior, now_utc(), cfg)

    # ---- coverage accounting (see build_coverage): the funnel from collected signal to shipped
    # card, with every unmeasured field named. ``collection`` is injectable for tests; by default it
    # is read back from the collection ledger the --sources leg wrote for this same run_id.
    coll = collection if collection is not None else load_collection(run_id, archive_dir)
    coverage = build_coverage(candidates, cards, below_sources, community_pulse, suppressed,
                              g, pushed, coll, health=health)

    # ---- digest (idempotent item + file + deliver) ----
    # Track 2 (§7): the community-pulse rumors render as their own section AFTER the cards (and even
    # on an otherwise-empty card day). build_markdown forwards seen_keys so cross-day-seen rumors
    # never re-bubble into the pushed digest.
    md = dg.build_markdown(archivable, coverage, pulse=community_pulse, cfg=cfg,
                           seen_keys=pulse_seen_keys)
    digest_path = None
    if not dry_run:
        try:
            digest_path = str(dg.write_digest_file(md, archive_dir))
        except Exception as e:
            digest_path = None
            errors.append({"stage": "digest_file", "err": repr(e)[:200]})
        if ledger is not None:
            try:
                dg.register_digest_item(ledger, summary=f"{len(archivable)} cards, {len(pushed)} pushed")
            except Exception as e:
                errors.append({"stage": "digest_item", "err": repr(e)[:200]})
            # persist THIS run's rumor keys so tomorrow suppresses them (mirrors the watermark/bandit
            # singleton). Only on a clean run (no prior side-effect error) so a partial failure never
            # bakes in a half-recorded dedup state; a failure here holds the watermark for retry.
            # Stamp ONLY the rumors the digest ACTUALLY rendered (the capped, deduped subset), NOT the
            # full pre-cap candidate list: the community_pulse.max_per_day cap DEFERS overflow rumors
            # to a later day (they re-rank next run), so marking an un-shown item "seen" would suppress
            # it forever without ever displaying it (§7 cap defers, never drops). select_rendered_pulse
            # mirrors the renderer's exact gate+dedup+cap using the same cfg + cross-day seen keys.
            if community_pulse and not errors:
                rendered_pulse = dg.select_rendered_pulse(community_pulse, cfg=cfg,
                                                          seen_keys=pulse_seen_keys)
                try:
                    ledger.set_pulse_seen(dg.merge_pulse_seen(pulse_seen_prior, rendered_pulse,
                                                              now_utc(), cfg))
                except Exception as e:
                    errors.append({"stage": "pulse_seen", "err": repr(e)[:200]})
    # Deliver ONLY the compact headlines: the top `max_per_day` (default 5) of ALL qualifying
    # (archivable) opportunities ranked by score, a consistent top-N briefing, not just the strict
    # immediate-push subset. The full `md` is written to the archive file above and (once committed by
    # the wrapper) linked as the 完整版 GitHub URL. We never push the raw markdown to the channel.
    digest_url = dg.digest_github_url(digest_path) if not dry_run else ""
    headlines = dg.build_headlines(archivable, coverage,
                                   cap=int((cfg.get("push", {}) or {}).get("max_per_day", 5)),
                                   digest_url=digest_url)
    pc.deliver(headlines, dry_run=dry_run)

    # ---- bandit posterior save (R6 loop close): persist the learned arms ONLY on a clean run, so
    # a partial failure does not bake in a half-learned posterior (same atomicity as the watermark).
    bandit_persist_state = "not-requested" if not persist_bandit else (
        "no-ledger" if ledger is None else "dry-run" if dry_run else
        "no-arms" if bandit_arms_next is None else "held-errors" if errors else "pending")
    if bandit_persist_state == "pending":
        try:
            ledger.set_bandit_arms(bandit_arms_next)
            bandit_persist_state = "saved"
        except Exception as e:
            bandit_persist_state = "failed"
            errors.append({"stage": "bandit_persist", "err": repr(e)[:200]})

    # ---- atomic watermark (advances ONLY when the full success path was clean) ----
    watermark_advanced = False
    if ledger is not None and not dry_run:
        if not errors:
            try:
                ledger.add_watermark(iso(now_utc()))
                watermark_advanced = True
            except Exception as e:
                errors.append({"stage": "watermark", "err": repr(e)[:200]})
        # else: a side-effect failed this run -> hold the watermark so the failed slot is retried.

    res = {
        "run_id": run_id,
        "candidates": len(candidates),
        "built": len(cards),
        "excluded": len(excluded),
        "below_sources": below_sources,
        "community_pulse": community_pulse,
        "new": len(new_cards), "resurface": len(resurface_cards), "suppressed": len(suppressed),
        "blocked": g["blocked"],
        "pushed": [c["title"] for c in pushed],
        "archived": archived,
        "empty_day": len(archivable) == 0,
        "coverage": coverage,
        "digest_path": digest_path,
        "digest_markdown": md,
        "errors": errors,
        "watermark_advanced": watermark_advanced,
        "bandit_arms_next": bandit_arms_next,
    }
    # The bandit block exists ONLY when the bandit ran. Emitting an empty/None one on every run
    # would change the static path's output bytes for a feature that did nothing, and would make
    # "off" indistinguishable from "on but it decided nothing".
    if bandit_arms is not None:
        res["bandit"] = bandit_report(bandit_arms, bandit_arms_next, track_weights,
                                      bandit_seed, bandit_persist_state)
    return res


def _run_sources(a) -> int:
    """`--sources <file>`: the DENOMINATOR-writer entry point (spec §5.1/§6/§8), the piece that keeps
    the self-evolve yield engine from being inert.

    The SKILL's live MCP fan-out (twitterapi get_user_last_tweets over the roster + the community
    lanes) hands its RAW responses here as a JSON payload; run.py does the deterministic remainder ,
    origin-tag every signal (collect_sources) AND append one pulls-log line per pulled handle/source
    (append_pulls, the yield DENOMINATOR). Without this call on the daily path the pulls-log is never
    written, every handle's yield stays UNKNOWN forever, and auto-prune can never fire. Emits the
    origin-tagged signals for the SKILL to fold into its candidate clustering. ``--dry-run`` writes no
    pulls-log line (preview never inflates the denominator).

    Payload shape (all keys optional): ``{"roster_responses": {handle: raw}, "community": {source:
    [normalized items]}, "new_sources": {lane: raw response}, "health": <sourcehealth.probe_all
    result>, "last_run": "...Z"}``. ``new_sources`` lanes are parsed HERE (NEW_SOURCE_PARSERS), so a
    dead demand lane arrives as a structural failure instead of an empty list; ``health`` is stored
    on the collection record so the digest can report which sources were probed and what they said.

    Writes three things, all skipped by ``--dry-run``: the pulls-log denominator, the pull-errors
    ledger for anything that failed, and the collection record build_coverage replays."""
    cfg = load_config()
    raw = open(a.sources, encoding="utf-8").read() if a.sources != "-" else sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        # The payload comes from the LLM orchestration layer, which can wrap JSON in prose or emit a
        # trailing comma. Fail as a STRUCTURED error with rc=1 (the caller retries) instead of an
        # unhandled traceback; no pulls-log line is written this run (the denominator gaps, honestly).
        print(json.dumps({"error": "malformed sources payload", "detail": str(e)[:200],
                          "pulls_written": 0}, ensure_ascii=False))
        return 1
    if not isinstance(payload, dict):
        payload = {}
    roster = rt.load_roster(path=a.roster or None)
    out = collect_sources(roster=roster,
                          roster_responses=payload.get("roster_responses"),
                          community=payload.get("community"),
                          cfg=cfg, last_run=payload.get("last_run"),
                          run_id=a.run_id or None,
                          new_sources=payload.get("new_sources"))
    path = append_pulls(out["pulls"], a.archive_dir or None, dry_run=a.dry_run)
    # WRITE THE COLLECTION RECORD. reference/push-archive.md and reference/collect.md have both
    # documented ``collection-YYYY-MM.jsonl`` as a product of ``run.py --sources`` since the gap
    # ledger was built, and nothing wrote it: build_collection_record existed, append_collection
    # existed, and no entry point ever called either. So process() found no record for the run_id,
    # build_coverage took the "nobody measured this" branch every single day, and the whole of
    # sources_failed, signals_collected and signals_unaccounted printed as unmeasured forever. That
    # is the safe failure of the two (it never claimed a false zero), but it also means the failed
    # source list this task exists to surface had no route to the digest at all. This is that route.
    coll = build_collection_record(out, cfg=cfg, run_id=out["run_id"],
                                   health=payload.get("health"))
    cpath = append_collection(coll, a.archive_dir or None, dry_run=a.dry_run)
    _, failed = split_pulls(out["pulls"])
    print(json.dumps({"run_id": out["run_id"], "signals": out["signals"],
                      "pulls_written": len(out["pulls"]),
                      "pulls_log": str(path) if path else None,
                      "collection_log": str(cpath) if cpath else None,
                      # the SKILL orchestration layer retries on THIS: name, error, attempts and
                      # outcome per failed unit, on the day it happened.
                      "sources_failed": coll["sources_failed"],
                      "filtered": out.get("filtered") or {}},
                     ensure_ascii=False))
    # A failed lane is not a failed RUN (the other lanes still collected, and their denominator
    # lines are written and correct), so this stays rc=0 and the failure travels as data. Exit codes
    # are for "this pass could not do its job"; a half dead fan out did do its job, honestly.
    return 0


def _run_yield(a) -> int:
    """`--yield`: the weekly signal-yield pass entry point the spec §8 names ("runnable as run.py
    --yield or standalone"). Delegates to yield.py (imported by name, ``yield`` is a keyword) so the
    daily-radar CLI is the single documented surface for both the pipeline and its self-evolve loop."""
    Y = importlib.import_module("yield")
    yargs: list[str] = []
    if a.archive_dir:
        yargs += ["--archive-dir", a.archive_dir]
    if a.roster:
        yargs += ["--roster", a.roster]
    if a.apply:
        yargs.append("--apply")
    if a.write_review:
        yargs.append("--write-review")
    if a.user_info:
        yargs += ["--user-info", a.user_info]
    rc = Y.main(yargs)
    # §8/§4 weekly cadence: leave the idempotent per-ISO-week ledger item
    # (daily-hotspots:yield:<week>), the WEEKLY mirror of the daily digest item. Best-effort, a
    # missing/unreachable schedule-reminder base must never fail the deterministic replay, and
    # skipped under --no-ledger (offline / tests) so it never touches a live base implicitly.
    if rc == 0 and not a.no_ledger:
        try:
            led = dd.LedgerClient()
            led.init()
            Y.register_yield_item(led, summary="weekly yield pass")
        except Exception:
            pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--archive-dir", default="")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--catch-up", action="store_true",
                    help="R5: backfill missed daily-digest items since the last watermark, then exit "
                         "(idempotent; for the cron/orchestration layer after an oversleep)")
    # Source-coverage self-evolve wiring (spec §5.1/§6/§8): make the pulls-log DENOMINATOR writer and
    # the weekly yield pass reachable from the daily-radar CLI (the wrapper/cron path), not just from
    # yield.py standalone, otherwise the engine is inert (audit HARDEN r4).
    ap.add_argument("--sources", default="",
                    help="write the pulls-log denominator + emit origin-tagged signals from raw "
                         "roster/community MCP responses (JSON file, or '-' for stdin); spec §5.1/§6/§8")
    ap.add_argument("--yield", dest="do_yield", action="store_true",
                    help="run the weekly signal-yield pass (spec §8/§9) instead of the candidate "
                         "pipeline (delegates to yield.py)")
    ap.add_argument("--apply", action="store_true",
                    help="(with --yield) apply the reversible auto-prune to roster.json")
    ap.add_argument("--write-review", action="store_true",
                    help="(with --yield) write archive/roster-review.md")
    ap.add_argument("--roster", default="",
                    help="explicit roster.json path (with --sources / --yield); default = config probe")
    ap.add_argument("--user-info", default="",
                    help="(with --yield) get_user_info sweep JSON {handle: info} -> identity flags (§9)")
    ap.add_argument("--bandit", action="store_true",
                    help="R6: rank with the exploration-adjusted (Thompson) track weight, learn from "
                         "this run's outcomes and persist the posterior to the ledger. OFF by "
                         "default; scoring.bandit.enabled=true in config turns it on permanently. "
                         "The run result then carries a 'bandit' block with every draw it made.")
    a = ap.parse_args()

    # Self-evolve entry points short-circuit BEFORE the candidate-stdin read + ledger init (they read
    # neither): the weekly yield replay and the per-run denominator writer are their own passes.
    if a.do_yield:
        return _run_yield(a)
    if a.sources:
        return _run_sources(a)

    candidates = []
    if not a.catch_up:  # catch-up backfills digests from the ledger; it reads no candidate input
        raw = open(a.infile, encoding="utf-8").read() if a.infile else sys.stdin.read()
        try:
            candidates = json.loads(raw or "[]")
        except json.JSONDecodeError as e:
            # The candidate JSON comes from the LLM orchestration layer (prose-wrapped / trailing-comma
            # output is a known flake). Fail LOUD but GRACEFULLY: a structured error + rc=1 so the day
            # is held for retry (process() never runs -> the watermark cannot advance past an uncovered
            # slot), instead of an unhandled JSONDecodeError traceback.
            print(json.dumps({"error": "malformed candidate JSON", "detail": str(e)[:200],
                              "watermark_advanced": False}, ensure_ascii=False))
            return 1
        if isinstance(candidates, dict):
            candidates = candidates.get("candidates", [])

    cfg = load_config()
    ledger = None if a.no_ledger else dd.LedgerClient()
    if ledger is not None:
        try:
            ledger.init()
        except Exception:
            ledger = None
    if a.catch_up:
        if ledger is None:
            print(json.dumps({"catch_up": [], "error": "no ledger (schedule-reminder base required)"}))
            return 1
        dates = dg.catch_up_digests(ledger, ledger.get_watermark())
        print(json.dumps({"catch_up": dates}, ensure_ascii=False))
        return 0
    # THE R6 entry point. Before this the bandit was unreachable: process() took persist_bandit but
    # no flag ever set it, so the learning loop had never turned once in production. Two ways in and
    # both are explicit: --bandit for one run, scoring.bandit.enabled for good. Neither on by
    # default, so the shipped static path is unchanged.
    import bandit as bdt
    persist_bandit = bool(a.bandit) or bdt.bandit_enabled(cfg)
    res = process(candidates, cfg, ledger, dry_run=a.dry_run,
                  run_id=a.run_id or None, archive_dir=a.archive_dir or None,
                  persist_bandit=persist_bandit)
    res.pop("digest_markdown", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # EXIT CODE IS PART OF THE REPORT. process() does not raise on a side-effect failure, it records
    # the stage in ``errors`` and HOLDS the watermark so the day is retried. Returning 0 anyway made
    # that hold invisible to the only thing the cron wrapper actually reads: a day whose digest file
    # was never written, whose ledger upsert failed, or whose watermark never advanced exited exactly
    # like a day that shipped, so nothing ever retried and nobody was ever told. Any recorded error
    # is a nonzero exit; the full structured detail is already on stdout above.
    return 1 if res.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
