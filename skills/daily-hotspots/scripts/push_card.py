#!/usr/bin/env python3
"""Discord delivery, tiered push (anti-spam) with hard limit validation.

Builds BOTH a Discord embed dict (for a future embed-capable bot) AND a plain-text rendering
(for the current content-only relay). Validates Discord hard limits BEFORE sending so nothing is
silently truncated by Discord:
    embed <=6000 total | <=25 fields | field.value <=1024 | <=10 embeds/msg | content <=2000

Delivery seam (clean bot switch, zero code change):
  DAILY_HOTSPOTS_RELAY_CMD, JSON list / shell string; receives the message on argv[1] or stdin.
  else fallback to a standalone content-only relay at a generic local default path.
Token is NEVER read or echoed here, the relay owns the token; this script only hands it text.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import redact as rd

# Standalone CLI prints an embed dict that can contain emoji; force UTF-8 so a legacy Windows (GBK)
# console does not crash with UnicodeEncodeError. (run.py path is unaffected, it never prints this.)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

EMBED_TOTAL = 6000
FIELD_VALUE = 1024
MAX_FIELDS = 25
MAX_EMBEDS = 10
CONTENT_MAX = 2000

_GRADE_COLOR = {"A": 0xE74C3C, "B+": 0xE67E22, "B": 0x3498DB,
                "C+": 0x95A5A6, "C": 0x95A5A6, "D": 0x7F8C8D}


def build_embed(card: dict, update: bool = False) -> dict:
    bd = card.get("score_breakdown", {})
    tag = "🔄 UPDATE" if update else "🆕 NEW"
    title = f"{tag} · {card.get('title', '?')[:240]}"
    desc_parts = []
    if card.get("why_now"):
        desc_parts.append("**Why now:** " + card["why_now"])
    if card.get("contrarian_insight"):
        desc_parts.append("**非共识:** " + card["contrarian_insight"])
    if card.get("action"):
        desc_parts.append("**行动:** " + card["action"])
    desc = "\n".join(desc_parts)[:4000]
    fields = [{"name": k, "value": str(round(float(v)))[:FIELD_VALUE], "inline": True}
              for k, v in bd.items()]
    isc = card.get("independent_source_count", 0)
    footer = f"{isc} 独立源 · score {card.get('final_score')} ({card.get('grade')}) · {card.get('run_id','')}"
    return {
        "title": title[:256],
        "url": (card.get("evidence", [{}])[0] or {}).get("url", ""),
        "color": _GRADE_COLOR.get(card.get("grade", "C"), 0x3498DB),
        "description": desc,
        "fields": fields[:MAX_FIELDS],
        "footer": {"text": footer[:2048]},
    }


def validate_embed(embed: dict) -> list[str]:
    errs = []
    total = len(embed.get("title", "")) + len(embed.get("description", "")) + \
        len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        total += len(f.get("name", "")) + len(f.get("value", ""))
        if len(f.get("value", "")) > FIELD_VALUE:
            errs.append(f"field {f.get('name')} value > {FIELD_VALUE}")
    if len(embed.get("fields", [])) > MAX_FIELDS:
        errs.append(f">{MAX_FIELDS} fields")
    if total > EMBED_TOTAL:
        errs.append(f"embed total {total} > {EMBED_TOTAL}")
    return errs


def render_text(card: dict, update: bool = False) -> str:
    tag = "[UPDATE]" if update else "[NEW]"
    bd = card.get("score_breakdown", {})
    dims = " ".join(f"{k}={round(float(v))}" for k, v in bd.items())
    ev = card.get("evidence", [])
    src = ", ".join(sorted(set(e.get("source", "?") for e in ev)))
    lines = [
        f"{tag} {card.get('title','?')}  ({card.get('grade')} {card.get('final_score')})",
        f"track: {card.get('track')} | types: {','.join(card.get('machine_type', []))}",
        f"dims: {dims}",
    ]
    if card.get("why_now"):
        lines.append(f"why-now: {card['why_now']}")
    if card.get("contrarian_insight"):
        lines.append(f"非共识: {card['contrarian_insight']}")
    if card.get("action"):
        lines.append(f"行动: {card['action']}")
    lines.append(f"{card.get('independent_source_count',0)} 独立源 [{src}]")
    for e in ev[:4]:
        lines.append(f"  - {e.get('source','?')}: {e.get('url','')}  ({e.get('signal','')})")
    return "\n".join(lines)


def _relay_cmd():
    env = os.environ.get("DAILY_HOTSPOTS_RELAY_CMD")
    if env:
        try:
            v = json.loads(env)
            if isinstance(v, list):
                return v
        except Exception:
            return shlex.split(env)
    # Pluggable Agent Center egress: if schedule-reminder (the base) is installed, route to the
    # #hotspots stream via its unified relay (per-stream identity + registry + Big-Brother fallback).
    # If the base is absent, fall back to the Big Brother relay so this skill still works standalone.
    rp = os.environ.get("SCHEDULE_RELAY_PY") or str(
        Path.home() / ".claude/skills/schedule-reminder/scripts/relay.py")
    if os.path.isfile(rp):
        return [sys.executable, rp, "send", "--stream", "hotspots", "--text"]
    return [sys.executable, str(Path.home() / ".claude/discord_relay/send.py")]


def deliver(message: str, dry_run: bool = False) -> tuple[bool, str]:
    """Send a (<=CONTENT_MAX, chunked by relay) text message. Length-only logging.

    EGRESS PII SCRUB (fail-safe, redact-in-place): the collected social content that feeds these
    headlines (reddit / twitter / linux.do / v2ex / HN) is untrusted DATA and can carry a real
    person's email / phone / card / secret / ip / discord-id. Before the message is handed to the
    relay we scrub ONLY those dangerous structured types in place (an email becomes [EMAIL_1]),
    while LEAVING the legitimate evidence URLs (<https://...>) and @handles intact, so one stray
    address is stripped cleanly and the digest still ships. A one-line note is logged on any scrub.
    """
    scrubbed = rd.scrub_egress(message)
    if scrubbed != message:
        try:
            found = rd.redact_egress(message)["found"]
            kinds = ",".join(sorted(found)) or "PII"
        except Exception:
            kinds = "PII"
        print(f"[push_card] egress scrub: redacted {kinds} before send", file=sys.stderr)
        message = scrubbed
    if len(message) > CONTENT_MAX:
        # the relay chunks on newlines; we still warn so callers can split into a digest file
        pass
    if dry_run or os.environ.get("DAILY_HOTSPOTS_DRYRUN"):
        return (True, f"[dry-run] would deliver {len(message)} chars")
    cmd = _relay_cmd() + [message]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        return (proc.returncode == 0, f"rc={proc.returncode} ({len(message)} chars)")
    except Exception as e:
        return (False, f"deliver error: {e!r}")


def push_card(card: dict, update: bool = False, dry_run: bool = False) -> dict:
    embed = build_embed(card, update)
    errs = validate_embed(embed)
    text = render_text(card, update)
    ok, detail = deliver(text, dry_run=dry_run)
    return {"ok": ok, "detail": detail, "embed_errors": errs, "embed": embed}


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    dry = bool(os.environ.get("DAILY_HOTSPOTS_DRYRUN"))
    res = push_card(data, update=bool(data.get("_update")), dry_run=dry)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
