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

# roster.json seed (config-spec §12.3 / source-coverage design Appendix A). The X KOL roster is the
# one genuinely-new data asset the design turns on; a clean install must ship it SEEDED, not dark
# (an empty/absent roster -> zero KOL pulls -> keyword-only discovery, the audit's Grade D+). These are
# the verified-live starter handles (audit 2026-07-13), mapped to tracks; the weekly yield engine then
# refines the roster (auto-prune / propose-add). added_at is a FIXED seed date so re-running the
# installer stays byte-identical (E4). Mirrors tests/fixtures/roster.sample.json. Edit freely: the
# installer SKIPs an existing roster.json (never clobbers your curation).
_SEED_DATE = "2026-07-13T00:00:00Z"


def _seed(handle, track, notes, topic_filter=None):
    e = {"handle": handle, "track": track, "tier": 1, "enabled": True,
         "added_at": _SEED_DATE, "provenance": "seed"}
    if topic_filter is not None:
        e["topic_filter"] = topic_filter
    e["notes"] = notes
    return e


ROSTER = {
    "schema_version": 1,
    "entries": [
        _seed("karpathy", "ai-agents",
              "ai-agents / research (audit-verified 2026-07-13: 3.36M followers, live tweets)"),
        _seed("swyx", "ai-agents", "ai-agents / research"),
        _seed("DrJimFan", "ai-agents", "ai-agents / research"),
        _seed("hwchase17", "ai-agents", "ai-agents / research (LangChain)"),
        _seed("yoheinakajima", "ai-agents", "ai-agents / research (BabyAGI)"),
        _seed("simonw", "ai-agents", "ai-agents / research"),
        _seed("jerryjliu0", "ai-agents", "ai-agents / research (LlamaIndex)"),
        _seed("levelsio", "dev-tools",
              "dev-tools / builders; topic_filter per spec Appendix A (broad-interest account)",
              topic_filter="(AI OR coding OR startup OR ship)"),
        _seed("gregisenberg", "dev-tools", "dev-tools / builders"),
        _seed("marclou", "dev-tools", "dev-tools / builders (NOT marc_louvion -> 404, per spec Appendix A)"),
        _seed("garrytan", "dev-tools", "dev-tools / builders (YC)"),
        _seed("paulg", "dev-tools", "dev-tools / builders (YC)"),
        _seed("VitalikButerin", "fintech-crypto", "fintech-crypto"),
        _seed("balajis", "fintech-crypto",
              "fintech-crypto; high-follower/noisy -> topic_filter recommended (spec Appendix A)",
              topic_filter="(startup OR crypto OR bitcoin OR AI OR founder OR build)"),
        _seed("dylan522p", "hardware-iot",
              "infra / systems (SemiAnalysis); mapped to hardware-iot track (closest of the 6 tracks)"),
    ],
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
    write(os.path.join(out, "roster.json"),
          json.dumps(ROSTER, indent=2, ensure_ascii=False) + "\n", a.force)
    write(os.path.join(out, ".gitignore"), GITIGNORE, a.force)
    write(os.path.join(out, "secrets", "README.md"), SECRETS_README, a.force)
    write(os.path.join(out, "secrets", ".gitkeep"), "", a.force)

    print("\nNext:")
    print("  1) Tune watchlist.json (full schema + example in CONFIG.md).")
    print("  2) Review roster.json (seeded with the Appendix A verified-live X handles; edit freely — "
          "the weekly yield engine then auto-prunes / proposes additions).")
    print("  3) For the Discord push bot: secrets/discord-hotspots.env with real values (gitignored).")
    print("  4) export %s=%s   (or use the default path)" % (ENV_VAR, out))
    print("  5) python scripts/verify_config.py   # doctor: confirms the config is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
