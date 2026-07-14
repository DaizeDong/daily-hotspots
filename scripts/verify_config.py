#!/usr/bin/env python3
"""Doctor for the daily-hotspots companion config (config-spec E3). Resolves the config dir via the
SAME discovery order the skill uses (lib.find_config_dir), validates it against the contract in
CONFIG.md, and prints PASS/FAIL per check naming exactly what is missing.
Exit 0 = ready, 1 = not ready, 2 = usage error.

Discovery order (config-spec E2):
  1. $DAILY_HOTSPOTS_CONFIG   2. ~/.daily-hotspots-config/   3. ~/.config/daily-hotspots-config/

Usage:
  python scripts/verify_config.py [--config-dir <dir>]
Stdlib only. Never echoes secret values (only presence). Imports the skill's own lib when available
so the check exercises the REAL loader (load_config + guardrail clamp); degrades to a structural
check if lib cannot be imported.
"""
import argparse
import json
import os
import sys

PASS, FAIL = "PASS", "FAIL"

ENV_VAR = "DAILY_HOTSPOTS_CONFIG"
FALLBACKS = ["~/.daily-hotspots-config", "~/.config/daily-hotspots-config"]

# Make the skill's lib importable (verify lives at <repo>/scripts/, lib at <repo>/skills/.../scripts/).
_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(os.path.dirname(_HERE), "skills", "daily-hotspots", "scripts")
if os.path.isdir(_LIB_DIR) and _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
try:
    import lib  # type: ignore
except Exception:
    lib = None
try:
    import roster  # type: ignore  # roster.validate_roster — the roster.json schema gate (spec 5.1)
except Exception:
    roster = None


# Sibling skills this design delegates to (spec sec 4). daily-hotspots is an orchestration product;
# a missing sibling must fail LOUD here, never silently degrade at run time. The deterministic
# reachability probe is a junction/dir-existence check against the skills root (default
# ~/.claude/skills; override with $DAILY_HOTSPOTS_SKILLS_DIR for tests / alt installs).
DEPENDENCY_SKILLS = ("market-intel", "self-evolve", "schedule-reminder", "small-cap-deepdive")
SKILLS_DIR_ENV = "DAILY_HOTSPOTS_SKILLS_DIR"

# MCP servers the source-wiring layer needs (spec sec 1/6). Probed only with --check-mcp (a
# subprocess to `claude mcp list`), OFF by default so the doctor stays offline + deterministic.
REQUIRED_MCPS = ("twitterapi", "brightdata")


def skills_root():
    """Resolve the skills install root (where sibling skills are junctioned)."""
    v = os.environ.get(SKILLS_DIR_ENV)
    if v:
        return os.path.abspath(os.path.expanduser(v))
    return os.path.abspath(os.path.expanduser(os.path.join("~", ".claude", "skills")))


def check_dependency_skills(skills_dir=None, required=DEPENDENCY_SKILLS):
    """Junction-probe each sibling skill (spec sec 4). Returns ``[(name, ok, detail)]`` — ok when the
    skill directory is reachable under skills_dir. Pure filesystem, no network (deterministic)."""
    root = skills_dir or skills_root()
    out = []
    for name in required:
        p = os.path.join(root, name)
        out.append((name, os.path.isdir(p), p))
    return out


def check_required_mcps(required=REQUIRED_MCPS, runner=None):
    """Best-effort MCP reachability via ``claude mcp list`` (spec sec 4). Returns
    ``[(name, ok, detail)]``. ``runner`` (a callable returning the listing text) is injectable for
    tests; the default shells out to the claude CLI with a short timeout. If the CLI is unavailable
    the checks report a soft SKIP (ok=True) rather than a false FAIL — absence of the tool is not
    absence of the server."""
    if runner is None:
        def runner():
            import subprocess
            return subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True,
                                  timeout=20).stdout
    try:
        text = (runner() or "").lower()
    except Exception as e:
        return [(name, True, "claude mcp list unavailable (%s) - skipped" % type(e).__name__)
                for name in required]
    out = []
    for name in required:
        present = name.lower() in text
        out.append((name, present,
                    "reachable" if present else "not present in `claude mcp list`"))
    return out


def validate_yield_block(y):
    """Range/type gate for the watchlist.json ``yield`` tuning block (design spec 8/9). Returns
    ``(ok, errors)``.

    The runtime loader (lib._clamp_guardrails) TIGHTENS the §9 anti-self-deception rails so a
    fat-fingered threshold can never actually gut the roster — but a silently-clamped value means the
    doctor would say READY while the user's setting was ignored. So this surfaces LOUDLY any value in
    the loosening direction (it will be clamped, i.e. NOT honored) as well as malformed types:

      * yield.floor > 0            -> would count productive handles "dead" (mass-prune); clamped to 0
      * yield.prune_after_weeks<2  -> prunes on too little evidence; clamped to 2
      * yield.min_history_days<7   -> nullifies the cold-start guard; clamped to 7

    A user may still make each STRICTER (prune slower / require more history) with no complaint."""
    errs = []
    if y is None:
        return True, errs
    if not isinstance(y, dict):
        return False, ["yield must be a JSON object, got %s" % type(y).__name__]

    def _num(k):
        if k not in y:
            return None
        v = y[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append("yield.%s must be a number, got %r" % (k, v))
            return None
        return float(v)

    for k in ("window_days", "propose_add_min_count", "pre_viral_faves_threshold",
              "noisy_pull_min", "noisy_yield_max"):
        _num(k)
    fl = _num("floor")
    if fl is not None and fl > 0:
        errs.append("yield.floor=%r > default 0 (would mass-prune; runtime clamps it to 0)" % y.get("floor"))
    pw = _num("prune_after_weeks")
    if pw is not None and pw < 2:
        errs.append("yield.prune_after_weeks=%r < default 2 (prunes too fast; runtime clamps to 2)"
                    % y.get("prune_after_weeks"))
    mh = _num("min_history_days")
    if mh is not None and mh < 7:
        errs.append("yield.min_history_days=%r < default 7 (nullifies cold-start; runtime clamps to 7)"
                    % y.get("min_history_days"))
    # window_days is the §1/§9 pre-viral guard's reach; a window below max(shipped default, prune span)
    # blinds it while decide_prune still prunes (audit HARDEN r3). The runtime FLOORS it up, so a
    # too-small window is silently lifted — surface it here or the user never learns their setting was
    # ignored. prune_after_weeks is itself clamped to >= 2, so the span is >= 14; the default is 30.
    wd = _num("window_days")
    if wd is not None:
        pw_eff = pw if (pw is not None and pw >= 2) else 2
        default_wd = float(lib.DEFAULT_CONFIG["yield"]["window_days"]) if lib is not None else 30.0
        floor = max(default_wd, 7.0 * pw_eff)
        if wd < floor:
            errs.append("yield.window_days=%r < guard floor %d (max of default 30 and prune span "
                        "7*prune_after_weeks); blinds the pre-viral prune guard (runtime floors it "
                        "up to %d)" % (y.get("window_days"), int(floor), int(floor)))
    return (len(errs) == 0), errs


def validate_sources_block(sources, yield_block=None):
    """Range/type gate for watchlist.json ``sources.twitterapi.min_faves_rostered`` (design spec 6/8/9).
    Returns ``(ok, errors)``.

    A rostered pull exists to catch a founder's post BELOW the keyword search's own ``min_faves:500``
    floor (§6 pre-viral). Left unbounded this knob routes AROUND the §9 anti-mass-prune clamp — set it
    high and every rostered pull keeps 0 tweets (numerator 0) while the pulls-log denominator still
    accrues, so after prune_after_weeks weeks the WHOLE roster reads dead and ``--apply`` disables it.
    roster._min_faves_rostered CAPS it at the keyword floor (a user pre_viral_faves_threshold that is
    LOWER tightens the cap), but a silently-capped value means the doctor would say READY while the
    user's setting was ignored — so surface any value in the loosening direction, plus malformed
    types. Mirrors validate_yield_block's contract (loud about a will-be-clamped value)."""
    errs = []
    if not isinstance(sources, dict):
        return True, errs
    tw = sources.get("twitterapi")
    if not isinstance(tw, dict) or "min_faves_rostered" not in tw:
        return True, errs
    v = tw.get("min_faves_rostered")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False, ["sources.twitterapi.min_faves_rostered must be a number, got %r" % v]
    cap = roster.KEYWORD_FAVES_FLOOR if roster is not None else 500
    if isinstance(yield_block, dict):
        pv = yield_block.get("pre_viral_faves_threshold")
        if not isinstance(pv, bool) and isinstance(pv, (int, float)) and 0 <= pv < cap:
            cap = int(pv)
    if v < 0:
        errs.append("sources.twitterapi.min_faves_rostered=%r < 0 (runtime clamps to 0)" % v)
    elif v > cap:
        errs.append("sources.twitterapi.min_faves_rostered=%r > keyword floor %d (defeats the "
                    "pre-viral catch + routes around the anti-mass-prune clamp; runtime caps to %d)"
                    % (v, cap, cap))
    return (len(errs) == 0), errs


def validate_source_filters(sources):
    """Type gate for the community-lane keep/drop whitelists (design spec 6). Returns ``(ok, errors)``.

    Each community source (linux.do / v2ex / cn-feeds) filters items by ``keep_nodes`` /
    ``keep_categories`` / ``drop_nodes`` / ``drop_categories``, which MUST be JSON arrays. A bare
    string (``"keep_nodes": "geek"`` instead of ``["geek"]``) is a plausible typo that the runtime
    coerces to a single-element list — but if that coercion is ever removed, iterating the string
    character-by-character (``{'g','e','k'}``) silently blinds the ENTIRE lane (every item dropped as
    not-whitelisted). The doctor must not print READY over that, so any non-array keep/drop value is a
    LOUD FAIL here naming the exact source + key. A number/object is likewise rejected."""
    errs = []
    if not isinstance(sources, dict):
        return True, errs
    filter_keys = ("keep_nodes", "keep_categories", "drop_nodes", "drop_categories")
    for sname, scfg in sources.items():
        if not isinstance(scfg, dict):
            continue
        for k in filter_keys:
            if k not in scfg:
                continue
            v = scfg[k]
            if not isinstance(v, list):
                errs.append("sources.%s.%s must be a JSON array (list), got %s %r — a bare string is "
                            "iterated char-by-char and blinds the whole lane (use [\"...\"])"
                            % (sname, k, type(v).__name__, v))
            elif not all(isinstance(x, str) for x in v):
                errs.append("sources.%s.%s must be a list of strings" % (sname, k))
    return (len(errs) == 0), errs


def discover(override):
    if override:
        return os.path.abspath(os.path.expanduser(override)), "explicit (--config-dir)"
    # Prefer the skill's own resolver so doctor and runtime can never diverge.
    if lib is not None and not os.environ.get("_DH_FORCE_LOCAL_DISCOVER"):
        try:
            d = lib.find_config_dir()
            if d:
                return os.path.abspath(str(d)), "lib.find_config_dir"
        except Exception:
            pass
    val = os.environ.get(ENV_VAR)
    if val and os.path.isdir(os.path.expanduser(val)):
        return os.path.abspath(os.path.expanduser(val)), "env:%s" % ENV_VAR
    for d in FALLBACKS:
        dd = os.path.expanduser(d)
        if os.path.isdir(dd):
            return os.path.abspath(dd), "default:%s" % dd
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Validate the daily-hotspots companion config.")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--check-mcp", action="store_true",
                    help="also probe MCP reachability via `claude mcp list` (subprocess; off by "
                         "default so the doctor stays offline/deterministic)")
    a = ap.parse_args()

    cfg, how = discover(a.config_dir)
    print("Config doctor for skill 'daily-hotspots'")
    print("Discovery env var: %s  (fallbacks %s)" % (ENV_VAR, ", ".join(FALLBACKS)))
    if not cfg:
        print("  [%s] config located -> none found." % FAIL)
        print("       The skill still RUNS on built-in defaults (degrade-safe), but to tune it:")
        print("       set %s=<dir> or run: python scripts/init_config.py" % ENV_VAR)
        return 1
    print("  resolved via %s -> %s" % (how, cfg))
    print("-" * 60)

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    check("config dir exists", os.path.isdir(cfg))

    # watchlist.json — the user-tunable surface (optional file, but if present must be valid).
    wl = os.path.join(cfg, "watchlist.json")
    wl_present = os.path.isfile(wl)
    check("watchlist.json present", wl_present,
          "absent => skill runs on built-in DEFAULT_CONFIG (allowed)")
    if wl_present:
        try:
            with open(wl, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            check("watchlist.json valid JSON object", isinstance(data, dict),
                  "type %s" % type(data).__name__)
            if isinstance(data, dict) and "schema_version" in data:
                check("schema_version is int", isinstance(data.get("schema_version"), int),
                      "got %r" % data.get("schema_version"))
            # §9 yield tuning block (spec 8/9): a loosening/malformed value is silently tightened by
            # the runtime clamp, so surface it here or the user never learns their setting was ignored.
            yok, yerrs = validate_yield_block(data.get("yield") if isinstance(data, dict) else None)
            check("yield block within §9 guardrails (spec 8/9)", yok, "; ".join(yerrs[:4]))
            # §6/§8/§9 collection-side rail: min_faves_rostered routes around the anti-mass-prune
            # clamp if left unbounded — surface a will-be-capped value the same way the yield block is.
            sok, serrs = validate_sources_block(
                data.get("sources") if isinstance(data, dict) else None,
                data.get("yield") if isinstance(data, dict) else None)
            check("min_faves_rostered within anti-mass-prune cap (spec 6/8/9)", sok,
                  "; ".join(serrs[:4]))
            # §6 community-lane rail: keep/drop whitelists must be arrays. A bare-string typo
            # ("keep_nodes":"geek") is char-shredded at runtime unless coerced and silently blinds the
            # whole lane — surface the bad type here so READY never hides a dark V2EX/linux.do lane.
            sfok, sferrs = validate_source_filters(
                data.get("sources") if isinstance(data, dict) else None)
            check("community source keep/drop lists are arrays (spec 6)", sfok,
                  "; ".join(sferrs[:4]))
        except Exception as e:
            check("watchlist.json valid JSON object", False, str(e))

    # registry.json — optional Mode-B audit inventory.
    reg = os.path.join(cfg, "registry.json")
    if os.path.isfile(reg):
        try:
            with open(reg, "r", encoding="utf-8-sig") as f:
                rdata = json.load(f)
            check("registry.json valid JSON", True)
            check("registry mode == 'B'", rdata.get("mode") == "B", "got %r" % rdata.get("mode"))
            check("registry tools[] is a list", isinstance(rdata.get("tools"), list),
                  "type %s" % type(rdata.get("tools")).__name__)
        except Exception as e:
            check("registry.json valid JSON", False, str(e))

    # secrets gate (Mode B).
    check("secrets/ dir present", os.path.isdir(os.path.join(cfg, "secrets")))
    gi = os.path.join(cfg, ".gitignore")
    gi_ok = os.path.isfile(gi)
    check(".gitignore present", gi_ok)
    if gi_ok:
        txt = open(gi, "r", encoding="utf-8", errors="replace").read()
        check(".gitignore blocks secrets (secrets/* + *.env)", "secrets/" in txt and "*.env" in txt)

    # self-contained (E5): no absolute-path leakage in committed config files.
    leak = []
    for rel in ("watchlist.json", "registry.json", ".gitignore",
                os.path.join("secrets", "README.md")):
        p = os.path.join(cfg, rel)
        if os.path.isfile(p):
            t = open(p, "r", encoding="utf-8", errors="replace").read()
            if any(s in t for s in ("C:\\", "C:/", "/home/", "/Users/", "/root/")):
                leak.append(rel)
    check("self-contained (no hardcoded absolute paths)", not leak, "leaks in %s" % leak)

    # roster.json — the X KOL roster data asset (spec 5.1). Absent => empty roster (the X
    # open-discovery keyword search still runs), but a PRESENT roster must be schema-valid so a
    # malformed handle/tier never silently corrupts the account-pull loop or the yield engine.
    rj = os.path.join(cfg, "roster.json")
    rj_present = os.path.isfile(rj)
    check("roster.json present (X KOL roster)", rj_present,
          "absent => empty roster; seed per design Appendix A (open-discovery search still runs)")
    if rj_present:
        try:
            with open(rj, "r", encoding="utf-8-sig") as f:
                rdata = json.load(f)
            if roster is not None:
                rok, rerrs = roster.validate_roster(rdata)
                check("roster.json schema valid (spec 5.1)", rok, "; ".join(rerrs[:4]))
            else:
                check("roster.json valid JSON (roster.py not importable -> structural only)",
                      isinstance(rdata, (dict, list)), "type %s" % type(rdata).__name__)
        except Exception as e:
            check("roster.json schema valid (spec 5.1)", False, str(e))

    # dependency-skill reachability (spec 4): the sibling skills daily-hotspots delegates to must be
    # junctioned + reachable, or the install silently degrades. A junction probe fails LOUD instead.
    for name, ok, detail in check_dependency_skills():
        check("dependency skill reachable: %s" % name, ok,
              "not found at %s (junction it; see spec 4/12)" % detail)
    # MCP reachability (spec 4): the source-wiring MCPs (twitterapi/brightdata) the whole design
    # depends on. The probe is a `claude mcp list` SUBPROCESS, so it stays opt-in behind --check-mcp
    # to keep the doctor offline/deterministic. But §4 says "never silently degrade": when the probe
    # DOES run, a soft-SKIP (claude CLI absent -> ok=True) must not masquerade as a verified PASS (the
    # printer below surfaces its detail even on PASS), and on the DEFAULT run the doctor must not stay
    # MUTE about these MCPs — it emits a visible SKIP advisory naming them + how to verify (below the
    # results). Either way a dead X-roster / linux.do lane can never hide behind a bare READY.
    if a.check_mcp:
        for name, ok, detail in check_required_mcps():
            check("MCP reachable: %s" % name, ok, detail)

    # exercise the REAL loader so doctor proves the runtime contract (degrade-safe + guardrails).
    if lib is not None:
        try:
            loaded = lib.load_config(str(wl) if wl_present else None)
            ok = isinstance(loaded, dict) and int(loaded["scoring"]["min_independent_sources"]) >= 2
            check("lib.load_config loads + guardrails hold (min_independent_sources >= 2)", ok)
        except Exception as e:
            check("lib.load_config loads", False, str(e))
    else:
        check("lib import (skipped — structural check only)", True,
              "lib.py not importable from %s" % _LIB_DIR)

    n_fail = sum(1 for _, ok, _ in results if not ok)
    for nm, ok, detail in results:
        line = "  [%s] %s" % (PASS if ok else FAIL, nm)
        # show the detail on any failure, and ALSO on an MCP check even when it "passed": a soft-SKIP
        # (claude CLI absent -> ok=True) must surface its skip reason so it can never masquerade as a
        # verified-reachable PASS (§4 no silent degrade).
        if detail and (not ok or nm.startswith("MCP reachable:")):
            line += "  -> %s" % detail
        print(line)
    # §4 never-silently-degrade: on the DEFAULT (no --check-mcp) run the doctor is otherwise MUTE about
    # the source-wiring MCPs the design depends on — surface them explicitly so READY can't imply an
    # MCP reachability it never checked.
    if not a.check_mcp:
        print("  [SKIP] MCP reachability NOT verified this run (offline default): %s"
              % ", ".join(REQUIRED_MCPS))
        print("         source-wiring depends on them; re-run with --check-mcp to probe "
              "`claude mcp list` (spec 4).")
    print("-" * 60)
    if n_fail:
        print("NOT READY: %d check(s) failed. Fix the above (or re-run init_config.py)." % n_fail)
        return 1
    print("READY: config at %s conforms. Tune watchlist.json + add secrets/<slug>.env." % cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
