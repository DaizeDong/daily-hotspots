#!/usr/bin/env python3
"""Initialize the spec-conformant companion config repo for daily-hotspots (config-spec E3/E4).

Deterministic + template-driven: re-running with the same --out produces byte-identical output, so
generation is reproducible (E4). Stamps a Mode-B skeleton (secrets gitignored) into the companion
config dir that `lib.find_config_dir()` discovers; it never writes secrets and never echoes any.

Discovery convention this skill uses (also in CONFIG.md, E2) — first that exists wins:
  1. $DAILY_HOTSPOTS_CONFIG
  2. ~/.daily-hotspots-config/
  3. ~/.config/daily-hotspots-config/

Usage:
  python scripts/init_config.py [--out <dir>] [--force]

--out   target dir; default is the primary discovery path ~/.daily-hotspots-config/.
Stdlib only. Cross-platform. Writes only skeleton/template files — never secret values.
"""
import argparse
import json
import os
import sys

SKILL = "daily-hotspots"
ENV_VAR = "DAILY_HOTSPOTS_CONFIG"
DEFAULT_DIR = "~/.daily-hotspots-config"
SPEC_VERSION = "1.0"

GITIGNORE = """\
# Secrets gate (config-spec E6 / Mode B) — real values never enter git.
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
# secrets/ — Mode B (gitignored)

Real secret values live here and are **gitignored** (see ../.gitignore). They never enter git.
Back them up out-of-band (cloud sync / encrypted drive). Restore on a new machine by copying the
`*.env` files back into this directory, then re-running `scripts/verify_config.py`.

Active storage mode: **B** (gitignored + out-of-band backup).
Net-new secret for daily-hotspots = the Discord push bot. Shared data-source keys reuse
`companion-config`; do not duplicate them here.

  # secrets/discord-hotspots.env   (gitignored)
  DISCORD_HOTSPOTS_BOT_TOKEN=...
  DISCORD_HOTSPOTS_USER_ID=...

Files MUST be UTF-8 without BOM.
"""

# A safe minimal watchlist: a no-op that inherits every DEFAULT_CONFIG value. Edit to tune.
# (An empty list would REPLACE a default list, so the skeleton sets no lists — see CONFIG.md.)
WATCHLIST = {"schema_version": 1}

# Mode-B audit inventory skeleton (tools[] empty; populate per CONFIG.md).
REGISTRY = {
    "schema_version": 1,
    "spec_version": SPEC_VERSION,
    "companion_of": SKILL,
    "mode": "B",
    "tools": [],
}


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

    out = a.out or DEFAULT_DIR
    out = os.path.abspath(os.path.expanduser(out))

    print("Init config for skill '%s' (mode B) at %s" % (SKILL, out))
    print("Discovery env var: %s  (fallback %s)" % (ENV_VAR, DEFAULT_DIR))

    write(os.path.join(out, "watchlist.json"),
          json.dumps(WATCHLIST, indent=2, ensure_ascii=False) + "\n", a.force)
    write(os.path.join(out, "registry.json"),
          json.dumps(REGISTRY, indent=2, ensure_ascii=False) + "\n", a.force)
    write(os.path.join(out, ".gitignore"), GITIGNORE, a.force)
    write(os.path.join(out, "secrets", "README.md"), SECRETS_README, a.force)
    write(os.path.join(out, "secrets", ".gitkeep"), "", a.force)

    print("\nNext:")
    print("  1) Tune watchlist.json (full schema + example in CONFIG.md).")
    print("  2) For the Discord push bot: secrets/discord-hotspots.env with real values (gitignored).")
    print("  3) export %s=%s   (or use the default path)" % (ENV_VAR, out))
    print("  4) python scripts/verify_config.py   # doctor: confirms the config is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
