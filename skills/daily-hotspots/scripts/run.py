#!/usr/bin/env python3
"""Deterministic pipeline orchestrator, the gate that disposes what the LLM proposes.

INPUT (stdin or --in): a JSON list of *candidate clusters* the SKILL.md orchestration layer
already produced from the live MCP fan-out, each already cross-source de-duplicated into one
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
import importlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from lib import (canonical_key, extract_entities, iso, load_config, now_utc,
                 opportunity_id, parse_ts)
from classify import classify, check_excluded, keyword_hit
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


def _ev_origin(e: dict) -> str:
    return (e.get("origin") or e.get("source") or "").strip().lower()


def _quote_parent_origin(e: dict) -> str | None:
    """The origin an evidence item was QUOTE-DERIVED from, or None.

    A roster quote-signal (collect_roster) carries ``via_handle=<amplifying roster member>``: the item
    surfaces that member's amplification of a non-roster voice, not an independent collection of that
    voice. Its parent origin is the member's own ``x.com/<member>`` label, the form _handle_origin
    emits and that _distinct_origins lowercases, so the two align for the guard below."""
    vh = e.get("via_handle")
    if isinstance(vh, str) and vh.strip():
        return _handle_origin(vh)
    return None


def count_independent_sources(evidence: list[dict]) -> int:
    """Independent-source count for the >=2-ORIGIN red line, with two DETERMINISTIC anti-crowd guards.

    The naive count is "distinct origin labels", but two failure modes fake a crowd from non-independent
    material:

    1. TRANSLOAD (audit MEDIUM#2): a single wire story republished verbatim under several outlet labels
       (same exact URL listed N times) is NOT N independent sources. When every evidence item carries a
       URL, cap the independent count at the number of DISTINCT URLs: identical-URL republications
       collapse to one; genuinely distinct outlets each with their own write-up are unaffected.

    2. QUOTE-DERIVATIVE (audit HARDEN r4): a roster member QUOTING a non-roster voice emits TWO signals
       from a SINGLE pull, the member (``origin=x.com/<member>``) and the quoted voice
       (``origin=x.com/<quoted>``, ``via_handle=<member>``). A quote is by construction ABOUT the post
       it quotes, so if those two signals co-cluster into one candidate they carry two distinct origins
       and would clear the >=2-independent-origin red line, but the quoted voice is DERIVED from the
       member's amplification, not independent corroboration (the quoted signal's real job is the §8
       propose-add feed). So an origin whose ONLY appearances are quote-derivatives of ANOTHER origin
       PRESENT in the same evidence is not counted independently. Crucially, if that same origin ALSO
       appears as an independently-collected (non-quote) signal, e.g. the keyword search surfaced it
       too, it is NOT discounted and genuinely corroborates: the discount is precise, mirroring the
       transload guard.

    Scope (explicitly recorded, the SKILL/LLM normalization layer owns the rest): guard 1 catches
    exact-URL double-counting; guard 2 catches quote-derivatives that still carry ``via_handle`` after
    normalization. Semantic syndication (the SAME copy rehosted at DIFFERENT URLs) and a normalization
    pass that strips ``via_handle`` remain the LLM layer's job; the "deterministic gate disposes"
    guarantee does not extend to those.
    """
    origin_set = set(_distinct_origins(evidence))

    def _purely_quote_derived(e: dict, o: str) -> bool:
        p = _quote_parent_origin(e)                 # the origin e was quote-derived from, if any
        return p is not None and p != o and p in origin_set

    # QUOTE-DERIVATIVE discount: drop an origin whose EVERY appearance is a quote-derivative of a
    # co-present, different parent origin (a single roster pull's member+quoted pair). An origin with
    # even one independently-collected (non-quote) item survives and genuinely corroborates.
    for o in list(origin_set):
        items = [e for e in evidence if isinstance(e, dict) and _ev_origin(e) == o]
        if items and all(_purely_quote_derived(e, o) for e in items):
            origin_set.discard(o)
    n_origins = len(origin_set)
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
# deterministic remainder, filter, tag every evidence item with its origin
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


# A topic_filter term is split on WHITESPACE and query punctuation ONLY. The previous tokenizer,
# ``re.findall(r"[A-Za-z0-9_#]+", ...)``, split on every character outside that class, so a HYPHENATED
# term was shredded into its generic halves: ``open-source`` became ``open`` + ``source``,
# ``self-host`` became ``self`` + ``host``, ``no-code`` became ``no`` + ``code``. Any half then
# satisfied the OR on its own, so a filter written to TIGHTEN a noisy handle went straight back to
# keeping nearly everything, re-opening the very "filter keeps everything" bug the whole-word rewrite
# was written to close. A hyphen (or a CJK character, or an apostrophe) inside a term is PART OF THE
# TERM; only whitespace and boolean-query punctuation separate one term from the next.
_FILTER_SPLIT_RE = re.compile(r"""[\s()\[\]{}<>,;:|&+*"']+""")


def _topic_filter_terms(topic_filter) -> list[str]:
    """The non-operator terms of a twitter-style boolean filter, hyphenated terms kept WHOLE."""
    terms: list[str] = []
    for raw in _FILTER_SPLIT_RE.split(str(topic_filter).lower()):
        t = raw.strip("-.!?")
        if t and t not in _QUERY_OPS:
            terms.append(t)
    return terms


def _topic_filter_match(text: str, topic_filter) -> bool:
    """Deterministic stand-in for a twitter topic_filter query: keep a tweet when ANY non-operator
    term in the filter appears AS A WHOLE TERM (case-insensitive, token-boundary, NOT a bare
    substring) in the tweet text. A falsy filter, or one with no terms left after operators are
    removed, keeps everything. This approximates the boolean query twitterapi would run, enough to
    honor e.g. levelsio's ``(AI OR coding OR startup OR ship)`` in-core, deterministically.

    Whole-term matching is the whole point of the filter. A naive substring test let a short term
    match INSIDE unrelated words, ``ai`` in *email* / *brain* / *training*, ``ship`` in
    *relationship* / *shipping*, so a topic_filter meant to TIGHTEN a noisy handle kept almost
    everything and the section 8 suggest-filter remedy could never actually bite. The boundary rule
    is classify.keyword_hit, the SAME matcher the track / machine-type / community-lane keyword rules
    use, so the four can never drift apart: an ASCII edge needs a non-word neighbour, a CJK term
    matches as a substring (Chinese has no spaces)."""
    if not topic_filter:
        return True
    terms = _topic_filter_terms(topic_filter)
    if not terms:
        return True
    hay = (text or "").lower()
    return any(keyword_hit(hay, t) for t in terms)


def _handle_origin(handle: str) -> str:
    """Per-account origin label so two DIFFERENT roster handles count as two distinct origins in the
    >=2-origin gate, while the same handle's tweets collapse to one."""
    return "x.com/" + rt.normalize_handle(handle).lower()


# --------------------------------------------------------------------------- failed vs zero-yield
# A pull that FAILED and a pull that HONESTLY RETURNED NOTHING are different facts, and the pulls-log
# is the denominator the weekly auto-prune reads. Recording a failure as ``pulled=0, kept=0`` told
# yield.py "this handle was observed and produced nothing", which is how an unreachable source turns
# into a prune recommendation. So a failed unit gets an ERROR record instead: it carries ``error`` +
# ``observed: False``, travels in the same ``pulls`` list (the return shape is contract), and
# append_pulls routes it to the pull-errors ledger, NEVER to the denominator. yield.py globs
# ``pulls-*.jsonl`` and therefore never sees it, so a failure can no longer be read as a zero.
#
# NOTE on retry: run.py is the deterministic core and does NO network, so it cannot re-issue the
# failed call. What it CAN do, and now does, is make the failure a first-class, machine-readable
# record that the SKILL orchestration layer retries on (--sources reports sources_failed) instead of
# a silent zero nobody ever looks at.

def failed_pull(unit: dict, error: str, run_id: str, now) -> dict:
    """One failed-pull record: the unit that was attempted, why it failed, and NOT an observation."""
    rec = {"run_id": run_id, "ts": iso(now)}
    rec.update(unit)
    rec["error"] = str(error)[:300]
    rec["observed"] = False
    return rec


def is_failed_pull(rec) -> bool:
    return isinstance(rec, dict) and bool(rec.get("error")) and rec.get("observed") is False


def split_pulls(records) -> tuple[list, list]:
    """(observed denominator lines, failed-pull records). A non-dict record is neither, and is
    reported as a malformed record rather than silently dropped, by append_pulls_report."""
    observed, failed = [], []
    for r in records or []:
        if is_failed_pull(r):
            failed.append(r)
        elif isinstance(r, dict):
            observed.append(r)
    return observed, failed


_ROSTER_ERROR_FIELDS = ("error", "errors", "err")


def roster_payload_status(raw) -> tuple[list, str | None]:
    """Split ONE handle's raw ``get_user_last_tweets`` payload into ``(tweets, error)``.

    ``error is None`` means the call really was OBSERVED, and an empty ``tweets`` list then means a
    genuine zero-yield pull (that IS deadweight evidence and keeps its denominator line). Every other
    shape is a FAILURE, not a zero: a null/absent payload, a non-object payload, a payload carrying an
    error field, a payload with no ``tweets`` key at all, or a ``tweets`` that is not a list. The old
    code coerced all of them to ``[]`` and wrote a ``pulled=0`` denominator line."""
    if raw is None:
        return [], "no payload (null response)"
    if not isinstance(raw, dict):
        return [], f"malformed payload: expected an object, got {type(raw).__name__}"
    for f in _ROSTER_ERROR_FIELDS:
        v = raw.get(f)
        if v:
            detail = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            return [], f"{f}: {detail[:200]}"
    if "tweets" not in raw:
        return [], "malformed payload: no 'tweets' key"
    tw = raw.get("tweets")
    if not isinstance(tw, list):
        return [], f"malformed payload: 'tweets' is {type(tw).__name__}, not a list"
    return tw, None


def collect_roster(roster, responses: dict, cfg: dict | None = None, last_run=None,
                   run_id: str | None = None, now=None, tier: int = 1,
                   include_quoted: bool = True) -> dict:
    """Roster loop (§6): turn RAW twitterapi ``get_user_last_tweets`` responses into origin-tagged
    evidence signals + one pulls-log line per pulled handle.

    Args:
      roster, the loaded roster.json (roster.py shapes/validates it); plan_pulls picks the
                   enabled tier-1 handles and injects min_faves + topic_filter.
      responses, ``{handle: raw get_user_last_tweets JSON}`` the SKILL's MCP fan-out returned. A
                   handle present with a well-formed but EMPTY payload (``{"tweets": []}``) still
                   counts as a pull (one line, pulled=0) so a barren handle stays observable to
                   auto-prune (§8 deadweight). A handle present with a FAILED payload (null,
                   non-object, an error field, no/!list ``tweets``) is NOT an observation of zero
                   yield: it emits a failed_pull record instead (see roster_payload_status). A handle
                   ABSENT here was not attempted this run -> no record (honestly unobserved).

    Filtering (§6): ``createdAt >= last_run``, ``likeCount >= min_faves`` (the LOW
    ``min_faves_rostered`` floor, to catch PRE-VIRAL posts a min_faves:500 keyword search never
    sees), and the entry's ``topic_filter``. Each kept tweet becomes an evidence signal tagged
    ``origin_handle=H`` (identity carries the track, no keyword classify). A kept QUOTE of a
    non-roster author additionally surfaces THAT author as ``origin_handle`` (the §8 propose-add
    feed: a fresh voice a roster member amplified), it has no pulls-log line, so its yield stays
    UNKNOWN and it is prune-excluded, exactly as §9 requires. Returns ``{"signals", "pulls"}``."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or f"daily-{now.date().isoformat()}"
    lr = parse_ts(last_run) if isinstance(last_run, str) and last_run.strip() else last_run
    # A valid-JSON payload can still carry a NON-dict roster_responses (a list / str / number the MCP
    # fan-out mis-shaped): coerce to {} rather than crash on ``.items()``, a malformed sub-field must
    # degrade to "no observations", never abort the whole --sources pass (which would silently gap the
    # yield DENOMINATOR while everything else looks healthy).
    responses = responses if isinstance(responses, dict) else {}
    resp_by_handle = {(k or "").strip().lower(): v for k, v in responses.items()}

    signals: list[dict] = []
    pulls: list[dict] = []
    for task in rt.plan_pulls(roster, cfg, tier=tier):
        h = task["handle"]
        hk = h.lower()
        if hk not in resp_by_handle:
            continue  # not attempted this run -> unobserved; emitting no line is the honest record
        tweets, perr = roster_payload_status(resp_by_handle[hk])
        if perr is not None:
            # ATTEMPTED and FAILED. Not an observation of zero yield: it gets an error record, not a
            # pulled=0 denominator line, so auto-prune can never read an unreachable API as deadweight.
            pulls.append(failed_pull({"handle": h}, perr, run_id, now))
            continue
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
    "a malformed row yields nothing, never raises", so one bad epoch must degrade to an empty ts,
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
    V2EX MUST use direct WebFetch (brightdata returns empty), the fetch is the SKILL's, the parse
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
# guard), a hostile feed then degrades to [] exactly like any other parse error. Pure stdlib, no
# defusedxml dependency, and version-independent (the C-accelerated expat handler is not settable on
# every build). The prolog allows only the XML decl / PIs / comments before the (forbidden) DOCTYPE.
#
# A PI ends at its FIRST ``?>`` and may legitimately contain a bare ``>`` in between (XML spec: a PI's
# content is any char sequence not containing ``?>``). An earlier ``<\?[^>]*\?>`` could NOT consume
# such a PI, so a hostile prolog like ``<?xml?><?e a>b ?><!DOCTYPE ...>`` slipped past the whole regex
# (the ``*`` stopped at the un-matchable PI and ``<!DOCTYPE`` never anchored) and the DOCTYPE reached
# expat un-refused (audit HARDEN r3). The PI alternative therefore matches ``<\?.*?\?>``, minimal up
# to the first ``?>``, DOTALL so a multi-line PI is consumed, exactly the XML PI terminator rule; the
# comment alternative already handles ``>`` inside ``<!-- ... -->`` the same way.
_DOCTYPE_PROLOG_RE = re.compile(
    r"^\s*(?:<\?.*?\?>\s*|<!--.*?-->\s*)*<!DOCTYPE", re.IGNORECASE | re.DOTALL)


def _has_prolog_doctype(xml_text: str) -> bool:
    """True if a DOCTYPE appears in the XML prolog (before the root element), the only place expat
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


def _keepdrop_set(v) -> set:
    """Coerce a keep/drop config value to a lowercased string set, ROBUST to the most likely
    misconfig: a bare string where a list was meant (audit HARDEN r3).

    ``"keep_nodes": "geek"`` (vs the intended ``["geek"]``) must NOT be iterated character-by-character
, that made ``keep_set = {'g','e','k'}`` so the real node ``geek`` was never whitelisted and EVERY
    item was dropped, silently blinding the whole lane (the exact failure the design set out to fix). A
    lone string is wrapped as a single-element list; a genuinely non-iterable value (number / dict /
    None) degrades to the empty set, identical to an absent key (no whitelist / no drop), never a
    crash and never a char-shredded set. verify_config.validate_source_filters surfaces the bad type
    LOUDLY so the doctor never prints READY over a string-shredded lane."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple, set)):
        return set()
    return {str(x).lower() for x in v}


def _keyword_list(v) -> list[str]:
    """Coerce a keep_keywords / drop_keywords config value to a list of non-empty strings.

    Same robustness contract as _keepdrop_set (a bare string is ONE keyword, not a bag of
    characters), but keywords keep their order and are matched with classify.keyword_hit rather than
    compared for equality, because they are tested against free text, not against a category label."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple, set)):
        return []
    return [str(x) for x in v if str(x).strip()]


def community_payload_status(items) -> tuple[list, str | None]:
    """Split a community lane's payload into ``(items, error)``.

    Two accepted shapes. A bare LIST is the normal one: the already-normalized items from parse_v2ex
    / parse_rss. A DICT is the honest-failure envelope the fetch layer writes when a lane could not
    be reached, ``{"items": [...], "errors": [...]}`` (the exact shape the live linux.do fetcher
    emits on an all-attempts-failed day). A lane that reports errors is DOWN, not a zero-signal day,
    and must not be recorded as an observed zero-yield pull. Anything else is a malformed payload,
    which is also a failure, never a silent empty lane."""
    if isinstance(items, list):
        return items, None
    if items is None:
        return [], "no payload (null response)"
    if isinstance(items, dict):
        errs = items.get("errors") or items.get("error")
        if errs:
            detail = errs if isinstance(errs, str) else json.dumps(errs, ensure_ascii=False)
            status = items.get("lane_status")
            if status:
                detail = f"{status}: {detail}"
            return [], detail[:300]
        got = items.get("items")
        if isinstance(got, list):
            return got, None
        return [], "malformed payload: dict with no 'items' list and no error"
    return [], f"malformed payload: expected a list, got {type(items).__name__}"


def collect_community_source(source: str, items, cfg: dict | None = None, last_run=None,
                             run_id: str | None = None, now=None) -> dict:
    """Community lane (section 6): filter NORMALIZED items by the source's config and tag each with
    ``origin_source=<source>``; emit ONE pulls-log line for the source (one line per run).

    ``items`` are already normalized (parse_v2ex / parse_rss), or the failure envelope
    community_payload_status accepts. Filter config comes from watchlist.json ``sources[source]`` and
    is the ``keep_rule`` the config itself documents:

        KEEP if (category in keep_categories OR title/body matches a keep_keyword)
        AND NOT (category in drop_categories) AND NOT (title/body matches a drop_keyword)

    ``keep_nodes`` / ``drop_nodes`` are the v2ex spelling of ``keep_categories`` / ``drop_categories``.
    The KEYWORD half (``keep_keywords`` / ``drop_keywords``) was documented in watchlist.json, present
    in the LIVE config since 2026-07-15, and IMPLEMENTED NOWHERE: only the category lists ran. For
    linux.do that inverted the intended rule, because a keep_categories whitelist with no keyword
    escape hatch DROPPED every on-topic thread filed under any other category, and the whole
    drop_keywords mute list never ran at all. Keyword matching is classify.keyword_hit, the same
    token-boundary matcher the track rules use, so ``api`` cannot fire inside *rapid* while a CJK term
    still matches as a substring.

    An empty keep side (no keep_categories AND no keep_keywords) keeps everything not explicitly
    dropped. A keep/drop value written as a bare string (a plausible typo) is coerced to a
    single-element list by _keepdrop_set rather than shredded into characters. Track routing is
    keyword-classify downstream, collection only tags the origin (the yield numerator). Every item
    stays untrusted DATA (section 10).

    Returns ``{"signals", "pulls", "filtered"}``. ``filtered`` is the per-lane DROP LEDGER: every item
    that did not become a signal is counted under the rule that dropped it, so "the lane was clean"
    and "the lane dropped 40 items on keep_keywords" can never print the same thing. On a FAILED lane
    ``pulls`` carries a failed_pull record (error + observed False) instead of a denominator line."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or f"daily-{now.date().isoformat()}"
    lr = parse_ts(last_run) if isinstance(last_run, str) and last_run.strip() else last_run
    src_cfg = ((cfg.get("sources") or {}).get(source) or {})
    keep_cats = _keepdrop_set(src_cfg.get("keep_nodes") or src_cfg.get("keep_categories") or [])
    drop_cats = _keepdrop_set(src_cfg.get("drop_nodes") or src_cfg.get("drop_categories") or [])
    keep_kws = _keyword_list(src_cfg.get("keep_keywords"))
    drop_kws = _keyword_list(src_cfg.get("drop_keywords"))

    items, lane_error = community_payload_status(items)
    dropped = {"keep": 0, "drop_category": 0, "drop_keyword": 0, "stale": 0, "malformed": 0}

    signals: list[dict] = []
    for it in (items or []):
        if not isinstance(it, dict):
            dropped["malformed"] += 1
            continue
        cat = it.get("category")
        catl = str(cat).lower() if cat is not None else None
        hay = ((it.get("title") or "") + " \n " + (it.get("summary") or "")).lower()
        if keep_cats or keep_kws:
            # the keep side is an OR: a category-whitelist hit OR a keep_keyword hit admits the item.
            if not ((catl is not None and catl in keep_cats)
                    or any(keyword_hit(hay, k) for k in keep_kws)):
                dropped["keep"] += 1
                continue
        if catl is not None and catl in drop_cats:
            dropped["drop_category"] += 1
            continue          # explicit drop (life / jobs / promotions)
        if any(keyword_hit(hay, k) for k in drop_kws):
            dropped["drop_keyword"] += 1
            continue          # explicit content mute
        ts = it.get("ts") or ""
        if lr is not None and ts:
            try:
                if parse_ts(ts) < lr:
                    dropped["stale"] += 1
                    continue  # stale relative to last_run
            except Exception:
                pass          # unparseable ts -> keep (best-effort, don't over-drop)
        heat = it.get("heat")
        signals.append({
            "source": source,
            "origin": source,
            "origin_source": source,   # section 6 attribution: community numerator tag
            "url": it.get("url", ""),
            "signal": (f"{heat} replies \u00b7 {cat}" if heat is not None
                       else (str(cat) if cat else source)),
            "ts": ts,
            "title": it.get("title", ""),
            "text": it.get("summary", ""),
            "category": cat,
            "heat": heat,
        })

    if lane_error is not None:
        pulls = [failed_pull({"source": source}, lane_error, run_id, now)]
    else:
        pulls = [{"run_id": run_id, "ts": iso(now), "source": source,
                  "pulled": len(items or []), "kept": len(signals)}]
    filtered = {"source": source, "pulled": len(items or []), "kept": len(signals),
                "dropped_by_keep": dropped["keep"],
                "dropped_by_drop_category": dropped["drop_category"],
                "dropped_by_drop_keyword": dropped["drop_keyword"],
                "dropped_stale": dropped["stale"],
                "dropped_malformed": dropped["malformed"],
                "error": lane_error}
    return {"signals": signals, "pulls": pulls, "filtered": filtered}


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
    # community, like roster_responses, may arrive as a non-dict in a valid-JSON payload -> coerce to
    # {} so a mis-shaped sub-field degrades to "no community lanes", never crashes the denominator pass.
    community = community if isinstance(community, dict) else {}
    filtered: dict = {}
    for source, items in community.items():
        c = collect_community_source(source, items, cfg=cfg, last_run=last_run,
                                     run_id=run_id, now=now)
        signals += c["signals"]
        pulls += c["pulls"]
        filtered[source] = c["filtered"]
    return {"signals": signals, "pulls": pulls, "run_id": run_id, "filtered": filtered}


def pulls_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded pulls-log path: ``archive/pulls-YYYY-MM.jsonl`` (§5.1). One file per month keeps
    the append-only denominator ledger bounded; yield.load_pulls globs ``pulls-*.jsonl`` across
    months. Resolves via the same config-dir probe archive.py uses (or an explicit archive_dir)."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"pulls-{now.year:04d}-{now.month:02d}.jsonl"


def pull_errors_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded FAILED-pull ledger: ``archive/pull-errors-YYYY-MM.jsonl``.

    Deliberately a SEPARATE file from ``pulls-YYYY-MM.jsonl``: yield.load_pulls globs ``pulls-*.jsonl``
    and counts every line it finds as one observation of the origin, so a failure recorded in that
    file would be read as "we looked and there was nothing", which is exactly how an unreachable
    source becomes an auto-prune recommendation. The failure is still WRITTEN, in full, next to the
    denominator it is deliberately kept out of."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"pull-errors-{now.year:04d}-{now.month:02d}.jsonl"


def pull_identity(rec) -> tuple | None:
    """``(run_id, kind, unit)`` identity of one pulls-log record, or None when it names no unit.

    This is the IDEMPOTENCY key: one run pulls one handle (or one community source) once, so a
    second record for the same pair is a re-run of the same work, not new evidence."""
    if not isinstance(rec, dict):
        return None
    handle = rec.get("handle")
    unit = handle if handle else rec.get("source")
    if not unit or not str(unit).strip():
        return None
    return (str(rec.get("run_id") or ""), "handle" if handle else "source", str(unit).strip().lower())


def _ledger_identities(base, prefix: str) -> set:
    """Every pull identity already recorded in ``<base>/<prefix>*.jsonl``, across month shards.

    Reads ALL shards, not just the current month, because a re-run that crosses midnight on the last
    day of a month would otherwise write today's shard, not see yesterday's, and duplicate."""
    keys: set = set()
    if not base.is_dir():
        return keys
    for f in sorted(base.glob(prefix + "*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue          # an unparseable historical line cannot claim an identity
            k = pull_identity(rec)
            if k is not None:
                keys.add(k)
    return keys


def _append_jsonl(path, records) -> None:
    """Append records to a jsonl ledger. WRITER: no try/except, an IO failure propagates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_pulls_report(records, archive_dir: str | None = None, now=None,
                        dry_run: bool = False) -> dict:
    """Append the pulls-log (the yield DENOMINATOR, §5.1/§8) IDEMPOTENTLY, and report what it did.

    Two invariants, both of which the previous plain append broke:

    1. IDEMPOTENT per ``(run_id, handle|source)``. A re-run of the same day used to append a SECOND
       line for every handle, doubling the yield denominator while the deduped numerator (archived
       cards, counted once per opportunity_id) stayed flat, so every handle's measured yield silently
       halved and auto-prune read a healthy roster as deadweight. A record whose identity is already
       in the ledger is skipped and REPORTED, never written twice and never silently dropped.
    2. A FAILED pull never lands in the denominator. split_pulls routes it to the pull-errors ledger
       instead (see pull_errors_log_path), where it is still permanently recorded.

    ``dry_run`` writes nothing (mirrors archive/push/digest dry_run) so a preview/test run can never
    inflate the denominator. WRITER: hard-fails; an IO error propagates to the caller.

    Returns ``{"path", "written", "skipped_duplicate", "duplicates", "unkeyed", "unusable",
    "errors_path", "errors_written", "errors_skipped_duplicate", "dry_run"}``."""
    rep = {"path": None, "written": 0, "skipped_duplicate": 0, "duplicates": [],
           "unkeyed": 0, "unusable": 0, "errors_path": None, "errors_written": 0,
           "errors_skipped_duplicate": 0, "dry_run": bool(dry_run)}
    records = list(records or [])
    rep["unusable"] = sum(1 for r in records if not isinstance(r, dict))
    if dry_run or not records:
        return rep
    observed, failed = split_pulls(records)
    base = ar.resolve_archive_dir(archive_dir)

    def _dedup(batch, prefix):
        seen = _ledger_identities(base, prefix)
        keep, dupes, unkeyed = [], [], 0
        for rec in batch:
            k = pull_identity(rec)
            if k is None:
                unkeyed += 1     # cannot be deduped; kept, and counted so it is never invisible
                keep.append(rec)
                continue
            if k in seen:
                dupes.append({"run_id": k[0], k[1]: rec.get("handle") or rec.get("source")})
                continue
            seen.add(k)          # also collapses duplicates WITHIN one batch
            keep.append(rec)
        return keep, dupes, unkeyed

    if observed:
        keep, dupes, unkeyed = _dedup(observed, "pulls-")
        path = pulls_log_path(archive_dir, now)
        if keep:
            _append_jsonl(path, keep)
        rep["path"] = path
        rep["written"] = len(keep)
        rep["skipped_duplicate"] = len(dupes)
        rep["duplicates"] = dupes
        rep["unkeyed"] = unkeyed
    if failed:
        keep, dupes, _unkeyed = _dedup(failed, "pull-errors-")
        epath = pull_errors_log_path(archive_dir, now)
        if keep:
            _append_jsonl(epath, keep)
        rep["errors_path"] = epath
        rep["errors_written"] = len(keep)
        rep["errors_skipped_duplicate"] = len(dupes)
    return rep


def append_pulls(records, archive_dir: str | None = None, now=None, dry_run: bool = False):
    """Idempotent pulls-log append; returns the DENOMINATOR path, or None when nothing was
    denominator-bound (dry-run, empty input, or a batch of failed pulls only). The full accounting,
    including how many records were skipped as duplicates, is append_pulls_report."""
    return append_pulls_report(records, archive_dir, now, dry_run)["path"]


# --------------------------------------------------------------------------- collection ledger
# The two legs of a run are two separate processes: ``--sources`` collects and origin-tags the raw
# signals, then the SKILL clusters them and hands ``--in`` a much smaller list of candidates. Nothing
# carried the FIRST leg's count into the second, so process() had no idea how much signal it started
# from and the digest reported the funnel as if the candidate list were the whole input. On
# 2026-08-27 that hid a 625 -> 12 collapse behind an "everything is fine" coverage line (08-23
# 276 -> 12, 08-24 460 -> 18, 08-25 530 -> 29 are the same shape). This ledger is the seam: the
# --sources leg persists what it collected, keyed by run_id, and the --in leg reads it back and
# reports the difference as an explicit GAP.

def collection_log_path(archive_dir: str | None = None, now=None):
    """Month-sharded collection ledger: ``archive/collection-YYYY-MM.jsonl``. The name deliberately
    does NOT start with ``pulls-``, so yield.load_pulls cannot mistake it for a denominator line."""
    now = now or now_utc()
    return ar.resolve_archive_dir(archive_dir) / f"collection-{now.year:04d}-{now.month:02d}.jsonl"


def signal_key(item) -> str:
    """Stable identity of ONE collected signal or ONE candidate evidence item, so the two can be
    reconciled across the process boundary.

    The URL is the join key when present (normalized: lowercased, scheme and ``www.`` and a trailing
    slash removed), because that is the one field that survives the SKILL's clustering verbatim. With
    no URL we fall back to ``origin|ts|title[:80]``, which is weaker; a signal that carries neither a
    URL nor an origin has no identity at all and returns "", counted as collected but never
    reconcilable, which is itself an honest gap rather than a fake match."""
    if not isinstance(item, dict):
        return ""
    u = str(item.get("url") or "").strip().lower().rstrip("/")
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
    if u.startswith("www."):
        u = u[4:]
    if u:
        return u
    origin = str(item.get("origin") or item.get("origin_source") or item.get("origin_handle")
                 or item.get("source") or "").strip().lower()
    if not origin:
        return ""
    return f"{origin}|{str(item.get('ts') or '').strip()}|{str(item.get('title') or '')[:80]}"


def build_collection_record(out: dict, cfg: dict | None = None, now=None,
                            run_id: str | None = None) -> dict:
    """The per-run collection summary the --in leg reads back (see collection_log_path).

    ``sources_available`` = the lanes the CONFIG says should run (enabled entries under
    ``sources``). ``sources_invoked`` = the lanes that actually delivered a payload this run, derived
    from the pull records themselves (the roster counts as the single ``twitterapi`` lane, each
    community source as its own). ``sources_failed`` = one entry per unit that failed, a community
    lane by its source name and a roster handle by its ``x.com/<handle>`` origin label, so a
    half-failed roster is not rounded down to "twitterapi worked"."""
    cfg = cfg if cfg is not None else load_config()
    now = now or now_utc()
    run_id = run_id or out.get("run_id") or f"daily-{now.date().isoformat()}"
    signals = [s for s in (out.get("signals") or []) if isinstance(s, dict)]
    observed, failed = split_pulls(out.get("pulls"))

    lanes: set = set()
    for rec in observed + failed:
        lanes.add("twitterapi" if rec.get("handle") else str(rec.get("source") or "?"))
    declared = [n for n, sc in ((cfg.get("sources") or {}).items())
                if (sc or {}).get("enabled", True)]
    sources_failed = []
    for rec in failed:
        h = rec.get("handle")
        sources_failed.append({"source": _handle_origin(h) if h else str(rec.get("source") or "?"),
                               "error": rec.get("error", "")})
    return {
        "schema_version": 1,
        "run_id": run_id,
        "ts": iso(now),
        "signals_collected": len(signals),
        "signal_keys": [signal_key(s) for s in signals],
        "sources_invoked": len(lanes),
        "sources_available": len(declared),
        "sources_failed": sources_failed,
        "pulls_observed": len(observed),
        "filtered": out.get("filtered") or {},
    }


def append_collection(record: dict, archive_dir: str | None = None, now=None,
                      dry_run: bool = False):
    """Append ONE collection record. WRITER: no try/except, an IO failure propagates.

    Append-only with LAST-WINS on read (load_collection): a re-run of the same day supersedes its
    earlier record instead of rewriting history."""
    if dry_run or not record:
        return None
    path = collection_log_path(archive_dir, now)
    _append_jsonl(path, [record])
    return path


def load_collection(run_id: str, archive_dir: str | None = None) -> dict | None:
    """The collection record for ``run_id``, or None when no --sources leg recorded one.

    READER: degrades. None is NOT "zero signals collected"; build_coverage reports it as an
    UNMEASURED field, so a missing collection leg and a genuinely empty one never print the same."""
    base = ar.resolve_archive_dir(archive_dir)
    if not base.is_dir():
        return None
    found = None
    for f in sorted(base.glob("collection-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("run_id") == run_id:
                found = rec          # last one wins: a re-run supersedes
    return found


def _track_weight(track: str, cfg: dict) -> float:
    for t in cfg.get("tracks", []):
        if t.get("id") == track:
            return float(t.get("weight", 1.0))
    return 1.0


def effective_track_weight(track: str, cfg: dict, arms: dict | None = None,
                           seed: int = 0, memo: dict | None = None) -> float:
    """Track weight fed into scoring. Without bandit arms this is the STATIC config weight (R6
    wiring is opt-in, byte-identical default). With arms, the static weight is multiplicatively
    nudged by a deterministic Thompson draw centered at 1.0 (explore_weight in [lo,hi]=[0.5,1.5]
    by default): a well-performing track gets lifted, an under-performing one dampened, and
    score.py re-folds track_weight at HALF strength + clamps, so the bandit nudges ranking toward
    promising-but-under-sampled tracks without ever overriding the evidence-driven score.

    ``memo`` is a per-run {track: entry} cache the caller owns. The weight depends only on
    (track, cfg, arms, seed), all fixed for the whole run, so recomputing it once per CANDIDATE was
    pure waste: a linear scan of cfg["tracks"] plus, under the bandit, an md5 + Beta draw + two
    config reads. It is also the run's decision record: each entry keeps the static weight, the
    explore multiplier and the product, which is what process() reports back to the operator."""
    if memo is not None and track in memo:
        return memo[track]["effective"]
    static = _track_weight(track, cfg)
    if not arms:
        entry = {"static": static, "explore": None, "effective": static}
    else:
        import bandit as bdt
        ew = bdt.explore_weight(arms, track, int(seed), cfg)
        entry = {"static": static, "explore": ew, "effective": round(static * ew, 6)}
    if memo is not None:
        memo[track] = entry
    return entry["effective"]


def bandit_report(arms_before: dict | None, arms_after: dict | None, track_weights: dict,
                  seed: int, persist_state: str) -> dict:
    """The run's account of what the R6 bandit DID, one row per track.

    An adaptive component nobody can observe is worse than none: before this, the only trace a run
    left of the bandit was ``bandit_arms_next``, a posterior with no record of which draw produced
    it or how the ranking moved. Each row now carries the static ARCHITECTURE 8.3 weight, the
    Thompson multiplier drawn this run, their product (what scoring actually used), and the
    posterior before and after, so the lift is reconstructible and the reward that caused it is
    visible. ``scored`` separates a track this run ranked from one that only came back from the
    ledger, and ``persist_state`` separates "saved" from every reason nothing was written, so a
    silent no-op can never read as a clean save. A null ``explore_multiplier`` on a scored track is
    the cold start: there was no posterior yet, so the run ranked on the static weight and the
    reward it collected is what the NEXT run will draw against."""
    before, after = arms_before or {}, arms_after or {}

    def _f(arm, key, default=0.0):
        try:
            return round(float((arm or {}).get(key, default)), 6)
        except (TypeError, ValueError):
            return default

    rows = []
    for track in sorted(set(track_weights) | set(after) | set(before)):
        w = track_weights.get(track) or {}
        b, a = before.get(track), after.get(track)
        n_before, n_after = int((b or {}).get("n", 0)), int((a or {}).get("n", 0))
        rows.append({
            "track": track,
            "scored": track in track_weights,
            "static_weight": w.get("static"),
            "explore_multiplier": w.get("explore"),
            "effective_weight": w.get("effective"),
            "alpha_before": _f(b, "alpha", 1.0), "beta_before": _f(b, "beta", 1.0),
            "alpha_after": _f(a, "alpha", 1.0), "beta_after": _f(a, "beta", 1.0),
            "n_before": n_before, "n_after": n_after,
            "pulls_this_run": n_after - n_before,
            "reward_this_run": round(_f(a, "alpha", 1.0) - _f(b, "alpha", 1.0), 6),
        })
    return {"seed": int(seed), "tracks": rows, "persist_state": persist_state,
            "persisted": persist_state == "saved"}


def build_card(cand: dict, cfg: dict, run_id: str, arms: dict | None = None,
               seed: int = 0, weight_memo: dict | None = None) -> dict | None:
    title = cand.get("title", "")
    summary = cand.get("summary", "")
    body = summary + " " + " ".join(cand.get("entities", []))
    if not cand.get("track"):
        cls = classify(title, body, cfg)
    else:
        # A preset track (roster identity, §6) carries the TRACK, so classify() (track selection) is
        # skipped, but it is NEVER a license to bypass the exclude content gate. Run the SAME mute
        # check classify() would have run, so excluded content (memecoin / giveaway airdrop / crypto
        # pump / nsfw / mlm) can't slip through the X-roster lane just because it arrived with a track.
        reason = check_excluded(title, body, cfg)
        cls = {"track": cand["track"], "excluded": bool(reason), "exclude_reason": reason,
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

    _side = "demand" if str(cand.get("side", "supply")).strip().lower() == "demand" else "supply"
    sc = score_opportunity(
        cand.get("score_breakdown", {}),
        isc,
        float(cand.get("age_hours", 0.0)),
        cand.get("velocity"),
        effective_track_weight(track, cfg, arms, seed, weight_memo),
        cfg,
        lifecycle_stage=cand.get("lifecycle_stage"),  # R4: feed lifecycle downweight into live scoring
        side=_side,                                    # two-column model: demand vs supply scoring
        crowdedness=cand.get("crowdedness"),           # demand-only red-ocean penalty
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
        # two-column model: side routes the card to the demand/supply section; crowdedness + pain are
        # demand-side fields (pain_evidence is the concrete unmet-need quote that leads a demand card).
        "side": _side, "crowdedness": sc.get("crowdedness"), "pain_evidence": cand.get("pain_evidence", ""),
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
    scored (no final_score/grade/score_breakdown, a rumor is never dressed as an opportunity).

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


# --------------------------------------------------------------------------- coverage accounting
# Every number on the digest's coverage line is either MEASURED or listed in ``unmeasured``. There is
# no third state and no placeholder: the line used to print ``源 (see SKILL run)/(see SKILL run)``,
# which reads like a value and asserts nothing, and it reported ``候选 12`` on a day the collection
# layer had handed over 625 signals.

# The keys a caller may find missing, listed in ``coverage["unmeasured"]`` when they are. A field in
# that list was NOT observed this run; it is never quietly reported as 0.
COVERAGE_FIELDS = ("signals_collected", "signals_unaccounted", "sources_invoked",
                   "sources_available", "sources_failed", "below_floor")


def candidate_signal_keys(candidates) -> set:
    """Every signal identity reachable from the candidate clusters (their evidence items)."""
    keys: set = set()
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        for e in c.get("evidence") or []:
            k = signal_key(e)
            if k:
                keys.add(k)
    return keys


def build_coverage(candidates, cards, below_sources, community_pulse, suppressed, gate, pushed,
                   collection: dict | None) -> dict:
    """The run's funnel accounting, from what the collection layer reported down to what shipped.

    ``signals_unaccounted`` is the headline number: how many origin-tagged signals the --sources leg
    collected that no candidate cluster's evidence can be traced back to. It is a REPORTED GAP, not
    an error; the clustering layer legitimately merges and discards. What is NOT acceptable, and was
    the defect, is 95% of the collected signal evaporating with the run reporting zero dropped items.

    A missing collection record (no --sources leg ran, or it ran against a different archive dir)
    yields 0 for those fields AND names them in ``unmeasured``, so "nothing was collected" and
    "nobody measured what was collected" are different outputs."""
    unmeasured: list[str] = []
    if isinstance(collection, dict):
        collected_keys = [k for k in (collection.get("signal_keys") or [])]
        signals_collected = int(collection.get("signals_collected") or 0)
        cand_keys = candidate_signal_keys(candidates)
        accounted = sum(1 for k in collected_keys if k and k in cand_keys)
        # a record that carries a count but no keys can state the count and nothing more
        if signals_collected and not collected_keys:
            unmeasured.append("signals_unaccounted")
            signals_unaccounted = 0
        else:
            signals_unaccounted = max(0, signals_collected - accounted)
        sources_invoked = int(collection.get("sources_invoked") or 0)
        sources_available = int(collection.get("sources_available") or 0)
        sources_failed = [f for f in (collection.get("sources_failed") or []) if isinstance(f, dict)]
        if not sources_available and sources_invoked:
            # the config declared no source list, so "how many SHOULD have run" is unknown; the
            # invoked count is still real.
            unmeasured.append("sources_available")
    else:
        signals_collected = 0
        signals_unaccounted = 0
        sources_invoked = 0
        sources_available = 0
        sources_failed = []
        unmeasured += ["signals_collected", "signals_unaccounted", "sources_invoked",
                       "sources_available", "sources_failed"]

    gate = gate or {}
    if "below_floor" in gate:
        below_floor = list(gate.get("below_floor") or [])
    else:
        # verify_gate.gate_batch is expected to return the cards it dropped for missing their score
        # floor. An older gate that does not is reported as unmeasured, never as "none were dropped".
        below_floor = []
        unmeasured.append("below_floor")

    return {
        "signals_collected": signals_collected,
        "sources_invoked": sources_invoked,
        "sources_available": sources_available,
        "sources_failed": sources_failed,
        "candidates": len(candidates or []),
        "signals_unaccounted": signals_unaccounted,
        "below_sources": list(below_sources or []),
        "community_pulse": list(community_pulse or []),
        "suppressed": len(suppressed or []),
        "below_floor": below_floor,
        "pushed": len(pushed or []),
        "deepdived": sum(1 for c in (cards or []) if c.get("delegated_deepdive")),
        # kept for the existing renderer, the count form of community_pulse
        "pulse": len(community_pulse or []),
        "unmeasured": unmeasured,
    }


def process(candidates: list[dict], cfg: dict | None = None, ledger=None,
            dry_run: bool = False, run_id: str | None = None,
            archive_dir: str | None = None, bandit_arms: dict | None = None,
            bandit_seed: int = 0, persist_bandit: bool = False,
            collection: dict | None = None) -> dict:
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
    # A constant seed makes the draw a pure function of the posterior, so two runs whose posterior
    # did not move would explore in exactly the same direction forever. Derive it from run_id when
    # the caller pinned none: re-running the same run_id redraws identically (what the byte-compare
    # suite needs), while successive days sample different corners. Bandit mode only, so the static
    # path never sees it.
    if persist_bandit and not bandit_seed:
        import bandit as bdt
        bandit_seed = bdt.run_seed(run_id)

    # ---- build + distinct-ORIGIN red line + DUAL-TRACK SPLIT (§7) ----
    # Track 1 (>=2 origins) flows on to scoring/dedup/gate as an opportunity card, unchanged. A
    # single-origin candidate is NOT dropped: route_below_gate diverts a fresh, track-relevant,
    # community-sourced rumor to the community_pulse lane (Track 2), and everything else stays a
    # reported below_sources gap.
    cards, excluded, below_sources, community_pulse = [], [], [], []
    # Per-run track-weight cache AND the bandit's decision record: the weight is a function of
    # (track, cfg, arms, seed), all constant for the run, so it is computed once per TRACK instead
    # of once per candidate, and what it computed is what bandit_report() hands back to the operator.
    track_weights: dict = {}
    for cand in candidates:
        card = build_card(cand, cfg, run_id, arms=bandit_arms, seed=bandit_seed,
                          weight_memo=track_weights)
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
    # match_existing is a full scan of every ledger row (simhash + Jaccard + char n-grams) per card,
    # and the upsert loop below needs the SAME row again to carry first_seen/push_count forward. It
    # is pure in (candidate.canonical_key, title, summary) and none of those change between here and
    # there, so the second scan was a guaranteed-identical recomputation of the most expensive step
    # in process(). Keep the row instead. Identity-keyed, NOT stashed on the card: two cards can
    # share a canonical_key, and a ledger row hung on a card would ride along into every JSON the
    # card is serialized into.
    matched_rows: dict[int, dict] = {}
    for c in cards:
        matched = dd.match_existing(c, ledger_rows, cfg)
        if matched is not None:
            matched_rows[id(c)] = matched
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

    # ---- delivery model (2026-07): ONE consolidated 'headlines' digest per day, not a message per
    # card. The old per-card push (a Discord message per pushable card, each a raw multi-line block
    # with urls) was noisy and spawned link-embed cards. We now just MARK the pushable cards as shown
    # here and render them as a single ranked headline list at the digest-deliver step below; the full
    # cards + links stay in the archived digest file. No per-card network call.
    pushed = []
    for c in pushable:
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
    # Only ACTIONABLE cards (real gate outcomes) update an arm, suppressed/below-source/excluded
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
            matched = matched_rows.get(id(c))
            prior = dd._row_ext(matched) if matched else {}
            sample = {"ts": iso(now_utc()), "score": c.get("final_score"),
                      "n_sources": c.get("independent_source_count"),
                      "velocity": c.get("velocity"), "stage": c.get("lifecycle_stage", "")}
            ext = dd.build_ext(c, sample, prior, cfg)
            if c.get("pushed"):
                ext[dd.EXT_PREFIX + "push_count"] = int(c.get("push_count", 0))
            try:
                ledger.upsert(c, ext)
            except Exception as e:  # recorded, not swallowed, gates the watermark below
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

    # ---- coverage accounting (see build_coverage): the funnel from collected signal to shipped
    # card, with every unmeasured field named. ``collection`` is injectable for tests; by default it
    # is read back from the collection ledger the --sources leg wrote for this same run_id.
    coll = collection if collection is not None else load_collection(run_id, archive_dir)
    coverage = build_coverage(candidates, cards, below_sources, community_pulse, suppressed,
                              g, pushed, coll)

    # ---- digest (idempotent item + file + deliver) ----
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
            # Stamp ONLY the rumors the digest ACTUALLY rendered (the capped, deduped subset), NOT the
            # full pre-cap candidate list: the community_pulse.max_per_day cap DEFERS overflow rumors
            # to a later day (they re-rank next run), so marking an un-shown item "seen" would suppress
            # it forever without ever displaying it (§7 cap defers, never drops). select_rendered_pulse
            # mirrors the renderer's exact gate+dedup+cap using the same cfg + cross-day seen keys.
            if community_pulse and not errors:
                rendered_pulse = dg.select_rendered_pulse(community_pulse, cfg=cfg,
                                                          seen_keys=pulse_seen_keys)
                try:
                    ledger.set_pulse_seen(dg.merge_pulse_seen(pulse_seen_prior, rendered_pulse,
                                                              now_utc(), cfg))
                except Exception as e:
                    errors.append({"stage": "pulse_seen", "err": repr(e)[:200]})
    # Deliver ONLY the compact headlines: the top `max_per_day` (default 5) of ALL qualifying
    # (archivable) opportunities ranked by score, a consistent top-N briefing, not just the strict
    # immediate-push subset. The full `md` is written to the archive file above and (once committed by
    # the wrapper) linked as the 完整版 GitHub URL. We never push the raw markdown to the channel.
    digest_url = dg.digest_github_url(digest_path) if not dry_run else ""
    headlines = dg.build_headlines(archivable, coverage,
                                   cap=int((cfg.get("push", {}) or {}).get("max_per_day", 5)),
                                   digest_url=digest_url)
    pc.deliver(headlines, dry_run=dry_run)

    # ---- bandit posterior save (R6 loop close): persist the learned arms ONLY on a clean run, so
    # a partial failure does not bake in a half-learned posterior (same atomicity as the watermark).
    bandit_persist_state = "not-requested" if not persist_bandit else (
        "no-ledger" if ledger is None else "dry-run" if dry_run else
        "no-arms" if bandit_arms_next is None else "held-errors" if errors else "pending")
    if bandit_persist_state == "pending":
        try:
            ledger.set_bandit_arms(bandit_arms_next)
            bandit_persist_state = "saved"
        except Exception as e:
            bandit_persist_state = "failed"
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

    res = {
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
        "coverage": coverage,
        "digest_path": digest_path,
        "digest_markdown": md,
        "errors": errors,
        "watermark_advanced": watermark_advanced,
        "bandit_arms_next": bandit_arms_next,
    }
    # The bandit block exists ONLY when the bandit ran. Emitting an empty/None one on every run
    # would change the static path's output bytes for a feature that did nothing, and would make
    # "off" indistinguishable from "on but it decided nothing".
    if bandit_arms is not None:
        res["bandit"] = bandit_report(bandit_arms, bandit_arms_next, track_weights,
                                      bandit_seed, bandit_persist_state)
    return res


def _run_sources(a) -> int:
    """`--sources <file>`: the DENOMINATOR-writer entry point (spec §5.1/§6/§8), the piece that keeps
    the self-evolve yield engine from being inert.

    The SKILL's live MCP fan-out (twitterapi get_user_last_tweets over the roster + the community
    lanes) hands its RAW responses here as a JSON payload; run.py does the deterministic remainder ,
    origin-tag every signal (collect_sources) AND append one pulls-log line per pulled handle/source
    (append_pulls, the yield DENOMINATOR). Without this call on the daily path the pulls-log is never
    written, every handle's yield stays UNKNOWN forever, and auto-prune can never fire. Emits the
    origin-tagged signals for the SKILL to fold into its candidate clustering. ``--dry-run`` writes no
    pulls-log line (preview never inflates the denominator).

    Payload shape (all keys optional): ``{"roster_responses": {handle: raw}, "community": {source:
    [normalized items]}, "last_run": "...Z"}``."""
    cfg = load_config()
    raw = open(a.sources, encoding="utf-8").read() if a.sources != "-" else sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        # The payload comes from the LLM orchestration layer, which can wrap JSON in prose or emit a
        # trailing comma. Fail as a STRUCTURED error with rc=1 (the caller retries) instead of an
        # unhandled traceback; no pulls-log line is written this run (the denominator gaps, honestly).
        print(json.dumps({"error": "malformed sources payload", "detail": str(e)[:200],
                          "pulls_written": 0}, ensure_ascii=False))
        return 1
    if not isinstance(payload, dict):
        payload = {}
    roster = rt.load_roster(path=a.roster or None)
    out = collect_sources(roster=roster,
                          roster_responses=payload.get("roster_responses"),
                          community=payload.get("community"),
                          cfg=cfg, last_run=payload.get("last_run"),
                          run_id=a.run_id or None)
    path = append_pulls(out["pulls"], a.archive_dir or None, dry_run=a.dry_run)
    print(json.dumps({"run_id": out["run_id"], "signals": out["signals"],
                      "pulls_written": len(out["pulls"]),
                      "pulls_log": str(path) if path else None},
                     ensure_ascii=False))
    return 0


def _run_yield(a) -> int:
    """`--yield`: the weekly signal-yield pass entry point the spec §8 names ("runnable as run.py
    --yield or standalone"). Delegates to yield.py (imported by name, ``yield`` is a keyword) so the
    daily-radar CLI is the single documented surface for both the pipeline and its self-evolve loop."""
    Y = importlib.import_module("yield")
    yargs: list[str] = []
    if a.archive_dir:
        yargs += ["--archive-dir", a.archive_dir]
    if a.roster:
        yargs += ["--roster", a.roster]
    if a.apply:
        yargs.append("--apply")
    if a.write_review:
        yargs.append("--write-review")
    if a.user_info:
        yargs += ["--user-info", a.user_info]
    rc = Y.main(yargs)
    # §8/§4 weekly cadence: leave the idempotent per-ISO-week ledger item
    # (daily-hotspots:yield:<week>), the WEEKLY mirror of the daily digest item. Best-effort, a
    # missing/unreachable schedule-reminder base must never fail the deterministic replay, and
    # skipped under --no-ledger (offline / tests) so it never touches a live base implicitly.
    if rc == 0 and not a.no_ledger:
        try:
            led = dd.LedgerClient()
            led.init()
            Y.register_yield_item(led, summary="weekly yield pass")
        except Exception:
            pass
    return rc


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
    # Source-coverage self-evolve wiring (spec §5.1/§6/§8): make the pulls-log DENOMINATOR writer and
    # the weekly yield pass reachable from the daily-radar CLI (the wrapper/cron path), not just from
    # yield.py standalone, otherwise the engine is inert (audit HARDEN r4).
    ap.add_argument("--sources", default="",
                    help="write the pulls-log denominator + emit origin-tagged signals from raw "
                         "roster/community MCP responses (JSON file, or '-' for stdin); spec §5.1/§6/§8")
    ap.add_argument("--yield", dest="do_yield", action="store_true",
                    help="run the weekly signal-yield pass (spec §8/§9) instead of the candidate "
                         "pipeline (delegates to yield.py)")
    ap.add_argument("--apply", action="store_true",
                    help="(with --yield) apply the reversible auto-prune to roster.json")
    ap.add_argument("--write-review", action="store_true",
                    help="(with --yield) write archive/roster-review.md")
    ap.add_argument("--roster", default="",
                    help="explicit roster.json path (with --sources / --yield); default = config probe")
    ap.add_argument("--user-info", default="",
                    help="(with --yield) get_user_info sweep JSON {handle: info} -> identity flags (§9)")
    ap.add_argument("--bandit", action="store_true",
                    help="R6: rank with the exploration-adjusted (Thompson) track weight, learn from "
                         "this run's outcomes and persist the posterior to the ledger. OFF by "
                         "default; scoring.bandit.enabled=true in config turns it on permanently. "
                         "The run result then carries a 'bandit' block with every draw it made.")
    a = ap.parse_args()

    # Self-evolve entry points short-circuit BEFORE the candidate-stdin read + ledger init (they read
    # neither): the weekly yield replay and the per-run denominator writer are their own passes.
    if a.do_yield:
        return _run_yield(a)
    if a.sources:
        return _run_sources(a)

    candidates = []
    if not a.catch_up:  # catch-up backfills digests from the ledger; it reads no candidate input
        raw = open(a.infile, encoding="utf-8").read() if a.infile else sys.stdin.read()
        try:
            candidates = json.loads(raw or "[]")
        except json.JSONDecodeError as e:
            # The candidate JSON comes from the LLM orchestration layer (prose-wrapped / trailing-comma
            # output is a known flake). Fail LOUD but GRACEFULLY: a structured error + rc=1 so the day
            # is held for retry (process() never runs -> the watermark cannot advance past an uncovered
            # slot), instead of an unhandled JSONDecodeError traceback.
            print(json.dumps({"error": "malformed candidate JSON", "detail": str(e)[:200],
                              "watermark_advanced": False}, ensure_ascii=False))
            return 1
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
    # THE R6 entry point. Before this the bandit was unreachable: process() took persist_bandit but
    # no flag ever set it, so the learning loop had never turned once in production. Two ways in and
    # both are explicit: --bandit for one run, scoring.bandit.enabled for good. Neither on by
    # default, so the shipped static path is unchanged.
    import bandit as bdt
    persist_bandit = bool(a.bandit) or bdt.bandit_enabled(cfg)
    res = process(candidates, cfg, ledger, dry_run=a.dry_run,
                  run_id=a.run_id or None, archive_dir=a.archive_dir or None,
                  persist_bandit=persist_bandit)
    res.pop("digest_markdown", None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # EXIT CODE IS PART OF THE REPORT. process() does not raise on a side-effect failure, it records
    # the stage in ``errors`` and HOLDS the watermark so the day is retried. Returning 0 anyway made
    # that hold invisible to the only thing the cron wrapper actually reads: a day whose digest file
    # was never written, whose ledger upsert failed, or whose watermark never advanced exited exactly
    # like a day that shipped, so nothing ever retried and nobody was ever told. Any recorded error
    # is a nonzero exit; the full structured detail is already on stdout above.
    return 1 if res.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
