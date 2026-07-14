#!/usr/bin/env python3
"""Deterministic pipeline orchestrator — the gate that disposes what the LLM proposes.

INPUT (stdin or --in): a JSON list of *candidate clusters* the SKILL.md orchestration layer
already produced from the live MCP fan-out — each already cross-source de-duplicated into one
opportunity with its evidence[] and a temperature-0 per-dimension score_breakdown proposal:

  {
    "title","summary","entities":[...],
    "evidence":[{"source","origin","url","signal","ts"}, ...],   # >=1 raw; distinct ORIGIN gated here
    "score_breakdown":{track_fit,timing,feasibility,competition,executability},  # 0-100 each
    "age_hours": <float>, "velocity": <float|null>, "lifecycle_stage": "...",
    "why_now","contrarian_insight","action",
    "track": <optional; classify fills if absent>
  }

This module runs the DETERMINISTIC remainder: classify → canonical_key → distinct-ORIGIN gate
(>=2) → score → cross-day dedup (NEW/SUPPRESS/RESURFACE) → verify gate → tiered push → archive →
idempotent digest → atomic watermark. No network here except the relay/ledger subprocess seams,
both injectable + dry-runnable. Returns a structured result for the SKILL to report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from lib import (canonical_key, extract_entities, iso, load_config, now_utc,
                 opportunity_id, parse_ts)
from classify import classify
from score import score_opportunity
import dedup as dd
from verify_gate import gate_batch, route_below_gate, COMMUNITY_PULSE
from lib import community_source_set
import push_card as pc
import archive as ar
import digest as dg
import roster as rt


def _distinct_origins(evidence: list[dict]) -> list[str]:
    return sorted(set((e.get("origin") or e.get("source") or "").lower()
                      for e in evidence if (e.get("origin") or e.get("source"))))


def count_independent_sources(evidence: list[dict]) -> int:
    """Independent-source count for the >=2-ORIGIN red line, with a DETERMINISTIC transload guard.

    The naive count is "distinct origin labels", but a single wire story republished verbatim under
    several outlet labels (same exact URL listed N times) is NOT N independent sources — it is one
    syndicated item dressed up as a crowd (audit MEDIUM#2). So when every evidence item carries a
    URL, we cap the independent count at the number of DISTINCT URLs: identical-URL republications
    collapse to one, while genuinely distinct outlets each with their own write-up are unaffected.

    Scope of this deterministic guard (explicitly recorded — the SKILL/LLM normalization layer owns
    the rest): it catches exact-URL double-counting only. Semantic syndication (the SAME agency copy
    rehosted at DIFFERENT URLs) is NOT detected here and remains the LLM normalization layer's job;
    the "deterministic gate disposes" guarantee does not extend to that case.
    """
    origins = _distinct_origins(evidence)
    n_origins = len(origins)
    urls = [(e.get("url") or "").strip().lower() for e in evidence]
    if urls and all(urls):  # only cap when every item is URL-attributed
        return min(n_origins, len(set(urls)))
    return n_origins


# ============================================================================
# Source collection (source-coverage design §6): the roster loop + community
# lanes that feed the pipeline ALONGSIDE the existing broad keyword search.
#
# run.py stays the deterministic core: the LIVE MCP fan-out (twitterapi
# get_user_last_tweets, brightdata/webfetch) runs in the SKILL orchestration
# layer, which hands the RAW responses here. These functions do the
# deterministic remainder — filter, tag every evidence item with its origin
# (origin_handle for an X account, origin_source for a community lane, §6), and
# emit the per-run per-handle/source pulled-count line for the pulls-log (the
# yield DENOMINATOR, §5.1/§8). PURE (clock only via the `now` seam, no network);
# the single I/O edge is append_pulls().
#
# The broad keyword search (twitterapi search_tweets, collect.md) is UNCHANGED
# and additive: its candidate clusters still arrive via process()'s stdin/--in
# input. The roster is a COMPLEMENT for open discovery, never a replacement.
# ============================================================================

_TWITTER_TS_FMT = "%a %b %d %H:%M:%S %z %Y"   # e.g. "Thu Jun 25 08:30:00 +0000 2026"
_FAVE_FIELDS = ("likeCount", "favoriteCount", "like_count", "faves", "likes")
_QUERY_OPS = {"and", "or", "not"}             # twitter-query operators, not terms to match


def _parse_created_at(s) -> datetime | None:
    """Parse a tweet createdAt to an aware UTC datetime (twitter format first, ISO fallback)."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip(), _TWITTER_TS_FMT).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        return parse_ts(s)
    except Exception:
        return None


def _tweet_faves(tw: dict) -> float:
    for k in _FAVE_FIELDS:
        v = tw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def _topic_filter_match(text: str, topic_filter) -> bool:
    """Deterministic stand-in for a twitter topic_filter query: keep a tweet when ANY non-operator
    term in the filter appears AS A WHOLE WORD (case-insensitive, word-boundary — NOT a bare
    substring) in the tweet text. A falsy filter keeps everything. This approximates the boolean
    query twitterapi would run — enough to honor e.g. levelsio's ``(AI OR coding OR startup OR
    ship)`` in-core, deterministically.

    Whole-word matching is the whole point of the filter. A naive substring test let a short term
    match INSIDE unrelated words — ``ai`` in *email* / *brain* / *training*, ``ship`` in
    *relationship* / *shipping* — so a topic_filter meant to TIGHTEN a noisy handle kept almost
    everything and the §8 suggest-filter remedy could never actually bite. A "word" here is a run of
    [A-Za-z0-9_]; a hashtag term keeps its ``#`` where the filter wrote one."""
    if not topic_filter:
        return True
    terms = [t for t in re.findall(r"[A-Za-z0-9_#]+", str(topic_filter).lower())
             if t not in _QUERY_OPS]
    if not terms:
        return True
    hay = (text or "").lower()
    return any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", hay)
               for t in terms)


def _handle_origin(handle: str) -> str:
    """Per-account origin label so two DIFFERENT roster handles count as two distinct origins in the
    >=2-origin gate, while the same handle's tweets collapse to one."""
    return "x.com/" + rt.normalize_handle(handle).lower()


def collect_roster(roster, responses: dict, cfg: dict | None = None, last_run=None,
                   run_id: str | None = None, now=None, tier: int = 1,
                   include_quoted: bool = True) -> dict:
    """Roster loop (§6): turn RAW twitterapi ``get_user_last_tweets`` responses into origin-tagged
    evidence signals + one pulls-log line per pulled handle.

    Args:
      roster     — the loaded roster.json (roster.py shapes/validates it); plan_pulls picks the
                   enabled tier-1 handles and injects min_faves + topic_filter.
      responses  — ``{handle: raw get_user_last_tweets JSON}`` the SKILL's MCP fan-out returned. A
                   handle present with an empty/failed payload STILL counts as a pull (one line,
                   pulled=0) so a barren handle stays observable to auto-prune (§8 deadweight). A
                   handle ABSENT here was not attempted this run -> no line (honestly unobserved).

    Filtering (§6): ``createdAt >= last_run``, ``likeCount >= min_faves`` (the LOW
    ``min_faves_rostered`` floor, to catch PRE-VIRAL posts a min_faves:500 keyword search never
    sees), and the entry's ``topic_filter``. Each kept tweet becomes an evidence signal tagged
    ``origin_handle=H`` (identity carries the track — no keyword classify). A kept QUOTE of a
    non-roster author additionally surfaces THAT author as ``origin_handle`` (the §8 propose-add
    feed: a fresh voice a roster member amplified) — it has no pulls-log line, so its yield stays
    UNKNOWN and it is prune-excluded, exactly as §9 requires. Returns ``{"signals", "pulls"}``."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or f"daily-{now.date().isoformat()}"
    lr = parse_ts(last_run) if isinstance(last_run, str) and last_run.strip() else last_run
    resp_by_handle = {(k or "").strip().lower(): v for k, v in (responses or {}).items()}

    signals: list[dict] = []
    pulls: list[dict] = []
    for task in rt.plan_pulls(roster, cfg, tier=tier):
        h = task["handle"]
        hk = h.lower()
        if hk not in resp_by_handle:
            continue  # not attempted this run -> unobserved; emitting no line is the honest record
        raw = resp_by_handle[hk] or {}
        tweets = raw.get("tweets") if isinstance(raw, dict) else None
        tweets = tweets if isinstance(tweets, list) else []
        min_faves = float(task.get("min_faves") or 0)
        tf = task.get("topic_filter")
        kept = 0
        for tw in tweets:
            if not isinstance(tw, dict):
                continue
            created = _parse_created_at(tw.get("createdAt"))
            if lr is not None and created is not None and created < lr:
                continue  # stale: before last_run (§6 createdAt >= last_run)
            if _tweet_faves(tw) < min_faves:
                continue  # below the (low) rostered faves floor
            if not _topic_filter_match(tw.get("text", ""), tf):
                continue  # honor the entry's topic_filter
            signals.append({
                "source": "twitterapi",
                "origin": _handle_origin(h),
                "origin_handle": h,          # §6 attribution: the yield numerator's tag
                "track": task.get("track"),  # identity carries the track (no keyword classify)
                "url": tw.get("url", ""),
                "signal": f"{int(_tweet_faves(tw))} faves",
                "ts": iso(created) if created else "",
                "title": (tw.get("text") or "")[:120],
                "text": tw.get("text", ""),
                "faves": int(_tweet_faves(tw)),
            })
            kept += 1
            # §8 propose-add feed: a roster member QUOTING a non-roster voice surfaces that voice.
            if include_quoted and tw.get("isQuote") and isinstance(tw.get("quoted_tweet"), dict):
                q = tw["quoted_tweet"]
                qh_raw = ((q.get("author") or {}).get("userName") or "").strip()
                qh = rt.normalize_handle(qh_raw)
                if qh and rt._HANDLE_RE.match(qh) and qh.lower() != hk \
                        and rt.find_entry(roster, qh) is None:
                    qc = _parse_created_at(q.get("createdAt"))
                    signals.append({
                        "source": "twitterapi",
                        "origin": _handle_origin(qh),
                        "origin_handle": qh,   # a NON-roster handle -> a propose-add candidate (§8)
                        "via_handle": h,       # amplified BY this roster member
                        "url": q.get("url", ""),
                        "signal": f"quoted by {h}",
                        "ts": iso(qc) if qc else "",
                        "title": (q.get("text") or "")[:120],
                        "text": q.get("text", ""),
                        "faves": int(q.get("likeCount") or 0),
                    })
        pulls.append({"run_id": run_id, "ts": iso(now), "handle": h,
                      "pulled": len(tweets), "kept": kept})
    return {"signals": signals, "pulls": pulls}


def _epoch_to_iso(created) -> str:
    """Epoch seconds -> ISO-Z, tolerant of garbage / out-of-range values (NEVER raises).

    An untrusted V2EX row (the keyless ``/api/topics/*.json`` endpoint is spoofable / MITM-able) can
    carry a ``created`` that is non-finite or outside the platform's ``time_t`` range; then
    ``datetime.fromtimestamp`` raises OverflowError / OSError / ValueError. parse_v2ex's contract is
    "a malformed row yields nothing, never raises" — so one bad epoch must degrade to an empty ts,
    not take down the whole V2EX lane (every legit topic in the same payload would otherwise be
    lost, unlike sibling parse_rss which degrades to [])."""
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return ""
    try:
        return iso(datetime.fromtimestamp(created, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return ""


def parse_v2ex(raw) -> list[dict]:
    """Parse a v2ex ``/api/topics/*.json`` array into normalized community items (parse-only, §6).

    Keeps the node name as the routing ``category``, reply count as ``heat``, and the epoch
    ``created`` as an ISO ``ts``. Tolerant: a non-list or malformed row yields nothing, never raises.
    V2EX MUST use direct WebFetch (brightdata returns empty) — the fetch is the SKILL's, the parse
    is here."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, dict):
            continue
        node = t.get("node") if isinstance(t.get("node"), dict) else {}
        ts = _epoch_to_iso(t.get("created"))
        out.append({
            "title": t.get("title", ""),
            "url": t.get("url", ""),
            "category": node.get("name"),
            "heat": t.get("replies"),
            "ts": ts,
            "summary": t.get("content", ""),
        })
    return out


# A DTD/DOCTYPE is the entry point for XML entity-expansion ("billion laughs") and external-entity
# (XXE) attacks; stdlib ElementTree/expat expands internal entities, and an interpreter built against
# expat < 2.4.0 has NO amplification cap at all (full memory-exhaustion DoS). A legitimate RSS/Atom
# feed never carries a DOCTYPE, so we refuse any document whose prolog declares one (§10 injection
# guard) — a hostile feed then degrades to [] exactly like any other parse error. Pure stdlib, no
# defusedxml dependency, and version-independent (the C-accelerated expat handler is not settable on
# every build). The prolog allows only the XML decl / PIs / comments before the (forbidden) DOCTYPE.
_DOCTYPE_PROLOG_RE = re.compile(
    r"^\s*(?:<\?[^>]*\?>\s*|<!--.*?-->\s*)*<!DOCTYPE", re.IGNORECASE | re.DOTALL)


def _has_prolog_doctype(xml_text: str) -> bool:
    """True if a DOCTYPE appears in the XML prolog (before the root element) — the only place expat
    will act on it, and the only place a hostile feed would hide an entity bomb. A leading BOM is
    stripped first so ``<BOM><!DOCTYPE ...`` cannot slip past (``\\s`` does not match U+FEFF)."""
    return bool(_DOCTYPE_PROLOG_RE.match(xml_text.lstrip("\ufeff")))


def parse_rss(xml_text) -> list[dict]:
    """Parse an RSS feed (linux.do ``/latest.rss``, qbitai ``/feed``, ...) into normalized items.

    The structured surface is injection-safe (§10): ``<title>/<link>/<category>/<pubDate>/
    <description>`` are read as DATA, never executed. A prolog DOCTYPE is refused up front (no
    entity-expansion / XXE surface); stdlib xml only; a parse error yields ``[]`` rather than raising
    (a broken or hostile feed degrades to no items, not a crash)."""
    out: list[dict] = []
    if not isinstance(xml_text, str) or not xml_text.strip():
        return out
    if _has_prolog_doctype(xml_text):
        return out          # refuse DTDs (billion-laughs / XXE); no legitimate feed declares one
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    def _text(item, tag):
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    for item in root.iter("item"):
        cat_el = item.find("category")
        cat = (cat_el.text or "").strip() if cat_el is not None and cat_el.text else None
        pub = _text(item, "pubDate")
        ts = ""
        if pub:
            try:
                ts = iso(parsedate_to_datetime(pub))
            except Exception:
                ts = ""
        out.append({
            "title": _text(item, "title"),
            "url": _text(item, "link"),
            "category": cat,
            "heat": None,
            "ts": ts,
            "summary": _text(item, "description"),
        })
    return out


def collect_community_source(source: str, items, cfg: dict | None = None, last_run=None,
                             run_id: str | None = None, now=None) -> dict:
    """Community lane (§6): filter NORMALIZED items by the source's node/category config and tag each
    with ``origin_source=<source>``; emit ONE pulls-log line for the source (§5.1, one line per run).

    ``items`` are already normalized (parse_v2ex / parse_rss). keep/drop lists come from
    watchlist.json ``sources[source]`` (``keep_nodes``|``keep_categories`` /
    ``drop_nodes``|``drop_categories``). An empty keep-list keeps everything not explicitly dropped.
    Track routing is keyword-classify downstream — collection only tags the origin (the yield
    numerator). Every item stays untrusted DATA (§10)."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or f"daily-{now.date().isoformat()}"
    lr = parse_ts(last_run) if isinstance(last_run, str) and last_run.strip() else last_run
    src_cfg = ((cfg.get("sources") or {}).get(source) or {})
    keep = src_cfg.get("keep_nodes") or src_cfg.get("keep_categories") or []
    drop = src_cfg.get("drop_nodes") or src_cfg.get("drop_categories") or []
    keep_set = {str(x).lower() for x in keep}
    drop_set = {str(x).lower() for x in drop}

    signals: list[dict] = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        cat = it.get("category")
        catl = str(cat).lower() if cat is not None else None
        if keep_set and (catl is None or catl not in keep_set):
            continue          # a keep-list is a whitelist: unknown/absent category is dropped
        if catl is not None and catl in drop_set:
            continue          # explicit drop (life / jobs / promotions)
        ts = it.get("ts") or ""
        if lr is not None and ts:
            try:
                if parse_ts(ts) < lr:
                    continue  # stale relative to last_run
            except Exception:
                pass          # unparseable ts -> keep (best-effort, don't over-drop)
        heat = it.get("heat")
        signals.append({
            "source": source,
            "origin": source,
            "origin_source": source,   # §6 attribution: community numerator tag
            "url": it.get("url", ""),
            "signal": (f"{heat} replies · {cat}" if heat is not None
                       else (str(cat) if cat else source)),
            "ts": ts,
            "title": it.get("title", ""),
            "text": it.get("summary", ""),
            "category": cat,
            "heat": heat,
        })
    pulls = [{"run_id": run_id, "ts": iso(now), "source": source,
              "pulled": len(items or []), "kept": len(signals)}]
    return {"signals": signals, "pulls": pulls}


def collect_sources(roster=None, roster_responses: dict | None = None,
                    community: dict | None = None, cfg: dict | None = None, last_run=None,
                    run_id: str | None = None, now=None) -> dict:
    """Run the roster loop + every community lane, returning the combined origin-tagged signals and
    the full pulls-log batch (the yield denominator). Additive to the broad keyword search (kept in
    the SKILL layer; its candidate clusters still arrive via process()). ``community`` maps a source
    name to its NORMALIZED items, e.g. ``{"v2ex": parse_v2ex(raw), "linux.do": parse_rss(xml)}``."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or f"daily-{now.date().isoformat()}"
    signals: list[dict] = []
    pulls: list[dict] = []
    if roster is not None and roster_responses is not None:
        r = collect_roster(roster, roster_responses, cfg=cfg, last_run=last_run,
                            run_id=run_id, now=now)
        signals += r["signals"]
        pulls += r["pulls"]
    for source, items in (community or {}).items():
        c = collect_community_source(source, items, cfg=cfg, last_run=last_run,
                                     run_id=run_id, now=now)
        signals += c["signals"]
        pulls += c["pulls"]
    return {"signals": signals, "pulls": pulls, "run_id": run_id}


def pulls_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded pulls-log path: ``archive/pulls-YYYY-MM.jsonl`` (§5.1). One file per month keeps
    the append-only denominator ledger bounded; yield.load_pulls globs ``pulls-*.jsonl`` across
    months. Resolves via the same config-dir probe archive.py uses (or an explicit archive_dir)."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"pulls-{now.year:04d}-{now.month:02d}.jsonl"


def append_pulls(records, archive_dir: str | None = None, now=None, dry_run: bool = False):
    """Append pulls-log lines (the yield DENOMINATOR, §5.1/§8) — one line per (run, handle/source).

    ``dry_run`` writes nothing (mirrors archive/push/digest dry_run) so a preview/test run can never
    inflate the denominator. Returns the written path, or None on dry_run / empty input."""
    if dry_run or not records:
        return None
    p = pulls_log_path(archive_dir, now)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def _track_weight(track: str, cfg: dict) -> float:
    for t in cfg.get("tracks", []):
        if t.get("id") == track:
            return float(t.get("weight", 1.0))
    return 1.0


def effective_track_weight(track: str, cfg: dict, arms: dict | None = None,
                           seed: int = 0) -> float:
    """Track weight fed into scoring. Without bandit arms this is the STATIC config weight (R6
    wiring is opt-in, byte-identical default). With arms, the static weight is multiplicatively
    nudged by a deterministic Thompson draw centered at 1.0 (explore_weight in [lo,hi]=[0.5,1.5]
    by default): a well-performing track gets lifted, an under-performing one dampened — and
    score.py re-folds track_weight at HALF strength + clamps, so the bandit nudges ranking toward
    promising-but-under-sampled tracks without ever overriding the evidence-driven score."""
    static = _track_weight(track, cfg)
    if not arms:
        return static
    import bandit as bdt
    ew = bdt.explore_weight(arms, track, int(seed), cfg)
    return round(static * ew, 6)


def build_card(cand: dict, cfg: dict, run_id: str, arms: dict | None = None,
               seed: int = 0) -> dict | None:
    title = cand.get("title", "")
    summary = cand.get("summary", "")
    cls = classify(title, summary + " " + " ".join(cand.get("entities", [])), cfg) \
        if not cand.get("track") else {"track": cand["track"], "excluded": False,
                                       "track_matched": True,  # a preset track (roster identity) IS a hit
                                       "machine_type": cand.get("machine_type", ["tool-saas"]),
                                       "focus_tags": cand.get("focus_tags", [])}
    if cls.get("excluded"):
        return {"_excluded": True, "title": title, "reason": cls.get("exclude_reason")}

    track = cls["track"]
    entities = cand.get("entities") or extract_entities(title + " " + summary)
    ck = canonical_key(entities, track)
    evidence = cand.get("evidence", [])
    origins = _distinct_origins(evidence)
    isc = count_independent_sources(evidence)  # transload-aware (audit MEDIUM#2)

    sc = score_opportunity(
        cand.get("score_breakdown", {}),
        isc,
        float(cand.get("age_hours", 0.0)),
        cand.get("velocity"),
        effective_track_weight(track, cfg, arms, seed),
        cfg,
        lifecycle_stage=cand.get("lifecycle_stage"),  # R4: feed lifecycle downweight into live scoring
    )
    card = {
        "opportunity_id": opportunity_id(ck),
        "canonical_key": ck,
        "cluster_id": cand.get("cluster_id", f"cl-{now_utc().date().isoformat()}-{ck[:8]}"),
        "title": title, "summary": summary,
        "track": track, "machine_type": cls.get("machine_type", []),
        "focus_tags": cls.get("focus_tags", []),
        "evidence": evidence, "independent_source_count": isc,
        "source_set": [e.get("source") for e in evidence if e.get("source")],
        "score_breakdown": sc["score_breakdown"], "raw_score": sc["raw_score"],
        "confidence": sc["confidence"], "freshness": sc["freshness"],
        "final_score": sc["final_score"], "grade": sc["grade"],
        "why_now": cand.get("why_now", ""), "contrarian_insight": cand.get("contrarian_insight", ""),
        "action": cand.get("action", ""), "lifecycle_stage": cand.get("lifecycle_stage", ""),
        "velocity": cand.get("velocity"),
        "delegated_deepdive": cand.get("delegated_deepdive"),
        "run_id": run_id, "schema_version": 1,
        # dual-track routing inputs (§7): track_matched distinguishes a real keyword hit from the
        # classifier's default fallback; age_hours makes the freshness gate self-contained on the
        # card. Harmless to the score/verify/archive path (extra fields are ignored downstream).
        "track_matched": bool(cls.get("track_matched", True)),
        "age_hours": float(cand.get("age_hours", 0.0) or 0.0),
    }
    return card


def _pulse_item(card: dict, cfg: dict) -> dict:
    """Shape a single-origin community card into the lightweight pulse item the digest renderer
    (§7, digest.render_community_pulse) consumes: title + source + link + one-line why, and NOTHING
    scored (no final_score/grade/score_breakdown — a rumor is never dressed as an opportunity).

    Picks the representative COMMUNITY evidence item (the origin the pulse is attributed to), so the
    origin_source tag + url + ts + heat come straight from the collected signal (the yield numerator
    carries through)."""
    comm = community_source_set(cfg)
    ev = [e for e in (card.get("evidence") or []) if isinstance(e, dict)]
    pick = next((e for e in ev
                 if (str(e.get("origin_source") or e.get("source") or e.get("origin") or "")
                     .strip().lower()) in comm),
                (ev[0] if ev else {}))
    src = pick.get("origin_source") or pick.get("source") or pick.get("origin") or card.get("track")
    return {
        "title": card.get("title") or pick.get("title") or "",
        "url": pick.get("url") or "",
        "source": pick.get("source") or src,
        "origin": pick.get("origin") or src,
        "origin_source": pick.get("origin_source") or src,
        "signal": pick.get("signal") or "",
        "ts": pick.get("ts") or "",
        "heat": pick.get("heat"),
        "text": card.get("summary") or pick.get("text") or "",
        "track": card.get("track"),
    }


def process(candidates: list[dict], cfg: dict | None = None, ledger=None,
            dry_run: bool = False, run_id: str | None = None,
            archive_dir: str | None = None, bandit_arms: dict | None = None,
            bandit_seed: int = 0, persist_bandit: bool = False) -> dict:
    cfg = cfg or load_config()
    run_id = run_id or f"daily-{now_utc().date().isoformat()}"
    min_src = int(cfg["scoring"].get("min_independent_sources", 2))

    # ---- bandit posterior load (R6 loop close): in persist mode, when arms are not passed
    # explicitly, hydrate them from the ledger so the explore-exploit posterior carries across runs.
    # Default (persist_bandit=False, no arms) stays byte-identical to the static path. ----
    if persist_bandit and bandit_arms is None and ledger is not None:
        try:
            bandit_arms = ledger.get_bandit_arms()
        except Exception:
            bandit_arms = {}

    # ---- build + distinct-ORIGIN red line + DUAL-TRACK SPLIT (§7) ----
    # Track 1 (>=2 origins) flows on to scoring/dedup/gate as an opportunity card, unchanged. A
    # single-origin candidate is NOT dropped: route_below_gate diverts a fresh, track-relevant,
    # community-sourced rumor to the community_pulse lane (Track 2), and everything else stays a
    # reported below_sources gap.
    cards, excluded, below_sources, community_pulse = [], [], [], []
    for cand in candidates:
        card = build_card(cand, cfg, run_id, arms=bandit_arms, seed=bandit_seed)
        if card is None:
            continue
        if card.get("_excluded"):
            excluded.append(card)
            continue
        if card["independent_source_count"] < min_src:
            if route_below_gate(card, cfg) == COMMUNITY_PULSE:
                community_pulse.append(_pulse_item(card, cfg))
            else:
                below_sources.append({"title": card["title"],
                                      "isc": card["independent_source_count"]})
            continue
        cards.append(card)

    # ---- cross-day dedup against the base ledger ----
    ledger_rows = []
    if ledger is not None:
        try:
            ledger_rows = ledger.list_active()
        except Exception:
            ledger_rows = []
    new_cards, resurface_cards, suppressed = [], [], []
    for c in cards:
        matched = dd.match_existing(c, ledger_rows, cfg)
        d = dd.decide(c, matched, cfg)
        c["_branch"] = d["branch"]
        c["_dedup_delta"] = d["delta"]
        if matched is not None:
            c["first_seen"] = dd._row_ext(matched).get(dd.EXT_PREFIX + "first_seen")
            c["push_count"] = int(dd._row_ext(matched).get(dd.EXT_PREFIX + "push_count", 0))
        if d["branch"] == dd.SUPPRESS:
            suppressed.append(c)
        elif d["branch"] == dd.RESURFACE:
            resurface_cards.append(c)
        else:
            new_cards.append(c)

    actionable = new_cards + resurface_cards

    # ---- verify gate (fail-closed) + bucketing ----
    g = gate_batch(actionable, cfg)
    pushable = g["pushable"]
    archivable = g["archivable"]

    # ---- tiered push ----
    pushed = []
    for c in pushable:
        is_update = c.get("_branch") == dd.RESURFACE
        res = pc.push_card(c, update=is_update, dry_run=dry_run)
        if res["ok"]:
            c["pushed"] = True
            c["push_count"] = int(c.get("push_count", 0)) + 1
            c["push_ts"] = iso(now_utc())
            pushed.append(c)

    # ---- archive (quality-gated) ----
    # dry_run threads through: preview re-asserts the archive quality gate but writes nothing, so a
    # test/preview run with $DAILY_HOTSPOTS_CONFIG set can't leak fake cards into the real archive.
    archived = []
    for c in archivable:
        status, detail = ar.archive_card(c, archive_dir, cfg, dry_run=dry_run)
        if status in ("archived", "would-archive"):
            c["archived"] = True
            archived.append(c["title"])

    # ---- bandit reward feedback (R6 run.py wiring): close the explore-exploit loop. Each track's
    # Beta-Bernoulli arm learns from this run's REALIZED outcome (pushed > archived > blocked/score),
    # so a track that keeps producing pushable opportunities earns more lift next run and a cold one
    # decays. PURE: the input arms are never mutated; we emit the NEXT arms for the orchestration
    # layer to persist (ledger persistence kept out of this deterministic core, like catch_up_digests).
    # Only ACTIONABLE cards (real gate outcomes) update an arm — suppressed/below-source/excluded
    # candidates never had an outcome and must not teach the bandit anything.
    bandit_arms_next = None
    if bandit_arms is not None:
        import bandit as bdt
        bandit_arms_next = {k: dict(v) for k, v in (bandit_arms or {}).items()}
        blocked_titles = {b.get("title") for b in g["blocked"]}
        for c in actionable:
            track = c.get("track")
            if not track:
                continue
            if c.get("title") in blocked_titles:
                c["blocked"] = True
            r = bdt.outcome_reward(c, cfg)
            arm = bandit_arms_next.get(track) or bdt.init_arm(cfg)
            bandit_arms_next[track] = bdt.update_arm(arm, r, cfg)

    # ---- side-effect error accumulator: the watermark only advances after EVERY ledger/digest
    # write on this run succeeded (SKILL Hard-rule #4 atomicity / audit MEDIUM#1). A swallowed
    # exception must NOT let the watermark move past a slot that was never actually covered, or the
    # next run would treat the failed item as "already done" and silently drop it.
    errors: list[dict] = []

    # ---- ledger upsert (NEW + RESURFACE + SUPPRESS get a sample; idempotent UPSERT) ----
    if ledger is not None and not dry_run:
        for c in actionable + suppressed:
            prior = {}
            matched = dd.match_existing(c, ledger_rows, cfg)
            if matched:
                prior = dd._row_ext(matched)
            sample = {"ts": iso(now_utc()), "score": c.get("final_score"),
                      "n_sources": c.get("independent_source_count"),
                      "velocity": c.get("velocity"), "stage": c.get("lifecycle_stage", "")}
            ext = dd.build_ext(c, sample, prior, cfg)
            if c.get("pushed"):
                ext[dd.EXT_PREFIX + "push_count"] = int(c.get("push_count", 0))
            try:
                ledger.upsert(c, ext)
            except Exception as e:  # recorded, not swallowed — gates the watermark below
                errors.append({"stage": "upsert", "key": c.get("canonical_key"), "err": repr(e)[:200]})

    # ---- cross-day community-pulse dedup (§7 "no rumor re-bubbles"): load the prior-shown rumor
    # keys (a bounded {pulse_key: last_shown_iso} singleton on the base ledger, mirroring the
    # watermark) so a single-source rumor rendered on an earlier day is SUPPRESSED in today's digest
    # rather than re-rendered as fresh every day until a 2nd origin escalates it to a scored card.
    # Read-only + defensive here (an absent/partial ledger degrades to no suppression), so it runs on
    # dry-run previews too; the write-back is gated on a clean, non-dry run below.
    pulse_seen_prior = {}
    if ledger is not None:
        try:
            pulse_seen_prior = ledger.get_pulse_seen() or {}
        except Exception:
            pulse_seen_prior = {}
    pulse_seen_keys = dg.active_pulse_seen_keys(pulse_seen_prior, now_utc(), cfg)

    # ---- digest (idempotent item + file + deliver) ----
    coverage = {"sources_invoked": "(see SKILL run)", "sources_available": "(see SKILL run)",
                "candidates": len(candidates), "pushed": len(pushed),
                "pulse": len(community_pulse),
                "deepdived": sum(1 for c in cards if c.get("delegated_deepdive"))}
    # Track 2 (§7): the community-pulse rumors render as their own section AFTER the cards (and even
    # on an otherwise-empty card day). build_markdown forwards seen_keys so cross-day-seen rumors
    # never re-bubble into the pushed digest.
    md = dg.build_markdown(archivable, coverage, pulse=community_pulse, cfg=cfg,
                           seen_keys=pulse_seen_keys)
    digest_path = None
    if not dry_run:
        try:
            digest_path = str(dg.write_digest_file(md, archive_dir))
        except Exception as e:
            digest_path = None
            errors.append({"stage": "digest_file", "err": repr(e)[:200]})
        if ledger is not None:
            try:
                dg.register_digest_item(ledger, summary=f"{len(archivable)} cards, {len(pushed)} pushed")
            except Exception as e:
                errors.append({"stage": "digest_item", "err": repr(e)[:200]})
            # persist THIS run's rumor keys so tomorrow suppresses them (mirrors the watermark/bandit
            # singleton). Only on a clean run (no prior side-effect error) so a partial failure never
            # bakes in a half-recorded dedup state; a failure here holds the watermark for retry.
            if community_pulse and not errors:
                try:
                    ledger.set_pulse_seen(dg.merge_pulse_seen(pulse_seen_prior, community_pulse,
                                                              now_utc(), cfg))
                except Exception as e:
                    errors.append({"stage": "pulse_seen", "err": repr(e)[:200]})
    pc.deliver(md if len(archivable) else md, dry_run=dry_run)

    # ---- bandit posterior save (R6 loop close): persist the learned arms ONLY on a clean run, so
    # a partial failure does not bake in a half-learned posterior (same atomicity as the watermark).
    if persist_bandit and ledger is not None and not dry_run and bandit_arms_next is not None:
        if not errors:
            try:
                ledger.set_bandit_arms(bandit_arms_next)
            except Exception as e:
                errors.append({"stage": "bandit_persist", "err": repr(e)[:200]})

    # ---- atomic watermark (advances ONLY when the full success path was clean) ----
    watermark_advanced = False
    if ledger is not None and not dry_run:
        if not errors:
            try:
                ledger.add_watermark(iso(now_utc()))
                watermark_advanced = True
            except Exception as e:
                errors.append({"stage": "watermark", "err": repr(e)[:200]})
        # else: a side-effect failed this run -> hold the watermark so the failed slot is retried.

    return {
        "run_id": run_id,
        "candidates": len(candidates),
        "built": len(cards),
        "excluded": len(excluded),
        "below_sources": below_sources,
        "community_pulse": community_pulse,
        "new": len(new_cards), "resurface": len(resurface_cards), "suppressed": len(suppressed),
        "blocked": g["blocked"],
        "pushed": [c["title"] for c in pushed],
        "archived": archived,
        "empty_day": len(archivable) == 0,
        "digest_path": digest_path,
        "digest_markdown": md,
        "errors": errors,
        "watermark_advanced": watermark_advanced,
        "bandit_arms_next": bandit_arms_next,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--archive-dir", default="")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--catch-up", action="store_true",
                    help="R5: backfill missed daily-digest items since the last watermark, then exit "
                         "(idempotent; for the cron/orchestration layer after an oversleep)")
    a = ap.parse_args()

    candidates = []
    if not a.catch_up:  # catch-up backfills digests from the ledger; it reads no candidate input
        raw = open(a.infile, encoding="utf-8").read() if a.infile else sys.stdin.read()
        candidates = json.loads(raw or "[]")
        if isinstance(candidates, dict):
            candidates = candidates.get("candidates", [])

    cfg = load_config()
    ledger = None if a.no_ledger else dd.LedgerClient()
    if ledger is not None:
        try:
            ledger.init()
        except Exception:
            ledger = None
    if a.catch_up:
        if ledger is None:
            print(json.dumps({"catch_up": [], "error": "no ledger (schedule-reminder base required)"}))
            return 1
        dates = dg.catch_up_digests(ledger, ledger.get_watermark())
        print(json.dumps({"catch_up": dates}, ensure_ascii=False))
        return 0
    res = process(candidates, cfg, ledger, dry_run=a.dry_run,
                  run_id=a.run_id or None, archive_dir=a.archive_dir or None)
    res.pop("digest_markdown", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
