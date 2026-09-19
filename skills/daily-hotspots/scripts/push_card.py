#!/usr/bin/env python3
"""Discord delivery, tiered push (anti-spam) with hard limit validation.

Builds BOTH a Discord embed dict (for a future embed-capable bot) AND a plain-text rendering
(for the current content-only relay). Validates Discord hard limits BEFORE sending so nothing is
silently truncated by Discord:
    embed <=6000 total | <=25 fields | field.value <=1024 | <=10 embeds/msg | content <=2000

Delivery seam (clean bot switch, zero code change):
  DAILY_HOTSPOTS_RELAY_CMD, JSON list / shell string; receives the message as the final argv item.
  else schedule-reminder's relay.py (SCHEDULE_RELAY_PY), `send --stream hotspots --text <msg>`.
  else the machine-local relay adapter at ~/.local/relay.py, same calling convention.
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
from digest import choose_card_links, score_text

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
    # score_text reads final_score OR the archived record's `score`, so re-pushing a card replayed
    # from the archive footers its real number instead of the literal "score None".
    footer = f"{isc} 独立源 · score {score_text(card)} ({card.get('grade')}) · {card.get('run_id','')}"
    # fields[:MAX_FIELDS] below is a HARD Discord limit, but a 25-dimension embed and a 30-dimension
    # embed look identical to the reader. Say N/M in the footer so a trimmed breakdown announces it.
    if len(fields) > MAX_FIELDS:
        footer += f" · 仅显示 {MAX_FIELDS}/{len(fields)} 个评分维度"
    return {
        "title": title[:256],
        # the SAME relevance-ranked chooser the digest and the pushed headline use, never
        # evidence[0] (which is ordered by collection source, so the embed used to link a card
        # titled after a shutdown announcement to the product's bare homepage).
        "url": choose_card_links(card).get("primary", ""),
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
        f"{tag} {card.get('title','?')}  ({card.get('grade')} {score_text(card)})",
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
    # If the base is absent, fall back to the machine-local relay adapter, which speaks the same
    # `send --stream <name> --text <msg>` convention. The old fallback pointed at
    # ~/.local/relay/send.py, a path that exists on no machine, so tier 3 could never deliver.
    rp = os.environ.get("SCHEDULE_RELAY_PY") or str(
        Path.home() / ".claude/skills/schedule-reminder/scripts/relay.py")
    if os.path.isfile(rp):
        return [sys.executable, rp, "send", "--stream", "hotspots", "--text"]
    return [sys.executable, str(Path.home() / ".local/relay.py"),
            "send", "--stream", "hotspots", "--text"]


def _notification_client():
    import importlib.util
    from pathlib import Path
    path = Path(os.environ.get('SCHEDULE_NOTIFICATION_CLIENT') or
                Path.home() / '.claude/skills/schedule-reminder/scripts/notification_client.py')
    if not path.is_file():
        raise RuntimeError('shared notification client missing; bind SCHEDULE_NOTIFICATION_CLIENT')
    spec = importlib.util.spec_from_file_location('_owner_notification_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deliver(message: str, dry_run: bool = False, *, run_id=None,
            phase='digest', condition='ready', retry_failed=False) -> tuple[bool, str]:
    """Owner-selected event; the shared producer alone owns delivery and retries."""
    message = rd.scrub_egress(message)
    if dry_run or os.environ.get('DAILY_HOTSPOTS_DRYRUN') or os.environ.get('AGENT_CENTER_RELAY_DRYRUN'):
        return (True, f'[dry-run] would deliver {len(message)} chars')
    try:
        client = _notification_client()
        receipt = client.submit('daily-hotspots', run_id, phase, condition, 'hotspots', message,
            language='preserve', retry_failed=retry_failed,
            **client.transport_options(_relay_cmd(), 'hotspots'))
        return receipt['state'] == 'sent', client.detail(receipt)
    except Exception as exc:
        return False, 'notification refused: ' + type(exc).__name__


def push_card(card: dict, update: bool = False, dry_run: bool = False) -> dict:
    embed = build_embed(card, update)
    errs = validate_embed(embed)
    text = render_text(card, update)
    identity = card.get('opportunity_id') or card.get('canonical_key') or card.get('id')
    if not identity and not (dry_run or os.environ.get('DAILY_HOTSPOTS_DRYRUN')):
        return {'ok': False, 'detail': 'stable card identity required', 'embed_errors': errs, 'embed': embed}
    ok, detail = deliver(text, dry_run=dry_run, run_id=card.get('run_id'), phase='card',
                         condition=('update:' if update else 'new:') + str(identity))
    return {"ok": ok, "detail": detail, "embed_errors": errs, "embed": embed}


def main() -> int:
    data = json.loads(sys.stdin.read() or "{}")
    dry = bool(os.environ.get("DAILY_HOTSPOTS_DRYRUN"))
    res = push_card(data, update=bool(data.get("_update")), dry_run=dry)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
