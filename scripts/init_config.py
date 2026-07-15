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
# the LIVE-VERIFIED starter handles (twitterapi get_user_info sweep 2026-07-13: each resolves + is
# active statusesCount>0, follower count recorded in notes), mapped so ALL SIX tracks have real X
# voices (the audit found 5 of 6 tracks blind). The weekly yield engine then refines the roster
# (auto-prune / propose-add). added_at is a FIXED seed date so re-running the installer stays
# byte-identical (E4). Mirrors tests/fixtures/roster.sample.json (that fixture is GENERATED from this
# ROSTER, so the two are byte-identical). Edit freely: the installer SKIPs an existing roster.json
# (never clobbers your curation). Drifted/dead handles the sweep caught are corrected here:
# t3dotgg->theo (redirect stub), leeerob->leerob (statusesCount:0 moved stub), marc_louvion->marclou
# (404), aeyakovenko->rajgokal (Solana handle not found on this API), brianchesky dropped
# (statusesCount:0 stub), realGeorgeHotz still FLAGGED not-seeded (purged).
_SEED_DATE = "2026-07-13T00:00:00Z"


def _seed(handle, track, notes, topic_filter=None):
    # §5.1 field order: handle, track, tier, enabled, topic_filter?, added_at, provenance, notes?
    e = {"handle": handle, "track": track, "tier": 1, "enabled": True}
    if topic_filter is not None:
        e["topic_filter"] = topic_filter
    e["added_at"] = _SEED_DATE
    e["provenance"] = "seed"
    e["notes"] = notes
    return e


ROSTER = {
    "schema_version": 1,
    "entries": [
        # --- ai-agents / research (10) ---
        _seed("karpathy", "ai-agents",
              "ai-agents / research (audit-verified 2026-07-13: 3.36M followers, live tweets)"),
        _seed("swyx", "ai-agents", "ai-agents / research"),
        _seed("DrJimFan", "ai-agents", "ai-agents / research"),
        _seed("hwchase17", "ai-agents", "ai-agents / research (LangChain)"),
        _seed("yoheinakajima", "ai-agents", "ai-agents / research (BabyAGI)"),
        _seed("simonw", "ai-agents", "ai-agents / research"),
        _seed("jerryjliu0", "ai-agents", "ai-agents / research (LlamaIndex)"),
        _seed("AndrewYNg", "ai-agents",
              "ai-agents / research (Coursera, DeepLearning.AI; verified 2026-07-13: 1.69M)"),
        _seed("omarsar0", "ai-agents",
              "ai-agents / research (elvis, DAIR.AI, agents focus; verified 2026-07-13: 311K)"),
        _seed("_philschmid", "ai-agents",
              "ai-agents / research (Agents & Gemini API @GoogleDeepMind; verified 2026-07-13: 99K)"),
        # --- dev-tools / builders (11) ---
        _seed("levelsio", "dev-tools",
              "dev-tools / builders; topic_filter per spec Appendix A (broad-interest account)",
              topic_filter="(AI OR coding OR startup OR ship)"),
        _seed("gregisenberg", "dev-tools", "dev-tools / builders"),
        _seed("marclou", "dev-tools", "dev-tools / builders (NOT marc_louvion -> 404, per spec Appendix A)"),
        _seed("garrytan", "dev-tools", "dev-tools / builders (YC)"),
        _seed("paulg", "dev-tools", "dev-tools / builders (YC)"),
        _seed("rauchg", "dev-tools",
              "dev-tools / builders (Vercel CEO; verified 2026-07-13: 669K)"),
        _seed("theo", "dev-tools",
              "dev-tools / builders (t3.gg / T3 Chat; verified 2026-07-13: 360K; "
              "corrected from t3dotgg redirect stub)"),
        _seed("leerob", "dev-tools",
              "dev-tools / builders (Cursor, ex-Vercel; verified 2026-07-13: 270K; "
              "corrected from leeerob -> statusesCount:0 moved stub)"),
        _seed("dhh", "dev-tools",
              "dev-tools / builders (Ruby on Rails, 37signals; verified 2026-07-13: 741K)"),
        _seed("mitchellh", "dev-tools",
              "dev-tools / builders (Ghostty, ex-HashiCorp/Terraform; verified 2026-07-13: 214K)"),
        _seed("amasad", "dev-tools",
              "dev-tools / builders (Replit CEO; verified 2026-07-13: 472K)"),
        # --- saas-niche / bootstrap (8; track was EMPTY pre-expansion) ---
        _seed("arvidkahl", "saas-niche",
              "saas-niche / bootstrap (The Bootstrapped Founder, build-in-public; verified 2026-07-13: 204K)"),
        _seed("tylertringas", "saas-niche",
              "saas-niche / bootstrap (Calm Company Fund, Storemapper; verified 2026-07-13: 31K)"),
        _seed("robwalling", "saas-niche",
              "saas-niche / bootstrap (TinySeed, MicroConf, Startups for the Rest of Us; verified 2026-07-13: 40K)"),
        _seed("jasonfried", "saas-niche",
              "saas-niche / bootstrap (37signals, Basecamp/HEY; verified 2026-07-13: 3.21M)"),
        _seed("csallen", "saas-niche",
              "saas-niche / bootstrap (Indie Hackers founder; verified 2026-07-13: 70K)"),
        _seed("agazdecki", "saas-niche",
              "saas-niche / bootstrap (Acquire.com founder; verified 2026-07-13: 312K)"),
        _seed("patio11", "saas-niche",
              "saas-niche / bootstrap (Patrick McKenzie, Stripe advisor, Bits about Money; verified 2026-07-13: 196K)"),
        _seed("dvassallo", "saas-niche",
              "saas-niche / bootstrap (Daniel Vassallo, Small Bets; verified 2026-07-13: 203K)"),
        # --- fintech-crypto (8) ---
        _seed("VitalikButerin", "fintech-crypto", "fintech-crypto"),
        _seed("balajis", "fintech-crypto",
              "fintech-crypto; high-follower/noisy -> topic_filter recommended (spec Appendix A)",
              topic_filter="(startup OR crypto OR bitcoin OR AI OR founder OR build)"),
        _seed("cdixon", "fintech-crypto",
              "fintech-crypto (a16z crypto Managing Partner; verified 2026-07-13: 933K)"),
        _seed("haydenzadams", "fintech-crypto",
              "fintech-crypto (Uniswap founder; verified 2026-07-13: 1.41M)"),
        _seed("RyanSAdams", "fintech-crypto",
              "fintech-crypto (Bankless co-founder; verified 2026-07-13: 276K)"),
        _seed("StaniKulechov", "fintech-crypto",
              "fintech-crypto (Aave founder & CEO; verified 2026-07-13: 301K)"),
        _seed("cobie", "fintech-crypto",
              "fintech-crypto; high-follower/noisy trader commentary -> topic_filter (verified 2026-07-13: 1.08M)",
              topic_filter="(crypto OR bitcoin OR eth OR ethereum OR defi OR token OR market OR trading OR onchain)"),
        _seed("rajgokal", "fintech-crypto",
              "fintech-crypto (Solana co-founder; verified 2026-07-13: 1.43M; aeyakovenko not found on "
              "this API -> rajgokal seeds Solana)"),
        # --- consumer-social (6; track was EMPTY pre-expansion) ---
        _seed("nikitabier", "consumer-social",
              "consumer-social (Head of Product @x, consumer growth; high-follower/noisy -> topic_filter; "
              "verified 2026-07-13: 1.20M)",
              topic_filter="(product OR startup OR growth OR consumer OR app OR viral OR founder OR launch OR distribution)"),
        _seed("eladgil", "consumer-social",
              "consumer-social (entrepreneur/investor, High Growth Handbook; verified 2026-07-13: 518K)"),
        _seed("packyM", "consumer-social",
              "consumer-social (Packy McCormick, Not Boring; verified 2026-07-13: 228K)"),
        _seed("bgurley", "consumer-social",
              "consumer-social (Bill Gurley, Benchmark, marketplaces; verified 2026-07-13: 770K)"),
        _seed("Suhail", "consumer-social",
              "consumer-social (ex-Mixpanel/Playground founder; verified 2026-07-13: 432K)"),
        _seed("naval", "consumer-social",
              "consumer-social (AngelList founder; high-follower/noisy philosophy feed -> topic_filter; "
              "verified 2026-07-13: 3.63M)",
              topic_filter="(startup OR founder OR build OR AI OR tech OR invest OR wealth OR product OR business OR leverage)"),
        # --- hardware-iot (6; spec Appendix A flagged this a GENUINE GAP; analysts/robotics founders
        #     are the closest live X voices, some noisy -> topic_filter) ---
        _seed("dylan522p", "hardware-iot",
              "infra / systems (SemiAnalysis); mapped to hardware-iot track (closest of the 6 tracks)"),
        _seed("adcock_brett", "hardware-iot",
              "hardware-iot (Figure humanoid robots founder; verified 2026-07-13: 655K)"),
        _seed("IanCutress", "hardware-iot",
              "hardware-iot (Dr Ian Cutress, TechTechPotato / More Than Moore semiconductors; verified 2026-07-13: 56K)"),
        _seed("ID_AA_Carmack", "hardware-iot",
              "hardware-iot (John Carmack, Keen Tech AGI, ex-Oculus VR; verified 2026-07-13: 2.93M)"),
        _seed("bunniestudios", "hardware-iot",
              "hardware-iot (Andrew 'bunnie' Huang, hardware hacker; verified 2026-07-13: 25K)"),
        _seed("Scobleizer", "hardware-iot",
              "hardware-iot (Robert Scoble, AI/robots/BCI futurist; high-follower/noisy firehose -> topic_filter; "
              "verified 2026-07-13: 592K)",
              topic_filter="(robot OR robotics OR AI OR hardware OR chip OR VR OR AR OR drone OR sensor OR BCI OR holodeck)"),
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
