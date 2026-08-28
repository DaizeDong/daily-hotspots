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
from digest import choose_card_links

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
    # If the base is absent, fall back to the machine-local relay adapter, which speaks the same
    # `send --stream <name> --text <msg>` convention. The old fallback pointed at
    # ~/.local/relay/send.py, a path that exists on no machine, so tier 3 could never deliver.
    rp = os.environ.get("SCHEDULE_RELAY_PY") or str(
        Path.home() / ".claude/skills/schedule-reminder/scripts/relay.py")
    if os.path.isfile(rp):
        return [sys.executable, rp, "send", "--stream", "hotspots", "--text"]
    return [sys.executable, str(Path.home() / ".local/relay.py"),
            "send", "--stream", "hotspots", "--text"]


# Room reserved inside each chunk for the "第 i/n 段" marker below, so a chunk plus its marker is
# still <= CONTENT_MAX after the marker is appended.
_CHUNK_MARKER_RESERVE = 48


def _hard_cut_line(line: str, budget: int) -> tuple[str, int]:
    """Cut ONE oversized line (a single line cannot be split at a boundary) and SAY SO in the text.

    Returns (visible_text, dropped_chars). The note is inside the returned text, so the reader of
    the Discord message sees that this line was cut and by how much; the tail is never dropped in
    silence. Budget too small for even a note -> a bare marker, still never a silent cut.
    """
    if len(line) <= budget:
        return (line, 0)
    keep = budget
    for _ in range(4):          # fixed point: the note's own length depends on the dropped count
        note = f"…（本行超长，已截断 {len(line) - keep} 字）"
        new_keep = max(0, budget - len(note))
        if new_keep == keep:
            break
        keep = new_keep
    note = f"…（本行超长，已截断 {len(line) - keep} 字）"
    if keep <= 0 or len(line[:keep] + note) > budget:
        return (note[:budget], len(line))
    return (line[:keep] + note, len(line) - keep)


def split_for_discord(message: str, limit: int = CONTENT_MAX) -> tuple[list, int]:
    """Split an over-length message into <=limit chunks at MESSAGE BOUNDARIES.

    Boundary preference: blank-line separated blocks first, then single lines, and only a line that
    is itself longer than the budget is hard-cut (with a visible note, see _hard_cut_line). Pure and
    deterministic. Returns (chunks, hard_cut_chars): the caller reports both, so "it fit" and "it
    was split" and "a line had to be cut" are three visibly different outcomes.
    """
    limit = max(1, int(limit))
    if len(message) <= limit:
        return ([message], 0)
    budget = max(1, limit - _CHUNK_MARKER_RESERVE)

    # (separator_before, text): blocks rejoin with the blank line they were split on, lines inside
    # an over-long block rejoin with a single newline, so a chunk reads exactly like the original.
    nl, blank = chr(10), chr(10) * 2
    pieces, dropped = [], 0
    for bi, block in enumerate(message.split(blank)):
        if len(block) <= budget:
            pieces.append((blank if bi else "", block))
            continue
        for li, line in enumerate(block.split(nl)):
            sep = (blank if bi else "") if li == 0 else nl
            if len(line) <= budget:
                pieces.append((sep, line))
            else:
                cut, d = _hard_cut_line(line, budget)
                pieces.append((sep, cut))
                dropped += d

    chunks, cur = [], ""
    for sep, text in pieces:
        cand = text if not cur else cur + sep + text
        if len(cand) <= budget:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = text
    if cur:
        chunks.append(cur)
    return (chunks or [message[:budget]], dropped)


def deliver(message: str, dry_run: bool = False) -> tuple[bool, str]:
    """Send a text message, splitting it at a message boundary when it exceeds Discord's
    CONTENT_MAX. Logging stays length-only (never the message body).

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
    # DISCORD HARD LIMIT (content <= CONTENT_MAX). This used to be an empty `if` whose comment
    # promised a warning that was never emitted, so an over-length headline message was handed to
    # the relay as-is and whatever the relay did with the tail was invisible here. Now: warn loudly,
    # split at a message boundary, mark every chunk in the DELIVERED TEXT, and report the split (and
    # any hard cut) in the return detail. The tail is never dropped in silence.
    chunks, hard_cut = split_for_discord(message, CONTENT_MAX)
    if len(chunks) > 1 or hard_cut:
        print(f"[push_card] discord content limit: {len(message)} chars > {CONTENT_MAX}, "
              f"split into {len(chunks)} messages"
              + (f", hard cut {hard_cut} chars from over-long lines" if hard_cut else ""),
              file=sys.stderr)
        n = len(chunks)
        if n > 1:
            chunks = [c + f"\n（本条过长，已分段发送：第 {i}/{n} 段）" for i, c in enumerate(chunks, 1)]

    split_note = ""
    if len(chunks) > 1:
        split_note += f", 分 {len(chunks)} 段"
    if hard_cut:
        split_note += f", 硬截断 {hard_cut} 字"

    if dry_run or os.environ.get("DAILY_HOTSPOTS_DRYRUN"):
        return (True, f"[dry-run] would deliver {len(message)} chars{split_note}")

    base_cmd = _relay_cmd()
    rcs = []
    for chunk in chunks:
        try:
            proc = subprocess.run(base_cmd + [chunk], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
            rcs.append(proc.returncode)
        except Exception as e:
            return (False, f"deliver error: {e!r} (sent {len(rcs)}/{len(chunks)} 段)")
    bad = [rc for rc in rcs if rc != 0]
    rc = bad[0] if bad else 0
    return (not bad, f"rc={rc} ({len(message)} chars{split_note})")


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
