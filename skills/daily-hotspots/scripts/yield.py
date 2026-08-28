#!/usr/bin/env python3
"""yield.py -- the self-evolve signal-yield engine (design spec sections 8 and 9).

Replays the append-only archive (ZERO new state store, Approach A) to keep the X KOL roster and
the community sources honest over time:

  numerator   = archive evidence tagged ``origin_handle`` / ``origin_source`` that reached a
                pushed/archived card (``archive/opportunities.jsonl``).
  denominator = per-run per-handle/source pulled-count log (``archive/pulls-*.jsonl``); one line
                per (run, handle/source), so the count of lines is the number of pull events.
  yield[X]    = contributions[X] / pulls[X] over a rolling window (default 30 days).

Two semi-automatic decisions (self-evolve autonomy: pure reversible subtraction is automatic, any
addition is human-gated):

  * AUTO-PRUNE (section 8): a rostered handle whose weekly contributions stay at/below ``floor``
    (default 0) for ``prune_after_weeks`` (default 2) CONSECUTIVE, fully-observed weeks is disabled
    via roster.set_enabled (``enabled=false``, never a delete, reversible), logged with reason and
    stats.
  * PROPOSE-ADD (section 8): handles that appear in evidence but are NOT in the roster, ranked by
    frequency, are written to ``archive/roster-review.md`` for a human to approve. NEVER auto-added.

Anti-self-deception guardrails (section 9), all enforced here:
  * only auto-PRUNE, never auto-ADD (no echo-chamber self-reinforcement);
  * report-only until >= ``min_history_days`` (default 7) of real history (honest cold-start);
  * prune is reversible (enabled=false, surfaced in the review queue for un-pruning);
  * thresholds are config (watchlist.json ``yield`` block), not hardcoded (methodology constant,
    thresholds tunable);
  * NEVER fabricate: a handle/source with a missing pulls-log entry gets ``yield=None`` (unknown,
    NOT 0) and is excluded from prune consideration.

The compute core is PURE (clock/network-free) so the acceptance-gate suite can byte-compare;
archive/roster I/O is isolated at the edges and never touches the live config in report-only mode.
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import timedelta
from pathlib import Path

from lib import iso, load_config, now_utc, parse_ts
from roster import entries_of, find_entry, load_roster, normalize_handle, save_roster, set_enabled
from archive import resolve_archive_dir

# --------------------------------------------------------------------------- config surface

# Origin kinds: an X account handle vs a community source (linux.do / v2ex / qbitai / ...).
KIND_HANDLE = "handle"
KIND_SOURCE = "source"

# Fallback thresholds. The live tunable surface is watchlist.json's ``yield`` block (lib defaults
# carry the same values); a user value deep-merges over these. Kept as a module constant only so a
# call with an empty/partial config still has a defined floor -- the same pattern roster.py uses for
# DEFAULT_MIN_FAVES_ROSTERED. Methodology is constant; every threshold here is overridable by config.
DEFAULT_YIELD_CONFIG = {
    "window_days": 30,             # rolling window for the yield ratio + propose-add frequency
    "floor": 0,                    # max weekly contributions still counted as "below floor" (dead)
    "prune_after_weeks": 2,        # consecutive fully-observed below-floor weeks that trigger prune
    "min_history_days": 7,         # cold-start gate: report-only until this much real history
    "propose_add_min_count": 2,    # min distinct-record frequency to propose a non-roster handle
    "pre_viral_faves_threshold": 500,   # keyword-search faves floor a rostered pull catches under
    "noisy_pull_min": 10,          # a high-pull handle this busy...
    "noisy_yield_max": 0.1,        # ...but this low-yield gets a SUGGESTED topic_filter (propose)
    # ABSOLUTE per-week coverage floor for "fully observed" (§8/§9). 0 = use only the RELATIVE bar
    # (a week must be observed at least as well as this origin's own best week in the prune span,
    # see required_observed_days). A positive value additionally demands that many distinct pulled
    # DAYS in every prune-span week; 7 gives literal calendar-week coverage. Tunable UP only.
    "min_observed_days_per_week": 0,
}

# A 7-day bucket is "fully observed" at 7 distinct pulled days. Used to REPORT how far short a prune
# decision's weeks fell, even when the relative bar let the decision through.
FULL_WEEK_DAYS = 7


def _coerce_num(val, default):
    """Coerce a config threshold to a FINITE number, degrading anything else back to ``default``.

    A JSON typo like ``"floor": "0"`` (a string) must not reach a downstream comparison, decide_prune
    does ``c <= floor`` and ``int <= str`` raises TypeError, which would take the whole weekly yield
    pass (and the report) down. An int default stays int (floor/window/weeks are counts) so the
    report and the comparisons remain integral; a bool is never a valid threshold.

    NON-FINITE guard (audit HARDEN r4): ``json.loads`` accepts JSON ``NaN`` / ``Infinity`` / ``1e999``
    by default, yielding python ``float('nan')`` / ``float('inf')``. Those survive lib._clamp_guardrails
    (``inf >= floor`` is True, so the floor keeps them) and then blow up a DOWNSTREAM ``int(...)``:
    ``int(inf)`` -> OverflowError, ``int(nan)`` -> ValueError (window_days in compute_yield,
    propose_add_min_count in decide_propose_add, prune_after_weeks/noisy_pull_min elsewhere), aborting
    the entire pass with no report / no prune / no propose-add. A string ``"inf"`` / ``"1e999"`` even
    made THIS function raise at ``int(f)``. So a value that does not resolve to a finite number (nan,
    +-inf, or an int so astronomically large it overflows ``float``) degrades to the shipped default ,
    the docstring's promise (a garbled threshold can never crash the pass) made true for non-finite too.
    Because yield_cfg coerces EVERY DEFAULT_YIELD_CONFIG key through here, this one guard neutralizes a
    non-finite value in ANY yield knob."""
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        try:
            f = float(val)                     # an astronomically large int can overflow float()
        except (OverflowError, ValueError):
            return default
        if not math.isfinite(f):
            return default                     # NaN / +-Infinity (JSON permits them) -> default
        return val
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default                         # "inf" / "nan" / "1e999" strings -> default
    return int(f) if (isinstance(default, int) and f == int(f)) else f


def yield_cfg(cfg: dict | None) -> dict:
    """Resolve the effective yield thresholds: module defaults overlaid by the config ``yield`` block.

    Reads only; never mutates ``cfg``. An absent or malformed block degrades to the module defaults,
    and EVERY known threshold is coerced to a number (a non-numeric config value falls back to its
    default) so the engine always has a defined, comparison-safe floor, methodology constant, a
    garbled threshold can never crash the pass."""
    y = dict(DEFAULT_YIELD_CONFIG)
    if isinstance(cfg, dict):
        blk = cfg.get("yield")
        if isinstance(blk, dict):
            for k, v in blk.items():
                y[k] = v
    for k, default in DEFAULT_YIELD_CONFIG.items():
        y[k] = _coerce_num(y.get(k), default)
    return _clamp_yield_guardrails(y)


def _clamp_yield_guardrails(y: dict) -> dict:
    """Re-impose the §9 anti-mass-prune rails at the YIELD-ENGINE boundary so they hold BY
    CONSTRUCTION, not by caller convention (audit HARDEN round 2).

    lib.load_config._clamp_guardrails already tightens these when the config is loaded the live way,
    but yield_cfg/run_yield honor whatever cfg they are handed, a future ``run.py --yield`` wiring, a
    schedule-reminder subprocess, or a hand-built test cfg could otherwise route around load_config
    and GUT the roster in one --apply run. The three prune-facing thresholds may only be made STRICTER
    than the shipped defaults, never looser (the direction that would mass-prune):

      * floor, higher floor = more handles read "dead"      -> CAP at the default (0)
      * prune_after_weeks, fewer weeks = faster prune                   -> FLOOR at the default (2)
      * min_history_days, less history = weaker cold-start guard       -> FLOOR at the default (7)

    A user may still tighten floor/prune_after_weeks/min_history_days (prune slower / require more
    history). ``window_days`` is ALSO a §1/§9 rail (see below): tunable UP (a longer window = more
    sparing = the safe direction), floored DOWN so it can never weaken the pre-viral guard.
    ``pre_viral_faves_threshold`` is a §1/§9 rail TOO (audit HARDEN round 4): it is the engagement
    ceiling below which a caught post counts as a PRE-VIRAL catch (the metric the prune guard reads
    to spare a handle, §8). A HIGHER threshold counts MORE catches as pre-viral -> spares MORE handles
    (safe); a value AT/BELOW a handle's real fave counts makes ``float(faves) < thr`` never true, so
    ``pre_viral`` collapses to 0 for EVERY handle, blinding the §1/§9 guard while decide_prune still
    prunes, the exact mass-prune direction the other rails are clamped against. §8 also DEFINES the
    metric relative to the keyword search's own min_faves:500 floor ("surfaced below min_faves:500"),
    so the shipped default IS the semantic reference. Floored at the default: tunable UP (more
    sparing), never DOWN (never blind the guard). NOTE this floor lives ONLY here (the yield-engine
    boundary): roster._min_faves_rostered_cap reads the RAW cfg value where a LOWER threshold
    LEGITIMATELY tightens the min_faves_rostered cap, and yield_cfg returns a fresh dict without
    mutating cfg, so the two uses stay independent, the guard is protected without disturbing the
    roster cap. The remaining knobs (propose_add_min_count, noisy_*) stay tunable both ways.
    Idempotent, the values are already numbers (coerced upstream) and re-clamping is a no-op."""
    d = DEFAULT_YIELD_CONFIG
    if y["floor"] > d["floor"]:
        y["floor"] = d["floor"]                       # cap: never count more handles as dead
    if y["prune_after_weeks"] < d["prune_after_weeks"]:
        y["prune_after_weeks"] = d["prune_after_weeks"]   # floor: never prune faster than default
    if y["min_history_days"] < d["min_history_days"]:
        y["min_history_days"] = d["min_history_days"]     # floor: never weaken the cold-start guard
    if y["pre_viral_faves_threshold"] < d["pre_viral_faves_threshold"]:
        y["pre_viral_faves_threshold"] = d["pre_viral_faves_threshold"]  # floor: never blind pre-viral guard
    # min_observed_days_per_week is an anti-mass-prune rail too: it is the ABSOLUTE coverage a week
    # must have before it may count toward a prune. LOWER = weeks with almost no coverage count as
    # "fully observed" = the mass-prune direction. Floored at the shipped default (0, which still
    # leaves the RELATIVE bar in force) and capped at a real calendar week (asking for more than 7
    # distinct days inside a 7-day bucket is unsatisfiable and would silently disable pruning
    # forever, an un-auditable no-op rather than a guard).
    if y["min_observed_days_per_week"] < d["min_observed_days_per_week"]:
        y["min_observed_days_per_week"] = d["min_observed_days_per_week"]
    if y["min_observed_days_per_week"] > FULL_WEEK_DAYS:
        y["min_observed_days_per_week"] = FULL_WEEK_DAYS
    # window_days is the reach of the §1/§9 PRE-VIRAL GUARD: decide_prune spares a handle whose
    # pre_viral > 0 anywhere in compute_yield's window_days window, EVEN WHEN the last
    # prune_after_weeks weeks read quiet. Shrink window_days and the guard goes BLIND while decide_prune
    # still prunes off its FIXED 7*prune_after_weeks weekly buckets, a demonstrated pre-viral catcher
    # gets auto-disabled (reproduced: a catch 18 days back spares the handle at the default 30 but is
    # PRUNED at window_days 7 / 0). Note the guard's UNIQUE protection is precisely for catches OLDER
    # than the prune window (a catch INSIDE it already shows as a weekly contribution and spares the
    # handle without the guard), so flooring merely AT the prune span would neuter the guard. Floor at
    # max(shipped default, prune span): never below the reach the guard was calibrated to (30d), and ,
    # if prune_after_weeks was raised, never below the enlarged prune window either. A <=0 window
    # (start>=end -> empty) is lifted by the same floor. A LARGER window stays honored.
    prune_span = 7 * int(y["prune_after_weeks"])
    guard_floor = max(int(d["window_days"]), prune_span)
    if y["window_days"] < guard_floor:
        y["window_days"] = guard_floor                # floor: the pre-viral guard keeps its full reach
    return y


# --------------------------------------------------------------------------- origin keys (pure)

def okey(kind: str, name: str) -> str:
    """Namespaced string key for a report dict: ``handle:karpathy`` / ``source:linux.do``.

    Handles and sources live in one dict but can never collide (twitter handles have no dot; a
    source label like linux.do is never a valid handle)."""
    return f"{kind}:{name}"


def _norm_handle_key(h: str) -> str:
    return normalize_handle(h).lower()


def _norm_source_key(s: str) -> str:
    return (s or "").strip().lower()


def evidence_origins(evidence) -> set:
    """Distinct ``(kind, name)`` origin tuples tagged on ONE card's evidence list.

    Names are case-folded (and handles have a leading '@' stripped) so a pulls-log entry and an
    evidence tag for the same account always align. A card is counted ONCE per distinct origin even
    if several of its evidence items carry the same handle."""
    out: set = set()
    if not isinstance(evidence, list):
        return out
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        h = ev.get("origin_handle")
        if isinstance(h, str) and h.strip():
            out.add((KIND_HANDLE, _norm_handle_key(h)))
        s = ev.get("origin_source")
        if isinstance(s, str) and s.strip():
            out.add((KIND_SOURCE, _norm_source_key(s)))
    return out


def pull_origin(line) -> tuple | None:
    """The ``(kind, name)`` a pulls-log line accounts for, or None if it names neither."""
    if not isinstance(line, dict):
        return None
    h = line.get("handle")
    if isinstance(h, str) and h.strip():
        return (KIND_HANDLE, _norm_handle_key(h))
    s = line.get("source")
    if isinstance(s, str) and s.strip():
        return (KIND_SOURCE, _norm_source_key(s))
    return None


def _opp_id(rec) -> str:
    """Per-OPPORTUNITY identity for numerator dedup, the yield engine counts "once per card" (§8).

    ``opportunities.jsonl`` is append-only and a RESURFACED card is re-archived every day it
    re-surfaces (archive._jsonl_record stamps a fresh ``last_seen``), so ONE opportunity becomes many
    lines sharing a single ``opportunity_id`` / ``canonical_key``. Counting raw lines would
    triple-count one story, inflating a rostered handle's yield and pushing a non-roster handle over
    ``propose_add_min_count`` on the strength of a single resurfacing story (audit HARDEN). We collapse
    by ``opportunity_id``, then ``canonical_key``; an untagged record falls back to its object id so it
    is NEVER merged with an unrelated record (no false collapse)."""
    if isinstance(rec, dict):
        oid = rec.get("opportunity_id")
        if isinstance(oid, str) and oid.strip():
            return "op:" + oid.strip()
        ck = rec.get("canonical_key")
        if isinstance(ck, str) and ck.strip():
            return "ck:" + ck.strip()
    return "id:" + str(id(rec))


# --------------------------------------------------------------------------- window helpers (pure)

def _rec_ts(rec: dict) -> str | None:
    """A card record's effective timestamp (when this archive line was written / last surfaced)."""
    if not isinstance(rec, dict):
        return None
    return rec.get("last_seen") or rec.get("first_seen")


def _in_window(ts, start, end) -> bool:
    """Half-open [start, end) membership; unparseable timestamps are simply out of window."""
    if not ts:
        return False
    try:
        d = parse_ts(ts)
    except Exception:
        return False
    return start <= d < end


_FAVE_KEYS = ("faves", "like_count", "likes", "favorite_count", "favoriteCount", "likeCount")


def _has_engagement(ev) -> bool:
    """True if ONE evidence item carries any key ``_evidence_is_pre_viral`` can actually read."""
    if not isinstance(ev, dict):
        return False
    for k in _FAVE_KEYS:
        if k in ev:
            try:
                float(ev[k])
            except (TypeError, ValueError):
                continue
            return True
    return False


def pre_viral_observability(records, now, ycfg: dict) -> dict:
    """Is the §1/§9 pre-viral prune guard ALIVE on this archive, or is it reading keys nobody writes?

    The guard spares a rostered handle that surfaced a founder's post below the keyword faves floor.
    It can only ever fire if the ARCHIVED evidence carries an engagement count (one of ``_FAVE_KEYS``).
    On the live archive it does NOT: the collect layer tags ``origin_handle`` but drops the fave count
    before ``archive/opportunities.jsonl`` is written, so every origin evaluates ``pre_viral == 0`` and
    the guard has never spared anything. A guard that cannot fire but LOOKS like protection is worse
    than no guard, so its liveness is now MEASURED and reported on every pass:

        {"state": "live"|"inert"|"empty", "engagement_keys": [...],
         "evidence_items": N, "with_engagement": M, "origins": O, "origins_with_engagement": P}

    ``state`` is ``empty`` when there is no in-window origin-tagged evidence at all (nothing to judge,
    which is NOT the same as a guard that ran and found nothing, §"clean != did not check"), ``inert``
    when evidence exists but not ONE item carries a readable engagement key, and ``live`` otherwise.
    PURE: reads records, mutates nothing."""
    window_days = int(ycfg["window_days"])
    end = now
    start = now - timedelta(days=window_days)
    items = 0
    with_eng = 0
    origins: set = set()
    origins_eng: set = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if not _in_window(_rec_ts(rec), start, end):
            continue
        evs = rec.get("evidence")
        if not isinstance(evs, list):
            continue
        for ev in evs:
            o = evidence_origins([ev])
            if not o:
                continue
            items += 1
            origins |= o
            if _has_engagement(ev):
                with_eng += 1
                origins_eng |= o
    if items == 0:
        state = "empty"
    elif with_eng == 0:
        state = "inert"
    else:
        state = "live"
    return {"state": state, "engagement_keys": list(_FAVE_KEYS), "evidence_items": items,
            "with_engagement": with_eng, "origins": len(origins),
            "origins_with_engagement": len(origins_eng)}


def _evidence_is_pre_viral(evidence, origin_t: tuple, thr: float) -> bool:
    """True if any evidence item tagged ``origin_t`` carries an engagement count below ``thr``.

    The pre-viral-catch metric (section 8): a rostered pull surfaces a founder's post by identity
    before it clears the keyword-search faves floor -- a signal keyword search would have dropped.
    Best-effort: if no engagement field is present the item simply does not count.

    WARNING, read ``pre_viral_observability`` before trusting a 0 here: on an archive whose evidence
    carries NO engagement key at all (the live one, today) this returns False for EVERY item and the
    metric is structurally 0, which is UNKNOWN, not "no pre-viral catches". run_yield reports that
    state as ``pre_viral_guard.state == "inert"`` so a 0 is never read as a measurement."""
    if not isinstance(evidence, list):
        return False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        if origin_t not in evidence_origins([ev]):
            continue
        for k in _FAVE_KEYS:
            if k in ev:
                try:
                    if float(ev[k]) < thr:
                        return True
                except (TypeError, ValueError):
                    pass
    return False


# --------------------------------------------------------------------------- yield math (pure)

def compute_yield(records, pull_lines, now, ycfg: dict) -> dict:
    """Rolling-window yield per origin. Returns ``{okey: stats}`` where stats is::

        {"kind","name","contributions","pushed_contributions","pre_viral","pulls","yield"}

    contributions[X] = archived cards in-window whose evidence tags X (once per card).
    pulls[X]         = pulls-log lines for X in-window (number of pull events).
    yield[X]         = contributions/pulls, or ``None`` (UNKNOWN) when pulls == 0 -- unknown is
                       NEVER coerced to 0 (section 9 no-fabrication rule)."""
    window_days = int(ycfg["window_days"])
    thr = float(ycfg["pre_viral_faves_threshold"])
    end = now
    start = now - timedelta(days=window_days)

    agg: dict = {}

    def slot(t: tuple) -> dict:
        return agg.setdefault(t, {"kind": t[0], "name": t[1], "contributions": 0,
                                  "pushed_contributions": 0, "pre_viral": 0, "pulls": 0})

    # Numerator, DEDUPED by opportunity identity: a resurfaced card is many append-only lines with
    # ONE opportunity_id -> count it ONCE per distinct origin ("once per card", §8). Merge each
    # in-window opportunity's lines: union of origins, ``pushed`` if ANY line was pushed, ``pre_viral``
    # for an origin if ANY of that opportunity's lines qualifies.
    by_opp: dict = {}
    for rec in records:
        if not _in_window(_rec_ts(rec), start, end):
            continue
        evs = rec.get("evidence") if isinstance(rec, dict) else None
        origins = evidence_origins(evs)
        if not origins:
            continue
        g = by_opp.setdefault(_opp_id(rec),
                              {"origins": set(), "pushed": False, "pre_viral": set()})
        g["origins"] |= origins
        if isinstance(rec, dict) and rec.get("pushed"):
            g["pushed"] = True
        for t in origins:
            if _evidence_is_pre_viral(evs, t, thr):
                g["pre_viral"].add(t)
    for g in by_opp.values():
        for t in g["origins"]:
            s = slot(t)
            s["contributions"] += 1
            if g["pushed"]:
                s["pushed_contributions"] += 1
            if t in g["pre_viral"]:
                s["pre_viral"] += 1

    for line in pull_lines:
        ts = line.get("ts") if isinstance(line, dict) else None
        if not _in_window(ts, start, end):
            continue
        t = pull_origin(line)
        if t is None:
            continue
        slot(t)["pulls"] += 1

    out: dict = {}
    for t, s in agg.items():
        pulls = s["pulls"]
        s["yield"] = (s["contributions"] / pulls) if pulls > 0 else None
        out[okey(*t)] = s
    return out


def weekly_observations(origin_t: tuple, records, pull_lines, now, weeks: int) -> list:
    """Per-week ``(contributions, pulls, observed_days)`` for the trailing ``weeks`` 7-day buckets.

    Index 0 is the most recent week ``[now-7d, now)``. A week with ``pulls == 0`` is UNOBSERVED for
    this origin (unknown yield that week) -- the prune rule requires every week to be observed.

    ``observed_days`` (added after the audit) is the number of DISTINCT CALENDAR DAYS in the bucket
    on which this origin was actually pulled, 0..7. ``pulls`` alone could never carry this: one pull
    event in a 7-day bucket made the bucket read "fully observed" and drove real prune decisions off
    weeks that were in truth 4/7 and 5/7 covered. Callers judge coverage on ``observed_days``; the raw
    day count travels with every decision so a reader can see exactly how thin the evidence was."""
    obs: list = []
    for k in range(int(weeks)):
        end = now - timedelta(days=7 * k)
        start = now - timedelta(days=7 * (k + 1))
        opp_ids: set = set()
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if _in_window(_rec_ts(rec), start, end) and origin_t in evidence_origins(rec.get("evidence")):
                opp_ids.add(_opp_id(rec))   # dedup resurfaced lines within the week (§8 once per card)
        c = len(opp_ids)
        p = 0
        days: set = set()
        for line in pull_lines:
            if not isinstance(line, dict):
                continue
            ts = line.get("ts")
            if _in_window(ts, start, end) and pull_origin(line) == origin_t:
                p += 1
                try:
                    days.add(parse_ts(ts).date())
                except Exception:
                    pass
        obs.append((c, p, len(days)))
    return obs


def required_observed_days(obs: list, ycfg: dict) -> int:
    """How many distinct pulled DAYS a week must have before it may count toward a prune.

    Two bars, the STRICTER wins:

      * RELATIVE (always on): the best-covered week in the decision span. This origin has demonstrated
        it can be observed that thoroughly, so a week covered LESS well is a gap in OUR observation,
        not evidence about the handle. This is what the live regression tripped over: the roster ran
        5/7 days one week and 4/7 the next, and the thinner week was still counted as fully observed.
      * ABSOLUTE (``min_observed_days_per_week``, default 0 = off): a hard day count, e.g. 7 for
        literal calendar-week coverage. Off by default because a deployment that legitimately pulls
        twice a week would otherwise never be able to prune anything; when it is off the relative bar
        still catches DEGRADED coverage, which is the failure that actually happened.

    Returns 1 at minimum (a week with zero pulls is unobserved by definition)."""
    rel = max((d for (_c, _p, d) in obs), default=0)
    absolute = int(ycfg.get("min_observed_days_per_week") or 0)
    return max(1, rel, absolute)


def _window_kept(origin_t: tuple, pull_lines, start, end) -> int:
    """Total ``kept`` signals an origin surfaced in [start, end), read from the pulls-log.

    The pulls-log line collect_roster / collect_community_source writes carries ``kept`` = how many
    pulled items cleared the freshness + rostered-faves + topic_filter gate to become signals (§6). It
    is the ONLY replayable trace of the SINGLE-ORIGIN (community-pulse / below-sources) signals a
    handle surfaced, those never reach the >=2-origin archive the yield NUMERATOR reads, so the
    contributions metric and the window pre-viral guard are both blind to them. A line with no
    ``kept`` field (older logs) or a non-numeric value contributes 0 (best-effort, never fabricated)."""
    total = 0
    for line in pull_lines:
        if not isinstance(line, dict):
            continue
        if pull_origin(line) != origin_t:
            continue
        if not _in_window(line.get("ts"), start, end):
            continue
        k = line.get("kept")
        if isinstance(k, (int, float)) and not isinstance(k, bool):
            total += int(k)
    return total


def history_days(records, pull_lines, now) -> float:
    """Real-history span in days: now minus the earliest observed timestamp.

    The pulls-log is the denominator ledger, so real history is measured from its earliest entry;
    with no pulls-log yet we fall back to the archive's earliest card. Zero when nothing is dated."""
    earliest = None
    for line in pull_lines:
        ts = line.get("ts") if isinstance(line, dict) else None
        if not ts:
            continue
        try:
            d = parse_ts(ts)
        except Exception:
            continue
        if earliest is None or d < earliest:
            earliest = d
    if earliest is None:
        for rec in records:
            ts = _rec_ts(rec)
            if not ts:
                continue
            try:
                d = parse_ts(ts)
            except Exception:
                continue
            if earliest is None or d < earliest:
                earliest = d
    if earliest is None:
        return 0.0
    return max(0.0, (now - earliest).total_seconds() / 86400.0)


# --------------------------------------------------------------------------- decisions (pure)

def decide_prune(roster, records, pull_lines, ycfg: dict, now, yields: dict | None = None) -> list:
    """AUTO-PRUNE candidates (section 8): rostered, enabled handles whose weekly contributions stay
    at/below ``floor`` for every one of the last ``prune_after_weeks`` FULLY-OBSERVED weeks.

    A week that was not pulled (pulls == 0) is unknown, not zero -> it breaks the consecutive run and
    the handle is spared (section 9 unknown-exclusion). Returns decisions with reason + stats; it
    does NOT mutate the roster (run_yield applies them only when asked).

    ``yields`` (the ``compute_yield`` result, passed by run_yield) drives a §1/§9 PRE-VIRAL GUARD: a
    handle that CAUGHT a pre-viral signal anywhere in the rolling window (``pre_viral > 0`` -- a
    founder post it surfaced by identity BELOW the min_faves:500 keyword floor, the roster's stated
    raison d'être) is doing exactly the job the roster exists for, so it is never auto-disabled even
    if the last weeks read quiet. The pre_viral metric was already computed but previously ignored by
    the prune decision (audit HARDEN round 2); a noisy such handle is steered to a HUMAN-gated
    ``topic_filter`` suggestion (decide_suggest_filters) instead of a silent auto-prune. When
    ``yields`` is absent (a direct caller) the guard is simply inactive -- the prune stays as before.
    NOTE: a handle that only ever surfaces uncorroborated SOLO signals never reaches a >=2-origin
    archived card, so this window-level guard cannot see it; that residual is bounded by the design's
    reversible prune (enabled=false, surfaced in the review queue for un-prune, section 9).

    NOTE 2 (audit): the pre-viral guard reads engagement keys that the ARCHIVE WRITER does not
    currently persist onto evidence, so on the live archive it is structurally unable to fire.
    run_yield measures that with ``pre_viral_observability`` and reports ``pre_viral_guard.state ==
    "inert"`` plus a warning next to any prune decision taken while it was inert, so the guard can no
    longer read as protection it is not providing. The live protection for a single-origin handle is
    the pulls-log ``kept`` guard below, which reads a field the writer really does emit."""
    weeks = int(ycfg["prune_after_weeks"])
    floor = ycfg["floor"]
    out: list = []
    for e in entries_of(roster):
        if not (isinstance(e, dict) and e.get("enabled") is True):
            continue
        h = e.get("handle")
        if not isinstance(h, str) or not h.strip():
            continue
        origin_t = (KIND_HANDLE, _norm_handle_key(h))
        # §1/§9 pre-viral guard: a demonstrated pre-viral catch in the window spares the handle.
        if yields is not None:
            st = yields.get(okey(KIND_HANDLE, _norm_handle_key(h)))
            if isinstance(st, dict) and (st.get("pre_viral") or 0) > 0:
                continue
        obs = weekly_observations(origin_t, records, pull_lines, now, weeks)
        req_days = required_observed_days(obs, ycfg)
        # Every week must be REALLY OBSERVED (pulled on at least ``req_days`` distinct days, which
        # implies p >= 1) AND at/below the floor. A single under-observed week (a gap in OUR coverage,
        # not evidence about the handle) or any above-floor contribution spares the handle.
        if obs and all(p >= 1 and d >= req_days and c <= floor for (c, p, d) in obs):
            # §1/§7/§2 KEPT GUARD: `contributions` counts only >=2-origin ARCHIVED cards, but a
            # rostered handle's core job (§1) is surfacing SINGLE-ORIGIN pre-viral founder posts that
            # route to the community-pulse lane (§7) and never become a >=2-origin card, so they
            # accrue 0 contributions AND 0 window pre_viral, and the pre-viral guard above cannot see
            # them. The pulls-log `kept` count is the ONLY replayable trace of that work (Approach A:
            # no new state store): kept>0 means fresh, on-topic posts ABOVE the low rostered faves
            # floor were surfaced. Such a handle is NOT deadweight, auto-pruning it would kill exactly
            # the pre-viral coverage the roster was built for. Only a handle pulled every week that kept
            # NOTHING (all stale / off-topic / below-faves) is genuine deadweight and is pruned.
            span_start = now - timedelta(days=7 * weeks)
            if _window_kept(origin_t, pull_lines, span_start, now) > 0:
                continue
            total_c = sum(c for c, _p, _d in obs)
            total_p = sum(p for _c, p, _d in obs)
            week_days = [d for _c, _p, d in obs]
            out.append({
                "handle": h,
                "track": e.get("track"),
                "reason": (f"{weeks} consecutive weeks with contributions <= floor ({floor}); "
                           f"{total_c} contributions over {total_p} pulls; "
                           f"weekly observed days {week_days} of {FULL_WEEK_DAYS} "
                           f"(required {req_days})"),
                "weeks": weeks,
                "floor": floor,
                "weekly": obs,
                "weekly_observed_days": week_days,
                "required_observed_days": req_days,
                "full_week_days": FULL_WEEK_DAYS,
                # True only when EVERY week behind this decision was a literal 7/7 calendar week.
                # False does not invalidate the decision (the relative bar was met) but it is the
                # number a reader needs to weigh it, so it travels with the decision, never hidden.
                "full_coverage": all(d >= FULL_WEEK_DAYS for d in week_days),
                "contributions": total_c,
                "pulls": total_p,
            })
    return out


def decide_propose_add(roster, records, pull_lines, ycfg: dict, now) -> list:
    """PROPOSE-ADD queue (section 8): handles seen in evidence but NOT in the roster, ranked by how
    many distinct cards they reached, above ``propose_add_min_count``.

    NEVER mutates the roster -- addition is human-gated (section 9). Returns an ordered list
    ``[{handle, count, tracks, sample_url}]`` (most frequent first, ties broken by handle)."""
    window_days = int(ycfg["window_days"])
    min_count = int(ycfg["propose_add_min_count"])
    end = now
    start = now - timedelta(days=window_days)
    counts: dict = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if not _in_window(_rec_ts(rec), start, end):
            continue
        oid = _opp_id(rec)
        evs = rec.get("evidence") or []
        for (kind, name) in evidence_origins(evs):
            if kind != KIND_HANDLE:
                continue
            if find_entry(roster, name) is not None:
                continue  # already rostered -> not an add candidate
            slot = counts.setdefault(name, {"opps": set(), "tracks": set(), "sample_url": None})
            slot["opps"].add(oid)   # dedup: one opportunity counts ONCE even if it resurfaced (§8)
            t = rec.get("track")
            if isinstance(t, str) and t:
                slot["tracks"].add(t)
            if slot["sample_url"] is None:
                for ev in evs:
                    if isinstance(ev, dict) and _norm_handle_key(ev.get("origin_handle") or "") == name:
                        u = ev.get("url")
                        if isinstance(u, str) and u.strip():
                            slot["sample_url"] = u
                            break
    out = [{"handle": name, "count": len(v["opps"]), "tracks": sorted(v["tracks"]),
            "sample_url": v["sample_url"]}
           for name, v in counts.items() if len(v["opps"]) >= min_count]
    out.sort(key=lambda d: (-d["count"], d["handle"]))
    return out


def decide_suggest_filters(roster, yields: dict, ycfg: dict) -> list:
    """SUGGEST a topic_filter (section 8) for a high-pull / low-yield NOISY (not dead) rostered
    handle that has none. Tightening collection is add-like -> proposed, never auto-applied."""
    noisy_pull_min = int(ycfg["noisy_pull_min"])
    noisy_yield_max = float(ycfg["noisy_yield_max"])
    out: list = []
    for e in entries_of(roster):
        if not (isinstance(e, dict) and e.get("enabled") is True):
            continue
        h = e.get("handle")
        if not isinstance(h, str) or not h.strip():
            continue
        if isinstance(e.get("topic_filter"), str) and e["topic_filter"].strip():
            continue  # already filtered
        stats = yields.get(okey(KIND_HANDLE, _norm_handle_key(h)))
        if not stats:
            continue
        y = stats.get("yield")
        if y is None:
            continue  # unknown -> not a suggestion target
        if stats.get("pulls", 0) >= noisy_pull_min and stats.get("contributions", 0) >= 1 \
                and y < noisy_yield_max:
            out.append({"handle": h, "track": e.get("track"), "pulls": stats["pulls"],
                        "contributions": stats["contributions"], "yield": round(y, 4)})
    out.sort(key=lambda d: (d["yield"], d["handle"]))
    return out


def flag_drift_and_dead(roster, user_infos) -> list:
    """Monthly identity sweep (section 9 guardrail 4): ingest ``get_user_info`` results for the
    rostered handles and FLAG (never auto-remove) two failure modes a human must resolve:

      * DRIFT -- the handle was renamed (the lookup resolves to a DIFFERENT current ``userName``,
                 e.g. marc_louvion -> marclou), so the roster keeps pulling a stale handle;
      * DEAD  -- the account is gone / purged (the lookup returned nothing, or ``statusesCount`` is 0,
                 e.g. realGeorgeHotz in Appendix A).

    ``user_infos`` maps a queried roster handle -> its get_user_info dict (or None/{} when the lookup
    404'd). PURE: reads the roster + the sweep payload and mutates NOTHING -- a rename is a human edit
    and a temporarily quiet account is not a dead one (section 9). A handle NOT present in the sweep
    is simply unobserved (never fabricated into a flag). Returns an ordered list
    ``[{handle, kind, detail, current_handle?}]`` (dead before drift, then by handle) for the review
    queue; the actual add/remove stays human-gated."""
    infos = user_infos if isinstance(user_infos, dict) else {}
    by_key = {_norm_handle_key(k): v for k, v in infos.items()
              if isinstance(k, str) and k.strip()}
    out: list = []
    for e in entries_of(roster):
        if not (isinstance(e, dict) and e.get("enabled") is True):
            continue
        h = e.get("handle")
        if not isinstance(h, str) or not h.strip():
            continue
        hk = _norm_handle_key(h)
        if hk not in by_key:
            continue  # not swept this pass -> unobserved, never fabricated (section 9)
        info = by_key[hk]
        if not isinstance(info, dict) or not info:
            out.append({"handle": h, "kind": "dead",
                        "detail": "get_user_info returned nothing (account not found / suspended)"})
            continue
        current = info.get("userName") or info.get("screen_name")
        if isinstance(current, str) and current.strip() and _norm_handle_key(current) != hk:
            cur = normalize_handle(current)
            out.append({"handle": h, "kind": "drift", "current_handle": cur,
                        "detail": f"handle renamed to '{cur}'"})
            continue
        sc = info.get("statusesCount")
        if isinstance(sc, bool):
            sc = None
        if isinstance(sc, (int, float)) and sc <= 0:
            out.append({"handle": h, "kind": "dead", "detail": "statusesCount 0 (purged / inactive)"})
    out.sort(key=lambda d: (0 if d["kind"] == "dead" else 1, d["handle"]))
    return out


# --------------------------------------------------------------------------- review render (pure)

# §10 markdown-injection neutralization for the review artifact. render_review_md writes
# archive-derived, UNTRUSTED fields into markdown TABLES: propose-add ``handle`` / ``sample_url`` come
# from collected evidence (normalize_handle only strips '@'/whitespace, it does NOT validate the
# handle against _HANDLE_RE at the propose-add stage), and identity-sweep ``detail`` / ``current_handle``
# carry the untrusted get_user_info ``userName``. A raw ``|`` forges table columns and an embedded
# newline + ``##`` opens a fabricated top-level heading at column 0, the exact class the card/pulse
# renderer was hardened against with digest._inline (round 2); this sibling artifact was missed. Mirror
# that guard: collapse ALL whitespace (newlines included) to one space so nothing reaches column 0, and
# neutralize the two cell-breaking metacharacters (``|`` -> ``/``, backtick -> ``'``). Data, never markup.
_MD_CELL_NEUTRALIZE = {ord("`"): "'", ord("|"): "/"}


def _md_cell(s) -> str:
    """Flatten an untrusted value to one injection-safe markdown table cell (§10 data-not-code)."""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip().translate(_MD_CELL_NEUTRALIZE)


def render_review_md(report: dict) -> str:
    """Render ``archive/roster-review.md`` from a run_yield report (human approves; engine proposes).

    Deterministic and sorted: a propose-add table, a recently-pruned (reversible / un-prune) log,
    and any suggested topic_filters. All content is DATA about the roster, never instructions."""
    lines: list = []
    lines.append("# roster-review")
    lines.append("")
    lines.append(f"generated_at: {report.get('generated_at', '')}")
    lines.append(f"window_days: {report.get('window_days', '')}  "
                 f"history_days: {report.get('history_days', '')}  "
                 f"cold_start: {str(bool(report.get('cold_start'))).lower()}")
    written = bool(report.get("roster_written", report.get("applied")))
    lines.append(f"roster_written: {str(written).lower()}  "
                 f"prune_proposed: {report.get('prune_proposed', len(report.get('prune') or []))}  "
                 f"prune_applied: {report.get('prune_applied', 0)}  "
                 f"numerator: {_md_cell((report.get('numerator_source') or {}).get('state', 'unknown'))}")
    lines.append("")
    if report.get("cold_start"):
        lines.append("> report-only: fewer than the minimum days of real history; no pruning applied.")
        lines.append("")
    if not written:
        lines.append("> NOTHING WAS APPLIED this pass: roster.json was not written, so every handle "
                     "listed under 'proposed prunes' is STILL ENABLED. Re-run with --apply to disable "
                     "them.")
        lines.append("")
    for w in (report.get("warnings") or []):
        lines.append(f"> WARNING: {_md_cell(w)}")
    if report.get("warnings"):
        lines.append("")

    # PROPOSED vs APPLIED are two different facts and they get two different blocks. The single
    # merged "recently pruned ... enabled=false" section documented 23 handles as disabled that were
    # still enabled in roster.json, because it listed this pass's DECISIONS regardless of whether
    # --apply ever ran. Membership is now decided by the ROSTER, not by the decision list: the
    # applied section below is exactly the entries whose ``enabled`` is false right now, and anything
    # decided but not written is filed here, above, as a proposal that changed nothing.
    #
    # This block is a ``###`` on purpose. The set of ``## `` section headings in this artifact is an
    # asserted contract in the existing suite (tests/test_harden_round3.py pins the exact four, and
    # tests/test_harden_round1.py slices the file on ``"
## "``), and those tests are not this
    # change's to rewrite. Depth is cosmetic; what the reader needs is that a proposal is never
    # printed as a disable, and it is not.
    disabled_entries = report.get("disabled") or []
    disabled_keys = {e.get("handle", "").lower() for e in disabled_entries
                     if isinstance(e.get("handle"), str)}
    proposed = [d for d in (report.get("prune") or [])
                if not (isinstance(d.get("handle"), str) and d["handle"].lower() in disabled_keys)]

    lines.append("### proposed prunes (DECIDED but NOT applied; these handles are STILL ENABLED)")
    lines.append("")
    if proposed:
        lines.append("| handle | track | observed days/week | reason |")
        lines.append("|---|---|---|---|")
        for d in proposed:
            days = d.get("weekly_observed_days")
            lines.append(f"| {_md_cell(d.get('handle'))} | {_md_cell(d.get('track') or '')} | "
                         f"{_md_cell(days if days is not None else '')} | "
                         f"{_md_cell(d.get('reason', ''))} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## propose-add (human-gated; NEVER auto-added)")
    lines.append("")
    pa = report.get("propose_add") or []
    if pa:
        lines.append("| handle | count | tracks | sample |")
        lines.append("|---|---|---|---|")
        for c in pa:
            tracks = ", ".join(_md_cell(t) for t in (c.get("tracks") or []))
            lines.append(f"| {_md_cell(c['handle'])} | {c['count']} | {tracks} | "
                         f"{_md_cell(c.get('sample_url') or '')} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## recently pruned (reversible: enabled=false, un-prune here)")
    lines.append("")
    lines.append("Every handle below is disabled in roster.json RIGHT NOW. A decision that was only "
                 "proposed this pass is not here; it is in the proposed-prunes block above.")
    lines.append("")
    # Every CURRENTLY-disabled handle, so a prune applied in a PRIOR run stays discoverable for
    # un-prune (§9). Dedup by handle; deterministic (roster order). This is the durable un-prune queue,
    # and every row in it is a handle that really is disabled right now.
    pruned_rows: list = []
    shown: set = set()
    for e in disabled_entries:
        h = e.get("handle")
        if isinstance(h, str) and h.lower() in shown:
            continue
        pruned_rows.append((h, e.get("track"), e.get("reason") or "previously pruned (enabled=false)"))
        if isinstance(h, str):
            shown.add(h.lower())
    if pruned_rows:
        lines.append("| handle | track | reason |")
        lines.append("|---|---|---|")
        for h, track, reason in pruned_rows:
            lines.append(f"| {_md_cell(h)} | {_md_cell(track or '')} | {_md_cell(reason)} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## suggested topic_filters (high-pull / low-yield; propose only)")
    lines.append("")
    sf = report.get("suggest_filters") or []
    if sf:
        lines.append("| handle | track | pulls | contributions | yield |")
        lines.append("|---|---|---|---|---|")
        for d in sf:
            lines.append(f"| {_md_cell(d['handle'])} | {_md_cell(d.get('track') or '')} | "
                         f"{d['pulls']} | {d['contributions']} | {d['yield']} |")
    else:
        lines.append("_none_")
    lines.append("")

    # §9 guardrail 4: the monthly get_user_info identity sweep surfaces renamed / dead handles for a
    # human to resolve. Flagged only, NEVER auto-removed (a rename is a human edit; a quiet account
    # is not a dead one). Empty when no sweep ran this pass.
    lines.append("## flagged accounts (monthly identity sweep; human-resolved, never auto-removed)")
    lines.append("")
    fl = report.get("flags") or []
    if fl:
        lines.append("| handle | kind | detail |")
        lines.append("|---|---|---|")
        for d in fl:
            lines.append(f"| {_md_cell(d.get('handle', ''))} | {_md_cell(d.get('kind', ''))} | "
                         f"{_md_cell(d.get('detail', ''))} |")
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- orchestrator (pure)

def run_yield(roster, records, pull_lines, cfg: dict | None = None, now=None,
              apply: bool = False, user_infos: dict | None = None,
              numerator_status: dict | None = None, denominator_status: dict | None = None) -> dict:
    """Replay archive + pulls-log into a full yield report, and (optionally) APPLY auto-prune.

    ``apply=True`` flips pruned handles to ``enabled=false`` in the passed roster (in place, via
    roster.set_enabled -- reversible, never a delete). Propose-add is NEVER applied. On cold-start
    (< ``min_history_days`` of history) the prune list is empty, so ``apply`` is a safe no-op --
    honest report-only until there is real history (section 9). ``user_infos`` (an optional monthly
    ``get_user_info`` sweep, ``{handle: info}``) drives the identity-flags section (drift / dead,
    section 9 guardrail 4) -- flagged only, never auto-removed.

    ``numerator_status`` / ``denominator_status`` are the read audits from
    ``load_opportunities_audited`` / ``load_pulls_audited``. Passing the numerator audit is what makes
    the prune path FAIL CLOSED: when the denominator says pulls happened but the numerator could not
    be read (absent file, unreadable file, undecodable bytes, unparseable lines), contributions are
    UNKNOWN, and unknown must never be spent as zero. The prune list is forced empty and the reason is
    named in the report. Omitting the argument means the caller handed records in directly (tests,
    a library caller), which is recorded as ``provided`` and trusted; only the file-reading path can
    be lied to by a missing file."""
    if cfg is None:
        cfg = load_config()
    ycfg = yield_cfg(cfg)
    now = now or now_utc()

    yields = compute_yield(records, pull_lines, now, ycfg)
    hist = history_days(records, pull_lines, now)
    cold_start = hist < float(ycfg["min_history_days"])

    # ---- numerator provenance + fail-closed gate on the write path (§9 no-fabrication) ----
    num_state = (numerator_status or {}).get("state", READ_PROVIDED)
    numerator_source = {
        "state": num_state,
        "trusted": num_state in NUMERATOR_TRUSTED,
        "records_in": len(records),
        "path": (numerator_status or {}).get("path"),
        "bad_lines": (numerator_status or {}).get("bad_lines", 0),
        "decode_clean": (numerator_status or {}).get("decode_clean", True),
        "error": (numerator_status or {}).get("error"),
    }
    denominator_source = {
        "state": (denominator_status or {}).get("state", READ_PROVIDED),
        "lines_in": len(pull_lines),
        "bad_lines": (denominator_status or {}).get("bad_lines", 0),
    }
    prune_blocked = None
    if pull_lines and not numerator_source["trusted"]:
        prune_blocked = (
            f"numerator UNREADABLE (opportunities state={num_state}"
            + (f", error={numerator_source['error']}" if numerator_source["error"] else "")
            + (f", bad_lines={numerator_source['bad_lines']}" if numerator_source["bad_lines"] else "")
            + f") while {len(pull_lines)} pulls-log lines were read; contributions are UNKNOWN, "
              "not zero, so no handle may be pruned this pass"
        )

    if cold_start:
        prune = []
    elif prune_blocked:
        prune = []
    else:
        prune = decide_prune(roster, records, pull_lines, ycfg, now, yields=yields)
    propose_add = decide_propose_add(roster, records, pull_lines, ycfg, now)
    suggest = decide_suggest_filters(roster, yields, ycfg)
    flags = flag_drift_and_dead(roster, user_infos) if user_infos else []

    applied = False
    if apply and prune:  # prune is [] on cold-start; propose-add is never applied (never auto-add)
        stamp = now.date().isoformat()
        for d in prune:
            set_enabled(roster, d["handle"], False)
            # §8 "logged with reason + stats": STAMP the justification onto the DURABLE entry, not just
            # this run's report. The un-prune queue (render_review_md + the ``disabled`` list below)
            # derives a prior-run prune's reason from ``entry.notes``; without this stamp a handle
            # pruned this week shows up NEXT week as a bare "previously pruned (enabled=false)" line ,
            # the §8 auditability of the reversible-prune guardrail degrades to nothing. notes doubles
            # as the prune-reason log for a disabled entry (validate_entry keeps it a non-empty string).
            e = find_entry(roster, d["handle"])
            if e is not None:
                e["notes"] = f"auto-pruned {stamp}: {d.get('reason', '')}".strip()
        applied = True

    # DURABLE recently-pruned surface (§9 un-prune affordance): every CURRENTLY-disabled handle,
    # not just the ones decided in THIS report. decide_prune only considers enabled=True entries, so
    # a handle pruned in a PRIOR week is skipped by it and would silently vanish from the review
    # queue after --apply. Enumerating the disabled entries here keeps a pruned handle discoverable
    # for un-prune across runs (audit HARDEN). Computed AFTER apply so this run's fresh prunes are in.
    disabled = [{"handle": e.get("handle"), "track": e.get("track"),
                 "provenance": e.get("provenance"),
                 "reason": (e.get("notes") if isinstance(e.get("notes"), str) and e.get("notes").strip()
                            else None)}
                for e in entries_of(roster)
                if isinstance(e, dict) and e.get("enabled") is False
                and isinstance(e.get("handle"), str) and e.get("handle").strip()]

    # ---- §1/§9 pre-viral guard liveness. The guard is a silent `continue` inside decide_prune, so
    # nothing downstream could ever tell "spared 3 handles" from "cannot fire at all". Measure both.
    pv = pre_viral_observability(records, now, ycfg)
    pv_spared = sorted(
        e["handle"] for e in entries_of(roster)
        if isinstance(e, dict) and e.get("enabled") is True
        and isinstance(e.get("handle"), str) and e["handle"].strip()
        and ((yields.get(okey(KIND_HANDLE, _norm_handle_key(e["handle"]))) or {}).get("pre_viral") or 0) > 0
    )
    pv["spared"] = pv_spared
    if pv["state"] == "inert":
        pv["note"] = ("the archived evidence carries none of the engagement keys this guard reads, so "
                      "it CANNOT fire; pre_viral 0 means UNKNOWN here, not 'no pre-viral catch'. The "
                      "live protection for a single-origin handle is the pulls-log kept guard.")

    # Stamp the guard's liveness onto EVERY decision, not just the report header. A row in the review
    # table is what a human actually reads before un-pruning, and "this handle was pruned while the
    # guard that exists to spare it could not fire" is a property of that row.
    for d in prune:
        d["pre_viral_guard_state"] = pv["state"]

    warnings: list = []
    if prune_blocked:
        warnings.append(prune_blocked)
    if prune and pv["state"] == "inert":
        warnings.append(f"pre-viral prune guard is INERT (0 of {pv['evidence_items']} in-window "
                        f"origin-tagged evidence items carry an engagement key); "
                        f"{len(prune)} prune decision(s) were made without it")
    thin = [d for d in prune if not d.get("full_coverage")]
    if thin:
        warnings.append(
            "prune decisions resting on weeks that were NOT fully observed (of "
            f"{FULL_WEEK_DAYS} days): " +
            "; ".join(f"{d['handle']} {d.get('weekly_observed_days')}" for d in thin))

    return {
        "generated_at": iso(now),
        "window_days": int(ycfg["window_days"]),
        "prune_after_weeks": int(ycfg["prune_after_weeks"]),
        "floor": ycfg["floor"],
        "history_days": round(hist, 3),
        "min_history_days": ycfg["min_history_days"],
        "min_observed_days_per_week": ycfg["min_observed_days_per_week"],
        "full_week_days": FULL_WEEK_DAYS,
        "cold_start": cold_start,
        # LEGACY FIELD, kept for its existing consumers: this is the COLD-START gate, NOT a statement
        # about whether the roster was written. Read ``roster_written`` for that. It used to be the
        # only signal, which is how a run that merely PROPOSED 23 prunes got reported as if it had
        # applied them.
        "report_only": cold_start,
        "report_only_reason": ("cold_start" if cold_start
                               else ("numerator_untrusted" if prune_blocked
                                     else (None if applied else "apply_not_requested"))),
        # The truthful pair: what was proposed vs what was actually written to roster.json.
        "roster_written": applied,
        "prune_proposed": len(prune),
        "prune_applied": len(prune) if applied else 0,
        "numerator_source": numerator_source,
        "denominator_source": denominator_source,
        "prune_blocked_reason": prune_blocked,
        "pre_viral_guard": pv,
        "warnings": warnings,
        "yields": yields,
        "prune": prune,
        "disabled": disabled,
        "propose_add": propose_add,
        "suggest_filters": suggest,
        "flags": flags,
        "applied": applied,
    }


# --------------------------------------------------------------------------- I/O (edges)

# Read outcomes for a JSONL ledger. "clean" and "did not check anything" MUST be different values.
READ_ABSENT = "absent"          # the file is not there at all
READ_UNREADABLE = "unreadable"  # the file exists but open/read raised -> nothing was read
READ_CORRUPT = "corrupt"        # bytes did not decode as UTF-8, or a line was not JSON -> PARTIAL
READ_OK = "ok"                  # every byte decoded and every non-blank line parsed
READ_PROVIDED = "provided"      # records handed straight to the engine (a caller/test), no file read

# The only states in which the NUMERATOR may be treated as a measurement. Anything else means we do
# not know how many contributions there were, and "we do not know" must never read as "zero".
NUMERATOR_TRUSTED = (READ_OK, READ_PROVIDED)


def read_jsonl_audited(p: Path) -> tuple:
    """``(records, status)`` for one JSONL ledger. The status is the whole point of this function.

    The pre-audit reader returned a bare ``[]`` for an absent file, for an unreadable file, and for a
    genuinely empty one, swallowing every exception on the way. Downstream, ``decide_prune`` gates on
    ``contributions <= floor`` with ``floor == 0``, so a numerator of "we could not read the file"
    is arithmetically identical to "this handle produced nothing" and prunes the roster on the
    strength of a missing file. Reproduced: the real pulls logs plus the real roster, with
    ``opportunities.jsonl`` simply absent, produced a byte-identical 23-handle prune list to the live
    run that had 112 contributions. The numerator has to be able to say "I do not know".

    ``status`` is::

        {"path", "state", "bytes", "lines", "records", "blank_lines", "bad_lines",
         "decode_clean" (bool), "error" (str|None)}

    ``state`` is one of READ_ABSENT / READ_UNREADABLE / READ_CORRUPT / READ_OK. Recovery behavior is
    UNCHANGED from before (tolerant decode, per-line skip) so one bad byte still never costs the whole
    month, but the fact that a byte or a line was lost is now REPORTED instead of silently absorbed."""
    status = {"path": str(p), "state": READ_ABSENT, "bytes": 0, "lines": 0, "records": 0,
              "blank_lines": 0, "bad_lines": 0, "decode_clean": True, "error": None}
    out: list = []
    if not p.is_file():
        return out, status
    try:
        raw = p.read_bytes()
    except Exception as e:
        status["state"] = READ_UNREADABLE
        status["error"] = f"{type(e).__name__}: {e}"
        return out, status
    status["bytes"] = len(raw)
    try:
        # Strict first, purely to LEARN whether the bytes are clean. The answer is the difference
        # between "this month contributed 0" and "this month is unreadable".
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        status["decode_clean"] = False
        status["error"] = f"UnicodeDecodeError: {e}"
        # errors="replace": ONE encoding-corrupt byte (a partial write on crash, a lone surrogate an
        # origin field carried) must NOT nuke the WHOLE file, so we still recover every intact record
        # around it. What changed is that the loss is now on the record.
        text = raw.decode("utf-8-sig", errors="replace")
    for line in text.splitlines():
        status["lines"] += 1
        line = line.strip()
        if not line:
            status["blank_lines"] += 1
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            status["bad_lines"] += 1
    status["records"] = len(out)
    status["state"] = READ_OK if (status["decode_clean"] and status["bad_lines"] == 0) else READ_CORRUPT
    return out, status


def _read_jsonl(p: Path) -> list:
    """Records only. Kept for callers that genuinely do not need the audit; the yield pass does."""
    return read_jsonl_audited(p)[0]


def load_opportunities_audited(archive_dir: str | None = None) -> tuple:
    """``(records, status)`` for the NUMERATOR ledger. Use this on any path that can prune."""
    return read_jsonl_audited(resolve_archive_dir(archive_dir) / "opportunities.jsonl")


def load_opportunities(archive_dir: str | None = None) -> list:
    """Read all archived card records from ``archive/opportunities.jsonl`` (never raises on absence).

    Records only, so an absent file is indistinguishable from an empty one. Any caller that can act
    on the result (prune) MUST use ``load_opportunities_audited`` instead."""
    return load_opportunities_audited(archive_dir)[0]


def load_pulls_audited(archive_dir: str | None = None) -> tuple:
    """``(lines, status)`` for the DENOMINATOR ledger, merged across every monthly file.

    The merged status is the WORST per-file state (corrupt beats ok, unreadable beats corrupt) plus
    the per-file statuses, so a single bad month is visible instead of averaged away."""
    base = resolve_archive_dir(archive_dir)
    lines: list = []
    files: list = []
    if not base.is_dir():
        return lines, {"state": READ_ABSENT, "dir": str(base), "files": files,
                       "records": 0, "bad_lines": 0, "decode_clean": True}
    for p in sorted(base.glob("pulls-*.jsonl")):
        rows, st = read_jsonl_audited(p)
        lines.extend(rows)
        files.append(st)
    if not files:
        state = READ_ABSENT
    elif any(f["state"] == READ_UNREADABLE for f in files):
        state = READ_UNREADABLE
    elif any(f["state"] == READ_CORRUPT for f in files):
        state = READ_CORRUPT
    else:
        state = READ_OK
    return lines, {"state": state, "dir": str(base), "files": files, "records": len(lines),
                   "bad_lines": sum(f["bad_lines"] for f in files),
                   "decode_clean": all(f["decode_clean"] for f in files)}


def load_pulls(archive_dir: str | None = None) -> list:
    """Read the denominator: every ``archive/pulls-*.jsonl`` line across months, in file order."""
    return load_pulls_audited(archive_dir)[0]


def write_review(md: str, archive_dir: str | None = None) -> Path:
    """Write the propose-add / pruned review queue to ``archive/roster-review.md`` (utf-8, LF)."""
    base = resolve_archive_dir(archive_dir)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "roster-review.md"
    p.write_text(md, encoding="utf-8", newline="\n")
    return p


def yield_week_key(now=None) -> str:
    """The ISO-8601 week label (``YYYY-Www``) that keys the weekly yield ledger item.

    Uses the ISO calendar so the week rolls on Monday and is stable across a year boundary. Reads the
    clock via lib.now_utc (honors DAILY_HOTSPOTS_NOW), so tests stay deterministic."""
    now = now or now_utc()
    y, w, _ = now.isocalendar()
    return f"{y:04d}-W{w:02d}"


def register_yield_item(ledger, week: str | None = None, summary: str = "", now=None) -> dict:
    """Idempotent weekly schedule-reminder item ``daily-hotspots:yield:<ISO-week>`` (spec §8/§4).

    Mirrors digest.register_digest_item's ``daily-hotspots:digest:<date>`` on the WEEKLY cadence: a
    base-ledger UPSERT keyed by ISO week, so the self-evolve pass leaves the same durable, dedup-safe
    trace the daily digest does. Re-running the pass in the same ISO week re-UPSERTs the SAME id (no
    duplicate item); the reversible auto-prune (set_enabled(false), a no-op when already disabled) is
    what actually makes a re-run harmless. Best-effort at the call site, a missing schedule-reminder
    base must never fail the deterministic replay."""
    week = week or yield_week_key(now)
    key = f"daily-hotspots:yield:{week}"
    ext = {"x_daily_hotspots_yield_week": week, "x_daily_hotspots_yield_summary": summary[:200]}
    args = ["--title", f"daily-hotspots yield {week}", "--kind", "task",
            "--source", "daily-hotspots", "--idempotency-key", key,
            "--ext", json.dumps(ext, ensure_ascii=False)]
    return ledger._run("add", args)


# --------------------------------------------------------------------------- CLI (edge)

def main(argv: list | None = None) -> int:
    """Weekly yield pass. Default is REPORT-ONLY (prints the JSON report, writes nothing).

    Flags: ``--apply`` also disables pruned handles in roster.json and saves it; ``--write-review``
    also writes archive/roster-review.md; ``--archive-dir`` / ``--roster`` override the config-dir
    probe (used by tests / dry runs so the live companion is never touched implicitly)."""
    import argparse
    ap = argparse.ArgumentParser(description="daily-hotspots signal-yield engine (spec 8/9)")
    ap.add_argument("--archive-dir", default=None)
    ap.add_argument("--roster", default=None)
    ap.add_argument("--apply", action="store_true", help="apply auto-prune to roster.json (reversible)")
    ap.add_argument("--write-review", action="store_true", help="write archive/roster-review.md")
    ap.add_argument("--user-info", default=None,
                    help="path to a get_user_info sweep JSON {handle: info} -> identity flags (§9)")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config()
    roster = load_roster(path=args.roster)
    # AUDITED reads: the CLI is the path that can write roster.json, so it is the path that must be
    # able to say "I could not read the numerator" instead of quietly treating it as zero.
    records, num_status = load_opportunities_audited(args.archive_dir)
    pulls, den_status = load_pulls_audited(args.archive_dir)
    if pulls and num_status["state"] not in NUMERATOR_TRUSTED:
        print(f"[daily-hotspots] WARNING: numerator ledger {num_status.get('path')} is "
              f"{num_status['state']} ({num_status.get('error') or 'no error detail'}; "
              f"bad_lines={num_status.get('bad_lines')}) while {len(pulls)} pulls-log lines were "
              f"read. Contributions are UNKNOWN this pass, so AUTO-PRUNE IS DISABLED.",
              file=sys.stderr)

    user_infos = None
    if args.user_info:
        try:
            loaded = json.loads(Path(args.user_info).read_text(encoding="utf-8-sig"))
            user_infos = loaded if isinstance(loaded, dict) else None
        except Exception:
            user_infos = None

    report = run_yield(roster, records, pulls, cfg=cfg, apply=args.apply, user_infos=user_infos,
                       numerator_status=num_status, denominator_status=den_status)

    if args.apply and report.get("applied"):
        save_roster(roster, path=args.roster)
    if args.write_review:
        write_review(render_review_md(report), args.archive_dir)

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
