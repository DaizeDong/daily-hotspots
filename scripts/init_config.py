#!/usr/bin/env python3
"""Initialize synthetic configuration templates for daily-hotspots.

Selection and required fields are defined in CONFIG.md and config.contract.json.
Explicit CLI paths isolate environment selection. Runtime uses the same pinned Guards
companion discovery; invalid selectors never fall through to another companion.
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
# Secrets gate (config-spec E6 / Mode B), Mode B excludes credential values from this backup.
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

Real secret values live here and are **gitignored** (see ../.gitignore). This is the default Mode B policy, not a prohibition on verified PRIVATE backup.
Back them up out-of-band under Mode B. An explicitly selected Mode A may instead version them only in a verified PRIVATE repository and restore from that history. Restore on a new machine by copying the
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

    from config_paths import companion_root
    out = a.out or companion_root() or DEFAULT_DIR
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
