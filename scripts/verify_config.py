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
        if detail and not ok:
            line += "  -> %s" % detail
        print(line)
    print("-" * 60)
    if n_fail:
        print("NOT READY: %d check(s) failed. Fix the above (or re-run init_config.py)." % n_fail)
        return 1
    print("READY: config at %s conforms. Tune watchlist.json + add secrets/<slug>.env." % cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
