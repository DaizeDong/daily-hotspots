#!/usr/bin/env python3
"""roster.py — the X (Twitter) KOL roster: load / validate / mutate + account-pull PLANNER.

Per the source-coverage design (§5.1 schema, §6 pull recipe, §8 yield-engine mutations). The
roster (``roster.json`` in the daily-hotspots-config companion) is the ONE genuinely-new data
asset the whole design turns on: a curated list of founder/KOL handles the collect loop pulls
every run, so a founder's post surfaces by *identity* (pre-viral) instead of only by keyword luck
after it clears 500 faves.

Design contract — each roster entry (§5.1):

    {handle, track, tier(1|2), enabled, topic_filter?(str), added_at, provenance(seed|approved),
     notes?}

This module keeps the parts that matter PURE (clock/network-free) so the acceptance-gate suite can
byte-compare:
  * ``validate_entry`` / ``validate_roster`` — schema validation (also reused by verify_config).
  * ``select_handles`` / ``plan_pulls``       — the account-pull planner (which handles to pull).
  * ``set_enabled`` / ``upsert_entry``         — the yield engine's auto-prune (reversible) and
                                                 propose-add (human-approved) mutations.
I/O (``load_roster`` / ``save_roster``) is isolated at the edges and never raises on absence — a
missing companion degrades to an empty roster, mirroring lib.load_config's contract.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from lib import find_config_dir, load_config, now_utc, iso, parse_ts

# --------------------------------------------------------------------------- schema constants

ROSTER_SCHEMA_VERSION = 1

# Twitter handles: 1-15 chars, ASCII letters/digits/underscore, no leading '@'.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

VALID_TIERS = (1, 2)
VALID_PROVENANCE = ("seed", "approved")

# Required keys on every entry, and the optional ones we still type-check when present.
_REQUIRED_KEYS = ("handle", "track", "tier", "enabled", "added_at", "provenance")
_OPTIONAL_STR_KEYS = ("topic_filter", "notes")

# Rostered handles pull with a LOW faves floor (§6: catch pre-viral). The knob lives in
# watchlist.json sources.twitterapi.min_faves_rostered; absent -> 0 (roster identity IS the trust
# signal, so no engagement floor is imposed on a trusted handle).
DEFAULT_MIN_FAVES_ROSTERED = 0

# The open keyword-search faves floor (spec §1/§6: the template's ``min_faves:500``). A rostered pull
# exists to catch a founder's post BELOW this floor (pre-viral); a min_faves_rostered set at or above
# it adds zero pre-viral value and, left UNBOUNDED, routes around the §9 anti-mass-prune clamp — so it
# is the guardrail CAP for min_faves_rostered (see _min_faves_rostered). A hardcoded rail (like
# lib._clamp_guardrails' floors) that another unclamped knob can never lift.
KEYWORD_FAVES_FLOOR = 500

# §6 recipe pins get_user_last_tweets(..., includeReplies=false); surface it so the emitted plan is
# fully self-describing for the collect loop.
PULL_INCLUDE_REPLIES = False


# --------------------------------------------------------------------------- normalization

def normalize_handle(handle: str) -> str:
    """Canonical handle form: strip surrounding whitespace and a single leading '@'.

    Case is PRESERVED (twitterapi is case-insensitive on lookup but we keep the roster's display
    casing, e.g. DrJimFan). Uniqueness is compared case-insensitively — see validate_roster."""
    h = (handle or "").strip()
    if h.startswith("@"):
        h = h[1:]
    return h.strip()


def entries_of(roster) -> list:
    """Return the entry list regardless of the top-level shape.

    We accept both the canonical object form ``{"schema_version", "entries": [...]}`` and a bare
    JSON array of entries (liberal in what we accept). Non-conforming input yields an empty list;
    validate_roster is what turns a malformed shape into a loud error."""
    if isinstance(roster, list):
        return roster
    if isinstance(roster, dict):
        ents = roster.get("entries")
        return ents if isinstance(ents, list) else []
    return []


def normalize_roster(roster) -> dict:
    """Coerce any accepted input shape into the canonical object form (does not deep-copy entries).

    A bare list is wrapped; a dict is returned with an ensured ``entries`` list and schema_version.
    Used by load_roster and before save so the on-disk form is always the object shape."""
    if isinstance(roster, list):
        return {"schema_version": ROSTER_SCHEMA_VERSION, "entries": list(roster)}
    if isinstance(roster, dict):
        out = dict(roster)
        out.setdefault("schema_version", ROSTER_SCHEMA_VERSION)
        ents = out.get("entries")
        out["entries"] = ents if isinstance(ents, list) else []
        return out
    return {"schema_version": ROSTER_SCHEMA_VERSION, "entries": []}


# --------------------------------------------------------------------------- validation

def validate_entry(entry, idx: int | None = None) -> list:
    """Return a list of human-readable error strings for ONE entry (empty list == valid).

    Enforces the §5.1 schema exactly: required keys present + well-typed; tier in {1,2};
    provenance in {seed,approved}; handle a well-formed twitter handle; added_at a parseable
    timestamp; optional topic_filter/notes, when present, non-empty / well-typed strings. Extra
    keys are tolerated (forward-compatible — e.g. later origin tags)."""
    where = f"entry[{idx}]" if idx is not None else "entry"
    errs: list = []
    if not isinstance(entry, dict):
        return [f"{where} must be an object, got {type(entry).__name__}"]

    for k in _REQUIRED_KEYS:
        if k not in entry:
            errs.append(f"{where} missing required key '{k}'")

    # handle
    raw_handle = entry.get("handle")
    if "handle" in entry:
        if not isinstance(raw_handle, str) or not raw_handle.strip():
            errs.append(f"{where} handle must be a non-empty string")
        else:
            norm = normalize_handle(raw_handle)
            if not _HANDLE_RE.match(norm):
                errs.append(f"{where} handle '{raw_handle}' is not a valid twitter handle "
                            f"(1-15 chars, [A-Za-z0-9_], no leading '@')")

    # track
    track = entry.get("track")
    if "track" in entry and (not isinstance(track, str) or not track.strip()):
        errs.append(f"{where} track must be a non-empty string")

    # tier (bool is a subclass of int in Python — reject it explicitly)
    tier = entry.get("tier")
    if "tier" in entry:
        if isinstance(tier, bool) or not isinstance(tier, int) or tier not in VALID_TIERS:
            errs.append(f"{where} tier must be one of {VALID_TIERS}, got {tier!r}")

    # enabled
    enabled = entry.get("enabled")
    if "enabled" in entry and not isinstance(enabled, bool):
        errs.append(f"{where} enabled must be a boolean, got {type(enabled).__name__}")

    # provenance
    prov = entry.get("provenance")
    if "provenance" in entry and prov not in VALID_PROVENANCE:
        errs.append(f"{where} provenance must be one of {VALID_PROVENANCE}, got {prov!r}")

    # added_at (must parse as a timestamp)
    added = entry.get("added_at")
    if "added_at" in entry:
        if not isinstance(added, str) or not added.strip():
            errs.append(f"{where} added_at must be a non-empty ISO timestamp string")
        else:
            try:
                parse_ts(added)
            except Exception:
                errs.append(f"{where} added_at '{added}' is not a parseable timestamp")

    # optional strings: if present, must be non-empty strings
    for k in _OPTIONAL_STR_KEYS:
        if k in entry and entry[k] is not None:
            v = entry[k]
            if not isinstance(v, str) or not v.strip():
                errs.append(f"{where} {k}, when present, must be a non-empty string")

    return errs


def validate_roster(roster) -> tuple:
    """Validate the whole roster. Returns ``(ok: bool, errors: list[str])``.

    Checks the top-level shape, every entry (§5.1), the schema_version type, and — a roster-level
    invariant not expressible per-entry — that handles are UNIQUE (case-insensitive). A duplicate
    handle would make auto-prune/propose-add ambiguous, so it is a hard error."""
    errs: list = []
    if not isinstance(roster, (list, dict)):
        return (False, [f"roster must be a JSON object or array, got {type(roster).__name__}"])

    if isinstance(roster, dict):
        sv = roster.get("schema_version", ROSTER_SCHEMA_VERSION)
        if isinstance(sv, bool) or not isinstance(sv, int):
            errs.append(f"schema_version must be an integer, got {sv!r}")

    entries = entries_of(roster)
    seen: dict = {}
    for i, e in enumerate(entries):
        errs.extend(validate_entry(e, idx=i))
        if isinstance(e, dict) and isinstance(e.get("handle"), str) and e["handle"].strip():
            key = normalize_handle(e["handle"]).lower()
            if key in seen:
                errs.append(f"duplicate handle '{e['handle']}' (entries {seen[key]} and {i})")
            else:
                seen[key] = i

    return (len(errs) == 0, errs)


# --------------------------------------------------------------------------- planner (pure)

def _min_faves_rostered_cap(cfg: dict | None) -> int:
    """Upper bound for min_faves_rostered: never above ``KEYWORD_FAVES_FLOOR`` (the keyword search's
    own faves floor the rostered pull exists to UNDERCUT, §6). A user ``yield.pre_viral_faves_threshold``
    that is LOWER tightens the cap; a higher one can NEVER raise it — so the cap can't be routed around
    by fat-fingering that other (unclamped) knob too. Guardrails only tighten (信条)."""
    cap = KEYWORD_FAVES_FLOOR
    try:
        pv = cfg["yield"]["pre_viral_faves_threshold"]  # type: ignore[index]
        if not isinstance(pv, bool) and isinstance(pv, (int, float)) and 0 <= pv < cap:
            cap = int(pv)
    except Exception:
        pass
    return cap


def _min_faves_rostered(cfg: dict | None) -> int:
    """The LOW faves floor a rostered pull applies (§6: catch PRE-VIRAL posts a min_faves:500 keyword
    search never sees). Read from ``sources.twitterapi.min_faves_rostered``; absent/garbled -> 0.

    CLAMPED into ``[0, cap]`` (cap = _min_faves_rostered_cap). This is the collection-side twin of the
    §9 anti-mass-prune rails lib._clamp_guardrails / yield._clamp_yield_guardrails enforce: an UNBOUNDED
    floor here routes AROUND them. Set it to 1e6 and every rostered pull keeps 0 tweets every run
    (numerator 0) while run.py still appends a pulls-log line per handle (denominator accrues); after
    ``prune_after_weeks`` fully-observed weeks decide_prune reads the ENTIRE roster as dead and
    ``--apply`` disables all of it — and the §1/§9 pre-viral guard is blind too (0 kept -> pre_viral 0).
    Capping at the keyword floor keeps the knob doing its documented job while a productive handle whose
    posts clear that floor still survives; a negative floor is nonsense -> 0; a non-numeric value ->
    default (no engagement floor on a trusted handle). Never raises."""
    try:
        raw = cfg["sources"]["twitterapi"]["min_faves_rostered"]  # type: ignore[index]
    except Exception:
        return DEFAULT_MIN_FAVES_ROSTERED
    if isinstance(raw, bool):
        return DEFAULT_MIN_FAVES_ROSTERED
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_FAVES_ROSTERED
    if v < 0:
        return 0
    cap = _min_faves_rostered_cap(cfg)
    return cap if v > cap else v


def _max_handles_per_run(cfg: dict | None) -> int | None:
    """Optional per-run pull CAP (cost / rate guardrail, §6): the max number of handles plan_pulls
    emits in one run. Read from ``sources.twitterapi.max_handles_per_run``; absent / garbled /
    non-positive -> None (NO cap, byte-identical to the pre-cap default).

    The roster is seeded at ~15-30 handles but grows via §8 propose-add over months; every add is
    human-gated, but nothing in the deterministic planner bounded the daily twitterapi fan-out — a
    roster grown to a few hundred handles meant a few hundred get_user_last_tweets calls EVERY day
    (rate-limit / cost blowup). A positive int N keeps the first N handles in roster order
    (deterministic — seeds first), giving the operator a hard ceiling without changing the default."""
    try:
        raw = cfg["sources"]["twitterapi"]["max_handles_per_run"]  # type: ignore[index]
    except Exception:
        return None
    if isinstance(raw, bool):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def select_handles(roster, tier: int = 1, enabled_only: bool = True) -> list:
    """Pure selector: return the entry dicts to act on for the given tier, in roster order.

    §6 pulls the roster's ``enabled tier-1`` handles. Order is preserved (stable) for
    determinism. Malformed entries (missing tier/enabled/handle) are skipped, never crash the
    planner — validation is a separate gate the caller runs first."""
    out: list = []
    for e in entries_of(roster):
        if not isinstance(e, dict):
            continue
        if e.get("tier") != tier:
            continue
        if enabled_only and e.get("enabled") is not True:
            continue
        if not isinstance(e.get("handle"), str) or not e["handle"].strip():
            continue
        out.append(e)
    return out


def plan_pulls(roster, cfg: dict | None = None, tier: int = 1) -> list:
    """The account-pull PLANNER (§6): which handles to pull this run, honoring topic_filter.

    Returns an ordered list of self-describing pull tasks, one per selected handle::

        {"handle", "track", "tier", "topic_filter"(str|None), "min_faves"(int),
         "include_replies"(bool)}

    The collect loop feeds each task to twitterapi ``get_user_last_tweets(userName=handle,
    includeReplies=include_replies)`` and, when ``topic_filter`` is set, keeps only tweets matching
    that query (honoring the filter). ``min_faves`` comes from config's ``min_faves_rostered`` (low,
    to catch pre-viral). Pure: no clock, no network — ``cfg`` is read but never mutated."""
    if cfg is None:
        cfg = load_config()
    min_faves = _min_faves_rostered(cfg)
    plan: list = []
    for e in select_handles(roster, tier=tier, enabled_only=True):
        tf = e.get("topic_filter")
        tf = tf if (isinstance(tf, str) and tf.strip()) else None
        plan.append({
            "handle": normalize_handle(e["handle"]),
            "track": e.get("track"),
            "tier": e.get("tier"),
            "topic_filter": tf,
            "min_faves": min_faves,
            "include_replies": PULL_INCLUDE_REPLIES,
        })
    cap = _max_handles_per_run(cfg)
    if cap is not None:
        plan = plan[:cap]          # bound the daily fan-out (§6 cost/rate guardrail); seeds pulled first
    return plan


# --------------------------------------------------------------------------- mutation

def find_entry(roster, handle: str) -> dict | None:
    """Return the entry for ``handle`` (case-insensitive) or None. Operates on live entries."""
    key = normalize_handle(handle).lower()
    for e in entries_of(roster):
        if isinstance(e, dict) and isinstance(e.get("handle"), str) \
                and normalize_handle(e["handle"]).lower() == key:
            return e
    return None


def set_enabled(roster, handle: str, enabled: bool) -> dict | None:
    """AUTO-PRUNE primitive (§8, reversible): flip an existing handle's ``enabled`` flag in place.

    Returns the mutated entry, or None if the handle is not in the roster. This is NEVER a delete —
    a pruned handle stays as ``enabled=false`` so a human can un-prune it from the review queue."""
    e = find_entry(roster, handle)
    if e is None:
        return None
    e["enabled"] = bool(enabled)
    return e


def new_entry(handle: str, track: str, tier: int = 1, enabled: bool = True,
              topic_filter: str | None = None, provenance: str = "seed",
              notes: str | None = None, added_at: str | None = None) -> dict:
    """Construct a schema-shaped entry, filling ``added_at`` from the clock seam if omitted.

    Uses lib.now_utc (which honors DAILY_HOTSPOTS_NOW/SCHEDULE_NOW), so tests stay deterministic."""
    entry = {
        "handle": normalize_handle(handle),
        "track": track,
        "tier": tier,
        "enabled": bool(enabled),
        "added_at": added_at or iso(now_utc()),
        "provenance": provenance,
    }
    if topic_filter is not None:
        entry["topic_filter"] = topic_filter
    if notes is not None:
        entry["notes"] = notes
    return entry


def upsert_entry(roster, entry: dict) -> dict:
    """PROPOSE-ADD approval primitive (§8): insert or update an entry by handle, validating first.

    A new handle is appended; an existing one is updated in place (keeping list position). The entry
    is schema-validated before it touches the roster — fail-closed: an invalid entry raises
    ValueError rather than corrupting the roster. Returns the stored entry."""
    errs = validate_entry(entry)
    if errs:
        raise ValueError("invalid roster entry: " + "; ".join(errs))
    existing = find_entry(roster, entry["handle"])
    if existing is not None:
        existing.clear()
        existing.update(entry)
        return existing
    # ensure the canonical container exists, then append
    if isinstance(roster, dict):
        roster.setdefault("entries", [])
        if not isinstance(roster["entries"], list):
            roster["entries"] = []
        roster["entries"].append(entry)
    elif isinstance(roster, list):
        roster.append(entry)
    return entry


# --------------------------------------------------------------------------- I/O (edges)

def resolve_roster_path(explicit: str | None = None) -> Path:
    """roster.json lives in the config companion (probe order mirrors archive.resolve_archive_dir)."""
    if explicit:
        return Path(explicit).expanduser()
    d = find_config_dir()
    if d:
        return d / "roster.json"
    return Path.home() / ".daily-hotspots-config" / "roster.json"


def _read_roster_file(p: Path) -> tuple:
    """``(roster, error)``: parse + normalize roster.json at ``p``.

    ``error`` is None when the file is ABSENT (a legitimately-empty roster) OR parsed cleanly; a
    non-None string names the CORRUPTION when the file EXISTS but cannot be parsed. This is the seam
    that lets a caller tell 'no roster yet' apart from 'roster present but unreadable' (§4: never
    silently degrade a broken asset at run time) — the empty fallback is identical, only the signal
    differs."""
    if p.is_file():
        try:
            return normalize_roster(json.loads(p.read_text(encoding="utf-8-sig"))), None
        except Exception as e:
            return ({"schema_version": ROSTER_SCHEMA_VERSION, "entries": []},
                    f"{type(e).__name__}: {e}")
    return {"schema_version": ROSTER_SCHEMA_VERSION, "entries": []}, None


def load_roster(path: str | None = None, warn: bool = True) -> dict:
    """Load + normalize roster.json to the canonical object form. Never raises.

    A MISSING companion legitimately degrades to an empty roster (mirrors lib.load_config) — silent,
    because 'no roster yet' is a valid state (open-discovery keyword search still runs). A PRESENT but
    CORRUPT file is DIFFERENT: it would silently nullify the roster asset (plan_pulls -> no tasks ->
    zero KOL pulls -> keyword-only discovery) while the daily cron NEVER runs verify_config to catch
    it. So a corrupt file is NOT treated as merely-missing: it still degrades to empty (the run's
    keyword lane must keep working — a hard raise would take the whole discovery pipeline down with
    it) but emits a LOUD stderr warning naming the corruption, so the failure is never MUTE (§4).
    ``warn=False`` silences the channel for callers that surface the state themselves (e.g.
    verify_config already schema-gates roster.json)."""
    p = resolve_roster_path(path)
    roster, err = _read_roster_file(p)
    if err is not None and warn:
        print(f"[daily-hotspots] WARNING: roster.json at {p} EXISTS but is CORRUPT ({err}); "
              f"treating the roster as EMPTY this run -> KOL account-pulls DISABLED and discovery "
              f"falls back to keyword-only. Fix the file or run scripts/verify_config.py.",
              file=sys.stderr)
    return roster


def save_roster(roster, path: str | None = None, validate: bool = True) -> Path:
    """Write the roster to disk in canonical object form (indent=2, LF, utf-8).

    Validates before writing by default (fail-closed — never persist a corrupt roster). Callers that
    must force a write can pass ``validate=False``."""
    norm = normalize_roster(roster)
    if validate:
        ok, errs = validate_roster(norm)
        if not ok:
            raise ValueError("refusing to save invalid roster: " + "; ".join(errs))
    p = resolve_roster_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(norm, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------- CLI (edge)

def main(argv: list | None = None) -> int:
    """Tiny CLI. ``validate`` (stdin roster -> ok/errors) or ``plan`` (stdin roster -> pull plan).

    With no stdin, both fall back to the on-disk roster.json (config-dir probe)."""
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "plan"
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    roster = normalize_roster(json.loads(raw)) if raw else load_roster()

    if cmd == "validate":
        ok, errs = validate_roster(roster)
        print(json.dumps({"ok": ok, "errors": errs}, ensure_ascii=False))
        return 0 if ok else 1
    if cmd == "plan":
        print(json.dumps(plan_pulls(roster, load_config()), ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "errors": [f"unknown command '{cmd}'"]}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
