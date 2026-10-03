#!/usr/bin/env python3
"""Initialize the spec-conformant companion config repo for daily-hotspots (config-spec E3/E4).

Deterministic + template-driven: re-running with the same --out produces byte-identical output, so
generation is reproducible (E4). Stamps a Mode-B skeleton (secrets gitignored) into the companion
config dir that `lib.find_config_dir()` discovers; it never writes secrets and never echoes any.

Discovery convention this skill uses (also in CONFIG.md, E2), first that exists wins:
  1. $DAILY_HOTSPOTS_CONFIG
  2. ~/.daily-hotspots-config/
  3. ~/.config/daily-hotspots-config/

Usage:
  python scripts/init_config.py [--out <dir>] [--force]

--out   target dir; default is the primary discovery path ~/.daily-hotspots-config/.
Stdlib only. Cross-platform. Writes only skeleton/template files, never secret values.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/daily-hotspots/scripts"))
import private_storage
import roster

SKILL = "daily-hotspots"
ENV_VAR = "DAILY_HOTSPOTS_CONFIG"
DEFAULT_DIR = "~/.daily-hotspots-config"
SPEC_VERSION = "1.0"

GITIGNORE = """\
# Secrets gate (config-spec E6 / Mode B), real values never enter git.
secrets/*
!secrets/README.md
!secrets/.gitkeep
*.env
!*.env.template
!env.template
claude.json
.claude.json
*credentials*.json
*.key
*.pem
!*.key.template
!*.pem.template
"""

SECRETS_README = """\
# secrets/, Mode B (gitignored)

Real secret values live here and are **gitignored** (see ../.gitignore). They never enter git.
Back them up out-of-band (cloud sync / encrypted drive). Restore on a new machine by copying the
`*.env` files back into this directory, then re-running `scripts/verify_config.py`.

Active storage mode: **B** (gitignored + out-of-band backup).
daily-hotspots has **no net-new secret**: push egress is the shared Agent Center #hotspots relay
stream (schedule-reminder relay.py), not a dedicated bot, and every data-source key
(twitterapi / brightdata / reddit OAuth) reuses `companion-config`; do not duplicate them here.

If a tool ever needs a repo-local secret, add `secrets/<slug>.env` (KEY=VALUE, one per line).
Files MUST be UTF-8 without BOM.
"""

# A safe minimal watchlist: a no-op that inherits every DEFAULT_CONFIG value. Edit to tune.
# (An empty list would REPLACE a default list, so the skeleton sets no lists, see CONFIG.md.)
WATCHLIST = {"schema_version": 1}

# Mode-B audit inventory skeleton (tools[] empty; populate per CONFIG.md).
REGISTRY = {
    "schema_version": 1,
    "spec_version": SPEC_VERSION,
    "companion_of": SKILL,
    "mode": "B",
    "tools": [],
}

# Runtime rosters start empty. Synthetic planner examples live only in generated test fixtures.
ROSTER = {"schema_version": 1, "entries": []}


def write(path, content, force):
    if os.path.exists(path) and not force:
        print("  SKIP (exists): %s" % path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("  wrote: %s" % path)


def main():
    ap = argparse.ArgumentParser(description="Stamp the daily-hotspots companion config repo.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    out = a.out or os.environ.get(ENV_VAR) or DEFAULT_DIR
    out = os.path.abspath(os.path.expanduser(out))
    try:
        out = str(private_storage.prove(out))
    except RuntimeError as exc:
        raise RuntimeError(
            "Initialize or clone a separate PRIVATE GitHub companion with committed history, "
            "an origin and a fresh PRIVATE visibility receipt before running init_config."
        ) from exc

    os.makedirs(out, exist_ok=True)
    roster_path = private_storage.prove(roster.resolve_config_roster_path(out))
    print("Init config for skill '%s' (mode B) at %s" % (SKILL, out))
    print("Discovery env var: %s  (fallback %s)" % (ENV_VAR, DEFAULT_DIR))

    write(os.path.join(out, "watchlist.json"),
          json.dumps(WATCHLIST, indent=2, ensure_ascii=False) + "\n", a.force)
    write(os.path.join(out, "registry.json"),
          json.dumps(REGISTRY, indent=2, ensure_ascii=False) + "\n", a.force)
    write(str(roster_path),
          json.dumps(ROSTER, indent=2, ensure_ascii=False) + "\n", a.force)
    write(os.path.join(out, ".gitignore"), GITIGNORE, a.force)
    write(os.path.join(out, "secrets", "README.md"), SECRETS_README, a.force)
    write(os.path.join(out, "secrets", ".gitkeep"), "", a.force)

    print("\nNext:")
    print("  1) Tune watchlist.json (full schema + example in CONFIG.md).")
    print("  2) Curate the empty roster at %s; add only accounts you have chosen to monitor." % roster_path)
    print("  3) Push egress = the Agent Center #hotspots relay (schedule-reminder); no dedicated bot / no secret here.")
    print("  4) export %s=%s   (or use the default path)" % (ENV_VAR, out))
    print("  5) python scripts/verify_config.py   # doctor: confirms the config is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
