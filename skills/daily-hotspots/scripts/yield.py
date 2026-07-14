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
}


def yield_cfg(cfg: dict | None) -> dict:
    """Resolve the effective yield thresholds: module defaults overlaid by the config ``yield`` block.

    Reads only; never mutates ``cfg``. An absent or malformed block degrades to the module defaults
    so the engine always has a defined floor."""
    y = dict(DEFAULT_YIELD_CONFIG)
    if isinstance(cfg, dict):
        blk = cfg.get("yield")
        if isinstance(blk, dict):
            for k, v in blk.items():
                y[k] = v
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


def _evidence_is_pre_viral(evidence, origin_t: tuple, thr: float) -> bool:
    """True if any evidence item tagged ``origin_t`` carries an engagement count below ``thr``.

    The pre-viral-catch metric (section 8): a rostered pull surfaces a founder's post by identity
    before it clears the keyword-search faves floor -- a signal keyword search would have dropped.
    Best-effort: if no engagement field is present the item simply does not count."""
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

    for rec in records:
        ts = _rec_ts(rec)
        if not _in_window(ts, start, end):
            continue
        evs = rec.get("evidence") if isinstance(rec, dict) else None
        origins = evidence_origins(evs)
        pushed = bool(rec.get("pushed")) if isinstance(rec, dict) else False
        for t in origins:
            s = slot(t)
            s["contributions"] += 1
            if pushed:
                s["pushed_contributions"] += 1
            if _evidence_is_pre_viral(evs, t, thr):
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
    """Per-week ``(contributions, pulls)`` for the trailing ``weeks`` 7-day buckets.

    Index 0 is the most recent week ``[now-7d, now)``. A week with ``pulls == 0`` is UNOBSERVED for
    this origin (unknown yield that week) -- the prune rule requires every week to be observed."""
    obs: list = []
    for k in range(int(weeks)):
        end = now - timedelta(days=7 * k)
        start = now - timedelta(days=7 * (k + 1))
        c = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if _in_window(_rec_ts(rec), start, end) and origin_t in evidence_origins(rec.get("evidence")):
                c += 1
        p = 0
        for line in pull_lines:
            if not isinstance(line, dict):
                continue
            if _in_window(line.get("ts"), start, end) and pull_origin(line) == origin_t:
                p += 1
        obs.append((c, p))
    return obs


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

def decide_prune(roster, records, pull_lines, ycfg: dict, now) -> list:
    """AUTO-PRUNE candidates (section 8): rostered, enabled handles whose weekly contributions stay
    at/below ``floor`` for every one of the last ``prune_after_weeks`` FULLY-OBSERVED weeks.

    A week that was not pulled (pulls == 0) is unknown, not zero -> it breaks the consecutive run and
    the handle is spared (section 9 unknown-exclusion). Returns decisions with reason + stats; it
    does NOT mutate the roster (run_yield applies them only when asked)."""
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
        obs = weekly_observations(origin_t, records, pull_lines, now, weeks)
        # Every week must be OBSERVED (p >= 1) AND at/below the floor. A single unobserved week
        # (unknown yield) or any above-floor contribution spares the handle.
        if obs and all(p >= 1 and c <= floor for (c, p) in obs):
            total_c = sum(c for c, _ in obs)
            total_p = sum(p for _, p in obs)
            out.append({
                "handle": h,
                "track": e.get("track"),
                "reason": (f"{weeks} consecutive weeks with contributions <= floor ({floor}); "
                           f"{total_c} contributions over {total_p} pulls"),
                "weeks": weeks,
                "floor": floor,
                "weekly": obs,
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
        evs = rec.get("evidence") or []
        for (kind, name) in evidence_origins(evs):
            if kind != KIND_HANDLE:
                continue
            if find_entry(roster, name) is not None:
                continue  # already rostered -> not an add candidate
            slot = counts.setdefault(name, {"count": 0, "tracks": set(), "sample_url": None})
            slot["count"] += 1
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
    out = [{"handle": name, "count": v["count"], "tracks": sorted(v["tracks"]),
            "sample_url": v["sample_url"]}
           for name, v in counts.items() if v["count"] >= min_count]
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


# --------------------------------------------------------------------------- review render (pure)

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
    lines.append("")
    if report.get("cold_start"):
        lines.append("> report-only: fewer than the minimum days of real history; no pruning applied.")
        lines.append("")

    lines.append("## propose-add (human-gated; NEVER auto-added)")
    lines.append("")
    pa = report.get("propose_add") or []
    if pa:
        lines.append("| handle | count | tracks | sample |")
        lines.append("|---|---|---|---|")
        for c in pa:
            tracks = ", ".join(c.get("tracks") or [])
            lines.append(f"| {c['handle']} | {c['count']} | {tracks} | {c.get('sample_url') or ''} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("## recently pruned (reversible: enabled=false, un-prune here)")
    lines.append("")
    pr = report.get("prune") or []
    if pr:
        lines.append("| handle | track | reason |")
        lines.append("|---|---|---|")
        for d in pr:
            lines.append(f"| {d['handle']} | {d.get('track') or ''} | {d.get('reason', '')} |")
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
            lines.append(f"| {d['handle']} | {d.get('track') or ''} | {d['pulls']} | "
                         f"{d['contributions']} | {d['yield']} |")
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- orchestrator (pure)

def run_yield(roster, records, pull_lines, cfg: dict | None = None, now=None,
              apply: bool = False) -> dict:
    """Replay archive + pulls-log into a full yield report, and (optionally) APPLY auto-prune.

    ``apply=True`` flips pruned handles to ``enabled=false`` in the passed roster (in place, via
    roster.set_enabled -- reversible, never a delete). Propose-add is NEVER applied. On cold-start
    (< ``min_history_days`` of history) the prune list is empty, so ``apply`` is a safe no-op --
    honest report-only until there is real history (section 9)."""
    if cfg is None:
        cfg = load_config()
    ycfg = yield_cfg(cfg)
    now = now or now_utc()

    yields = compute_yield(records, pull_lines, now, ycfg)
    hist = history_days(records, pull_lines, now)
    cold_start = hist < float(ycfg["min_history_days"])

    prune = [] if cold_start else decide_prune(roster, records, pull_lines, ycfg, now)
    propose_add = decide_propose_add(roster, records, pull_lines, ycfg, now)
    suggest = decide_suggest_filters(roster, yields, ycfg)

    applied = False
    if apply and prune:  # prune is [] on cold-start; propose-add is never applied (never auto-add)
        for d in prune:
            set_enabled(roster, d["handle"], False)
        applied = True

    return {
        "generated_at": iso(now),
        "window_days": int(ycfg["window_days"]),
        "prune_after_weeks": int(ycfg["prune_after_weeks"]),
        "floor": ycfg["floor"],
        "history_days": round(hist, 3),
        "min_history_days": ycfg["min_history_days"],
        "cold_start": cold_start,
        "report_only": cold_start,
        "yields": yields,
        "prune": prune,
        "propose_add": propose_add,
        "suggest_filters": suggest,
        "applied": applied,
    }


# --------------------------------------------------------------------------- I/O (edges)

def _read_jsonl(p: Path) -> list:
    out: list = []
    if p.is_file():
        try:
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
    return out


def load_opportunities(archive_dir: str | None = None) -> list:
    """Read all archived card records from ``archive/opportunities.jsonl`` (never raises on absence)."""
    return _read_jsonl(resolve_archive_dir(archive_dir) / "opportunities.jsonl")


def load_pulls(archive_dir: str | None = None) -> list:
    """Read the denominator: every ``archive/pulls-*.jsonl`` line across months, in file order."""
    base = resolve_archive_dir(archive_dir)
    lines: list = []
    if base.is_dir():
        for p in sorted(base.glob("pulls-*.jsonl")):
            lines.extend(_read_jsonl(p))
    return lines


def write_review(md: str, archive_dir: str | None = None) -> Path:
    """Write the propose-add / pruned review queue to ``archive/roster-review.md`` (utf-8, LF)."""
    base = resolve_archive_dir(archive_dir)
    base.mkdir(parents=True, exist_ok=True)
    p = base / "roster-review.md"
    p.write_text(md, encoding="utf-8", newline="\n")
    return p


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
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config()
    roster = load_roster(path=args.roster)
    records = load_opportunities(args.archive_dir)
    pulls = load_pulls(args.archive_dir)

    report = run_yield(roster, records, pulls, cfg=cfg, apply=args.apply)

    if args.apply and report.get("applied"):
        save_roster(roster, path=args.roster)
    if args.write_review:
        write_review(render_review_md(report), args.archive_dir)

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
