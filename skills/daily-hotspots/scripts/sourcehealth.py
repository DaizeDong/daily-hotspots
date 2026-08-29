#!/usr/bin/env python3
"""Fail-open detector for the collection sources, plus the four other health states.

WHY THIS EXISTS. On 2026-08-27 the brightdata MCP returned, for `scrape_as_markdown` on
https://example.com, a completely empty content block, and for `search_engine` on "weather today",
`{"organic": [], "current_page": 1}`. Well formed. No error. Zero data. brightdata is the FIRST hop
of the retrieval fallback chain in reference/collect.md and the SOLE route for the linux.do lane,
which is 11% of the archived cards. Every downstream counter read "this source contributed nothing
today", which is byte for byte what a genuinely quiet source looks like. A tool that cannot fetch
example.com and still reports success is the worst failure shape there is, because it is invisible
by construction.

THE CORE IDEA. A source is only `ok` when a CONTROL QUERY WITH A KNOWN NON-EMPTY ANSWER comes back
non-empty. Probing with the REAL query cannot work: a real query legitimately returns nothing on a
slow day, so silence carries no information. A control whose answer MUST be non-empty converts that
silence into a signal. example.com always contains "Example Domain". A web search for a common term
always has at least one organic result. An App Store RSS call for a known app id always has entries.
If the control comes back empty, the transport is lying, not the world.

CONTROLS ARE DATA (see CONTROLS below). One control per source KIND, and any spec may declare its
own inline, so adding a source never means editing the classifier.

FIVE STATES, and the distinctions are the product:
    ok                   the control returned its expected non-empty answer
    degraded             it worked, but slowly, or needed a retry, or came back partial, or the
                         content assertion missed while content was still present
    down                 it raised, timed out, or returned a transport error
    fail_open_suspected  it reported SUCCESS with an empty or contentless payload where the control
                         GUARANTEES content. This is the one that matters.
    unknown              the probe itself could not run: no fetcher wired, no control declared, or a
                         control that asserts nothing. "We did not check" is its own state and this
                         whole module is an argument for that. It is never folded into ok, and never
                         folded into down either, because a lane nobody probed is not a dead lane.

A PROBE NEVER RAISES INTO THE CALLER. A failed probe is a RESULT. The CLI is where the alarm lives:
it exits 3 when anything is down or fail-open, so a scheduled check can page someone.

READER / WRITER. Probing is a read path and degrades into a result. `write_report` is a WRITE path
and hard-fails: no try/except, no in-repo default destination.

WIRING. `LIVE_FETCHERS` holds the keyless HTTP adapters this deterministic process can run by
itself, and `probe_all` reports every other lane as `unknown` rather than pretending. MCP-routed
lanes (brightdata, tavily, google-news-trends, and the brightdata-only linux.do lane) cannot be
called from here at all; the skill orchestration layer runs their control query and hands the RAW
response back through `--observations`, which lands in exactly the same classifier.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

from lib import iso, load_config, now_utc

try:  # BOM-safe stdout on Windows GBK consoles, same seam as lib.py
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------- states

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
FAIL_OPEN = "fail_open_suspected"
UNKNOWN = "unknown"

#: Every state a probe may report, in the order the summary counts them.
STATES = (OK, DEGRADED, DOWN, FAIL_OPEN, UNKNOWN)

#: The states that mean "somebody has to look at this now".
ALARM_STATES = (DOWN, FAIL_OPEN)

# CLI exit codes. 0 / 2 / 3 are the cross-group contract; 1 is the leftover case (something WAS
# checked, nothing is hard down, but it is not clean either) and exists so 0 can keep meaning
# exactly clean.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_UNCHECKED = 2
EXIT_ATTENTION = 3
# The probe could not even be SET UP: an unreadable specs or observations file. Its own code,
# because an uncaught traceback exits 1 on this interpreter, which would be indistinguishable from
# "partial" to a scheduled check. Nothing is printed on stdout in this case, so a caller can also
# tell by the absence of a report.
EXIT_USAGE = 4

# --------------------------------------------------------------------------- interstitials

# Trustpilot WITHOUT the Firecrawl stealth proxy returns, on roughly half of calls, a 153 to 170 byte
# body reading "Verifying your connection". It is HTTP 200. Every char counter downstream reads it as
# a page that happened to have nothing on it. It is a challenge page, so it is contentless success:
# fail_open_suspected, never ok. Length-bounded so a real page that merely quotes the phrase (a
# review complaining about a captcha wall, say) is not misjudged.
INTERSTITIAL_MARKERS = (
    "verifying your connection",
    "checking your browser",
    "just a moment...",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
)
INTERSTITIAL_MAX_CHARS = 2000

#: Above this many milliseconds an otherwise successful probe is `degraded`, not `ok`.
DEFAULT_SLOW_MS = 5000.0

_MISSING = object()

# --------------------------------------------------------------------------- controls as data

# A control is: a query, plus at least one assertion whose answer is known non-empty and stable.
#
#   id            stable label, echoed back as `control_used` so a report says WHAT was asserted
#   kind          the source kind this control serves
#   query         handed verbatim to the fetcher. Its shape is the fetcher's business, not ours.
#   expect        the assertion. At least one of:
#                   substring   case-insensitive, must appear in the payload's text
#                   min_chars   the payload's text must be at least this long
#                   items_path  dotted path to a list ("" or "." means the payload root)
#                   min_items   that list must hold at least this many entries (default 1)
#   slow_ms       above this latency the result is degraded instead of ok
#
# A control that asserts NOTHING is refused (state `unknown`), because a probe that cannot fail is
# not a probe. That refusal is this module's negative control on itself.
CONTROLS: dict = {
    "web_scrape": {
        "id": "web_scrape:example.com",
        "kind": "web_scrape",
        "query": {"url": "https://example.com"},
        "expect": {"substring": "Example Domain", "min_chars": 200},
        "slow_ms": 15000.0,
        "note": "brightdata scrape_as_markdown returned an EMPTY content block for this exact URL "
                "on 2026-08-27. If this control is empty the transport is dead, whatever it claims.",
    },
    "web_search": {
        "id": "web_search:weather today",
        "kind": "web_search",
        "query": {"q": "weather today", "count": 5},
        "expect": {"items_path": "organic", "min_items": 1},
        "slow_ms": 15000.0,
        "note": "brightdata search_engine returned {\"organic\": [], \"current_page\": 1} for this "
                "exact query on 2026-08-27: well formed, zero data, reported as success.",
    },
    "appstore_rss": {
        "id": "appstore_rss:324684580",
        "kind": "appstore_rss",
        "query": {"url": "https://itunes.apple.com/us/rss/customerreviews/page=1/id=324684580/"
                         "sortby=mostrecent/json"},
        "expect": {"items_path": "feed.entry", "min_items": 1},
        "slow_ms": 4000.0,
        "note": "Keyless. Measured 0.10 to 0.59s, exactly 50 entries per page, 10 pages max. "
                "The app id is part of the CONTROL, so it has to be one whose feed is genuinely "
                "guaranteed non-empty. Measured 2026-08-29 on the first live run of this module: "
                "id 284882215 and 310633997 both return HTTP 200 with a well formed feed that has "
                "NO `entry` key at all, which this classifier correctly reported as fail-open. "
                "324684580 and 389801252 return 50 entries. A control is only a control if its "
                "answer is really guaranteed, and picking the wrong id makes the probe cry wolf.",
    },
    "json_list_api": {
        "id": "json_list_api:results",
        "kind": "json_list_api",
        "query": {},
        "expect": {"items_path": "results", "min_items": 1},
        "slow_ms": 5000.0,
        "note": "Generic keyless JSON endpoint whose payload carries a `results` list. A spec on "
                "this kind MUST override `query` with its own always-populated control URL.",
    },
    "json_root_list": {
        "id": "json_root_list:root",
        "kind": "json_root_list",
        "query": {},
        "expect": {"items_path": "", "min_items": 1},
        "slow_ms": 5000.0,
        "note": "The payload IS the list (v2ex /api/topics/latest.json). Specs override `query`.",
    },
    "sec_fts": {
        "id": "sec_fts:material weakness",
        "kind": "sec_fts",
        "query": {"url": "https://efts.sec.gov/LATEST/search-index?q=%22material+weakness%22"
                         "&forms=8-K"},
        "expect": {"items_path": "hits.hits", "min_items": 1, "substring": "highlight"},
        "slow_ms": 6000.0,
        "note": "Keyless, needs a User-Agent. 'material weakness' is always present in the 8-K "
                "corpus, so an empty hit list indicts the endpoint, not the corpus. The substring "
                "asserts the field the PARSER consumes, not merely the one the endpoint returns: "
                "measured live 2026-08-29, this url answered HTTP 200 with 100 hits and NOT ONE "
                "`highlight`, so the control passed at 'ok' while run.parse_sec_fulltext dropped "
                "all 100 under no_quote and the lane emitted nothing. A health check that goes "
                "green on a payload its own pipeline cannot use is the fail-open this module "
                "exists to catch, one seam further in. A miss here is DEGRADED, not down: the "
                "endpoint is up, it just stopped carrying what the lane needs.",
    },
    "reddit_archive": {
        "id": "reddit_archive:r/SaaS",
        "kind": "reddit_archive",
        "query": {"url": "https://arctic-shift.photon-reddit.com/api/posts/search"
                         "?subreddit=SaaS&limit=25&sort=desc"},
        "expect": {"items_path": "data", "min_items": 1},
        "slow_ms": 8000.0,
        "note": "Measured 6 of 12 sequential calls returning HTTP 500 on 2026-08-27. Those are "
                "HONEST failures (down). The control is still needed: a 200 carrying an empty list "
                "would be the other thing.",
    },
    "trustpilot_scrape": {
        "id": "trustpilot_scrape:example.com",
        "kind": "trustpilot_scrape",
        "query": {"url": "https://example.com", "proxy": "stealth"},
        "expect": {"substring": "Example Domain", "min_chars": 200},
        "slow_ms": 20000.0,
        "note": "Firecrawl /v2/scrape with proxy=stealth. The control deliberately does NOT hit "
                "Trustpilot: a 1-2 star page can legitimately be thin, example.com cannot. Without "
                "the stealth proxy this returns a 153 to 170 byte 'Verifying your connection' "
                "interstitial, which the classifier catches by shape.",
    },
}


# The lanes this module knows how to describe, and the control kind each rides on. A lane's health is
# the health of its TRANSPORT: linux.do is a web_scrape because brightdata is its only route, so
# "can brightdata fetch example.com" IS the linux.do liveness question.
DEFAULT_SPECS: list = [
    {"name": "brightdata", "kind": "web_search",
     "note": "first hop of the retrieval fallback chain; MCP only, probe from the skill layer"},
    {"name": "brightdata-scrape", "kind": "web_scrape",
     "note": "same MCP, different tool; scrape and search fail independently"},
    {"name": "tavily", "kind": "web_search",
     "note": "second hop; over quota but fails CLOSED with a clear error, which is correct"},
    {"name": "google-news-trends", "kind": "web_search", "note": "third hop; MCP only"},
    {"name": "linux.do", "kind": "web_scrape",
     "note": "routed solely through brightdata scrape_as_markdown on /latest.rss"},
    {"name": "reddit", "kind": "reddit_archive"},
    {"name": "v2ex", "kind": "json_root_list",
     "control": dict(CONTROLS["json_root_list"],
                     id="json_root_list:v2ex",
                     query={"url": "https://www.v2ex.com/api/topics/latest.json"})},
    {"name": "trustpilot", "kind": "trustpilot_scrape"},
    {"name": "appstore-rss", "kind": "appstore_rss"},
    {"name": "sec-edgar-fts", "kind": "sec_fts"},
    {"name": "federal-register", "kind": "json_list_api",
     "control": dict(CONTROLS["json_list_api"],
                     id="json_list_api:federal-register",
                     query={"url": "https://www.federalregister.gov/api/v1/documents.json"
                                   "?per_page=1&order=newest"})},
    {"name": "usaspending", "kind": "json_list_api",
     "control": dict(CONTROLS["json_list_api"],
                     id="json_list_api:usaspending-agencies",
                     query={"url": "https://api.usaspending.gov/api/v2/references/"
                                   "toptier_agencies/"})},
    {"name": "the-muse", "kind": "json_list_api",
     "control": dict(CONTROLS["json_list_api"],
                     id="json_list_api:the-muse",
                     query={"url": "https://www.themuse.com/api/public/jobs?page=1"})},
]


def control_for(spec: dict):
    """The control a spec probes with: its own inline one, else the one for its kind, else None.

    None is not an error here, it becomes state `unknown` at probe time. A source with no declared
    control has not been checked, and saying so is the entire point."""
    if not isinstance(spec, dict):
        return None
    inline = spec.get("control")
    if isinstance(inline, dict):
        return inline
    if isinstance(inline, str):
        return CONTROLS.get(inline)
    kind = spec.get("kind") or spec.get("control_kind")
    if isinstance(kind, str):
        return CONTROLS.get(kind)
    return None


def control_assertions(control) -> list:
    """The assertion names a control actually declares. Empty means it can never fail."""
    if not isinstance(control, dict):
        return []
    exp = control.get("expect")
    if not isinstance(exp, dict):
        return []
    names = []
    if isinstance(exp.get("substring"), str) and exp["substring"].strip():
        names.append("substring")
    try:
        if int(exp.get("min_chars") or 0) > 0:
            names.append("min_chars")
    except (TypeError, ValueError):
        pass
    if "items_path" in exp:
        names.append("items_path")
    return names


# --------------------------------------------------------------------------- payload inspection

def _dig(payload, path):
    """Walk a dotted path. "" / "." / "$" mean the payload root. Missing returns the sentinel."""
    if path is None or str(path).strip() in ("", ".", "$"):
        return payload
    cur = payload
    for seg in str(path).split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return _MISSING
            cur = cur[seg]
        elif isinstance(cur, list):
            if not seg.lstrip("-").isdigit():
                return _MISSING
            i = int(seg)
            if i >= len(cur) or i < -len(cur):
                return _MISSING
            cur = cur[i]
        else:
            return _MISSING
    return cur


def payload_text(payload) -> str:
    """The textual body of a payload, across the shapes the fetchers actually return.

    Handles MCP content-block lists ({"content": [{"type": "text", "text": ...}]}), which is the
    shape brightdata returns and the shape whose EMPTY form started all of this."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        blocks = payload.get("content")
        if isinstance(blocks, list):
            return "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
        if isinstance(payload.get("text"), str):
            return payload["text"]
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if isinstance(payload, list):
        if payload and all(isinstance(b, dict) and "text" in b for b in payload):
            return "".join(str(b.get("text") or "") for b in payload)
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)


def transport_error(payload):
    """A transport-level error carried INSIDE an otherwise well formed payload, or None.

    tavily over quota is the model citizen here: it fails CLOSED with a clear error. That is `down`,
    not fail-open, and the contrast is exactly what makes the brightdata case legible."""
    if isinstance(payload, BaseException):
        return "%s: %s" % (type(payload).__name__, payload)
    if not isinstance(payload, dict):
        return None
    if payload.get("isError") is True or payload.get("is_error") is True:
        return str(payload.get("error") or payload.get("message") or "isError")
    for key in ("error", "errors", "error_message"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict) and v:
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
        if isinstance(v, list) and v:
            return json.dumps(v, ensure_ascii=False)
    for key in ("status", "status_code", "statusCode", "http_status"):
        v = payload.get(key)
        try:
            code = int(v)
        except (TypeError, ValueError):
            continue
        if code >= 400:
            return "HTTP %d" % code
    return None


def looks_interstitial(text: str) -> bool:
    """The bot-challenge shape: a short body carrying a challenge phrase, served as HTTP 200."""
    if not text:
        return False
    if len(text) > INTERSTITIAL_MAX_CHARS:
        return False
    low = text.lower()
    return any(m in low for m in INTERSTITIAL_MARKERS)


def evaluate_content(payload, control) -> dict:
    """Measure a payload against a control's assertions. Pure: no clock, no network.

    Returns {"empty", "reason", "chars", "items", "interstitial", "assertion_ok", "detail"}.
    `empty` is the fail-open trigger: the control guarantees content and there is none."""
    exp = (control or {}).get("expect") or {}
    text = payload_text(payload)
    chars = len(text)
    items = None
    empty = False
    reason = ""
    assertion_ok = True

    if "items_path" in exp:
        where = exp.get("items_path") or "<root>"
        found = _dig(payload, exp.get("items_path"))
        if found is _MISSING:
            empty = True
            reason = "items path %r absent from payload" % where
        elif isinstance(found, (list, tuple)):
            items = len(found)
            try:
                need = max(1, int(exp.get("min_items", 1)))
            except (TypeError, ValueError):
                need = 1
            if items < need:
                empty = True
                reason = "control guarantees >=%d items at %r, got %d" % (need, where, items)
        elif isinstance(found, dict):
            items = len(found)
            if not found:
                empty = True
                reason = "items path %r is an empty object" % where
        else:
            empty = True
            reason = "items path %r is not a collection (%s)" % (where, type(found).__name__)

    if not empty and payload is None:
        empty, reason = True, "fetcher returned None while reporting success"

    if not empty and isinstance(payload, (dict, list, str, bytes)) and len(payload) == 0:
        empty, reason = True, "empty %s payload" % type(payload).__name__

    if not empty and items is None and not text.strip():
        empty, reason = True, "payload carries no text"

    try:
        min_chars = int(exp.get("min_chars") or 0)
    except (TypeError, ValueError):
        min_chars = 0
    if not empty and min_chars and items is None and chars < min_chars:
        empty, reason = True, "control guarantees >=%d chars, got %d" % (min_chars, chars)

    interstitial = looks_interstitial(text)

    sub = exp.get("substring")
    if isinstance(sub, str) and sub.strip():
        assertion_ok = sub.lower() in text.lower()

    detail = "chars=%d" % chars
    if items is not None:
        detail += " items=%d" % items
    return {"empty": empty, "reason": reason, "chars": chars, "items": items,
            "interstitial": interstitial, "assertion_ok": assertion_ok, "detail": detail}


# --------------------------------------------------------------------------- fetch outcome

class FetchOutcome:
    """What a fetcher hands back when it wants to report more than the payload.

    A plain payload (str, dict, list, None) is the common case and is normalized into one of these
    with attempts=1 and no meta. A fetcher that retried, that knows it returned partial data, or
    that caught its own transport error, says so HERE instead of encoding it in the payload."""

    __slots__ = ("payload", "attempts", "partial", "error", "latency_ms")

    def __init__(self, payload=None, attempts: int = 1, partial: bool = False,
                 error=None, latency_ms=None):
        self.payload = payload
        self.attempts = int(attempts or 1)
        self.partial = bool(partial)
        self.error = error
        self.latency_ms = latency_ms

    def __repr__(self) -> str:  # pragma: no cover, debugging aid
        return "FetchOutcome(attempts=%d, partial=%s, error=%r)" % (
            self.attempts, self.partial, self.error)


def _as_outcome(raw) -> FetchOutcome:
    return raw if isinstance(raw, FetchOutcome) else FetchOutcome(payload=raw)


# --------------------------------------------------------------------------- classification

def classify(payload, control, attempts: int = 1, partial: bool = False,
             error=None, latency_ms=None) -> tuple:
    """(state, detail) for one control response. Pure, and it never raises.

    Order matters and encodes the priorities:
      1. an explicit transport error is `down` even when a body came with it
      2. a challenge interstitial is contentless success, so fail-open, never ok
      3. empty where the control GUARANTEES content is THE fail-open case
      4. content present but the substring assertion missed is `degraded`, not a lie
      5. slow, retried or partial is `degraded`
    """
    if error:
        return DOWN, "transport error: %s" % str(error)[:300]
    inner = transport_error(payload)
    if inner:
        # DOWN, not OK. An error the source reported IN BAND is still the source telling us it
        # failed, and rule 1 above does not care whether the news arrived out of band or inside the
        # body. Returning OK here paired a "transport error: ..." detail with a healthy state and an
        # exit code of 0, which is the precise failure this module exists to make impossible.
        return DOWN, "transport error: %s" % inner[:300]

    ev = evaluate_content(payload, control)
    if ev["interstitial"]:
        return FAIL_OPEN, ("bot interstitial served as success (%d chars, challenge phrase "
                           "present); the control guarantees real content" % ev["chars"])
    if ev["empty"]:
        return FAIL_OPEN, "success reported with no content: %s" % ev["reason"]
    if not ev["assertion_ok"]:
        sub = ((control or {}).get("expect") or {}).get("substring")
        return DEGRADED, ("content returned but control assertion missed: %r not in body (%s)"
                          % (sub, ev["detail"]))

    notes = []
    try:
        if int(attempts) > 1:
            notes.append("needed %d attempts" % int(attempts))
    except (TypeError, ValueError):
        pass
    if partial:
        notes.append("fetcher reported a partial result")
    try:
        slow_ms = float((control or {}).get("slow_ms") or DEFAULT_SLOW_MS)
    except (TypeError, ValueError):
        slow_ms = DEFAULT_SLOW_MS
    if latency_ms is not None and float(latency_ms) > slow_ms:
        notes.append("slow: %.0fms over the %.0fms budget" % (float(latency_ms), slow_ms))
    if notes:
        return DEGRADED, "control satisfied (%s) but %s" % (ev["detail"], "; ".join(notes))
    return OK, "control satisfied (%s)" % ev["detail"]


# --------------------------------------------------------------------------- probing

def probe_source(name, fetcher, control, clock=None) -> dict:
    """Run ONE control against ONE fetcher and return a result. NEVER raises into the caller.

    Keys: name, state, detail, latency_ms, checked_at, control_used.
    `latency_ms` is None when nothing was actually called and `control_used` is None when no control
    could be resolved: a result that measured nothing must not look like a measurement."""
    label = str(name or "").strip() or "<unnamed>"
    checked_at = iso(now_utc())

    if isinstance(control, str):
        control = CONTROLS.get(control)
    if not isinstance(control, dict):
        return {"name": label, "state": UNKNOWN, "detail": "no control declared for this source",
                "latency_ms": None, "checked_at": checked_at, "control_used": None}

    control_used = str(control.get("id") or control.get("kind") or "control")
    if not control_assertions(control):
        return {"name": label, "state": UNKNOWN,
                "detail": "control %s asserts nothing, so it can never fail; refusing to call it "
                          "ok" % control_used,
                "latency_ms": None, "checked_at": checked_at, "control_used": control_used}

    if fetcher is None:
        return {"name": label, "state": UNKNOWN,
                "detail": "no fetcher wired for this source; nothing was checked",
                "latency_ms": None, "checked_at": checked_at, "control_used": control_used}
    if not callable(fetcher):
        return {"name": label, "state": UNKNOWN,
                "detail": "fetcher for this source is not callable (%s)" % type(fetcher).__name__,
                "latency_ms": None, "checked_at": checked_at, "control_used": control_used}

    tick = clock or time.monotonic
    t0 = tick()
    try:
        outcome = _as_outcome(fetcher(control.get("query")))
        latency_ms = (float(outcome.latency_ms) if outcome.latency_ms is not None
                      else max(0.0, (tick() - t0) * 1000.0))
        state, detail = classify(outcome.payload, control, attempts=outcome.attempts,
                                 partial=outcome.partial, error=outcome.error,
                                 latency_ms=latency_ms)
    except (socket.timeout, TimeoutError) as e:
        return {"name": label, "state": DOWN,
                "detail": "timeout: %s" % (str(e) or type(e).__name__),
                "latency_ms": round(max(0.0, (tick() - t0) * 1000.0), 2),
                "checked_at": checked_at, "control_used": control_used}
    except Exception as e:
        # A probe that raised is a RESULT, not an exception: the caller is a health check and must
        # keep going. It reports `down`, never fail_open. The source did not claim success here, it
        # blew up, and conflating the two would blunt the one signal this module exists to produce.
        return {"name": label, "state": DOWN,
                "detail": "fetcher raised %s: %s" % (type(e).__name__, str(e)[:250]),
                "latency_ms": round(max(0.0, (tick() - t0) * 1000.0), 2),
                "checked_at": checked_at, "control_used": control_used}

    return {"name": label, "state": state, "detail": detail,
            "latency_ms": round(float(latency_ms), 2), "checked_at": checked_at,
            "control_used": control_used}


def probe_all(specs, fetchers) -> dict:
    """Probe every spec. Returns {"results": [...], "ok", "degraded", "down",
    "fail_open_suspected", "unknown"}. Never raises."""
    fetchers = fetchers if isinstance(fetchers, dict) else {}
    items = []
    if isinstance(specs, dict):
        for k, v in specs.items():
            spec = dict(v) if isinstance(v, dict) else {}
            spec.setdefault("name", k)
            items.append(spec)
    else:
        for s in (specs or []):
            items.append(s if isinstance(s, dict) else {"name": str(s)})

    results = []
    for spec in items:
        name = str(spec.get("name") or "").strip() or "<unnamed>"
        results.append(probe_source(name, fetchers.get(name), control_for(spec)))

    summary = {"results": results}
    for st in STATES:
        summary[st] = sum(1 for r in results if r.get("state") == st)
    return summary


def coverage_block(summary: dict) -> dict:
    """The shape run.py folds into coverage["source_health"].

    A caller that never ran a probe must NOT call this and publish its zeros; it names
    `source_health` in coverage["unmeasured"] instead. Zeros here mean "probed, found none", which
    is a completely different claim from "nobody looked"."""
    results = (summary or {}).get("results") or []
    block = {st: int((summary or {}).get(st) or 0) for st in STATES}
    block["names_down"] = sorted(str(r.get("name", "?")) for r in results
                                 if r.get("state") == DOWN)
    block["names_fail_open"] = sorted(str(r.get("name", "?")) for r in results
                                      if r.get("state") == FAIL_OPEN)
    return block


def verdict(summary: dict) -> str:
    """all_ok / partial / unchecked / attention. The CLI exit code is a function of this."""
    s = summary or {}
    if int(s.get(DOWN) or 0) or int(s.get(FAIL_OPEN) or 0):
        return "attention"
    checked = int(s.get(OK) or 0) + int(s.get(DEGRADED) or 0)
    if not checked:
        return "unchecked"
    if int(s.get(DEGRADED) or 0) or int(s.get(UNKNOWN) or 0):
        return "partial"
    return "all_ok"


def exit_code(summary: dict) -> int:
    return {"all_ok": EXIT_OK, "partial": EXIT_PARTIAL,
            "unchecked": EXIT_UNCHECKED, "attention": EXIT_ATTENTION}[verdict(summary)]


# --------------------------------------------------------------------------- real fetchers

# Everything below actually touches the network and is wired ONLY when the CLI is given --live.
# The unit tests inject their own fetchers and never reach this section.

#: SEC asks callers to identify themselves. The default is SYNTHETIC on purpose: a real address in a
#: public repo is a PII leak, and this exact one has leaked before. Operators set the env var.
SEC_UA_ENV = "DAILY_HOTSPOTS_SEC_UA"
DEFAULT_UA = "daily-hotspots-sourcehealth/1.0 (contact: user1@example.com)"

HTTP_TIMEOUT_S = 20.0


def _http_json_or_text(url: str, headers=None, timeout: float = HTTP_TIMEOUT_S):
    """GET a URL, return parsed JSON when it parses, else the raw text. Raises on a transport
    error, which probe_source turns into `down`."""
    req = urllib.request.Request(url, headers=dict(headers or {"User-Agent": DEFAULT_UA}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _url_fetcher(headers=None):
    """Adapter: turns a control's {"url": ...} query into a live GET."""
    def _fetch(query):
        url = (query or {}).get("url")
        if not url:
            raise ValueError("control query carries no url")
        return _http_json_or_text(url, headers=headers)
    return _fetch


def _sec_fetcher(query):
    ua = os.environ.get(SEC_UA_ENV) or DEFAULT_UA
    url = (query or {}).get("url")
    if not url:
        raise ValueError("control query carries no url")
    return _http_json_or_text(url, headers={"User-Agent": ua, "Accept": "application/json"})


#: The lanes this deterministic process can probe BY ITSELF. Everything absent from this map is
#: reported `unknown` rather than assumed healthy. brightdata, tavily and google-news-trends are
#: absent because they are MCP tools: the skill layer runs their control and feeds the raw response
#: back through --observations, into the same classifier.
LIVE_FETCHERS: dict = {
    "v2ex": _url_fetcher(),
    "reddit": _url_fetcher(),
    "appstore-rss": _url_fetcher(),
    "federal-register": _url_fetcher(),
    "usaspending": _url_fetcher(),
    "the-muse": _url_fetcher(),
    "sec-edgar-fts": _sec_fetcher,
}

#: Named so a report can say WHY a lane is unknown instead of leaving the reader to guess.
MCP_ONLY = ("brightdata", "brightdata-scrape", "tavily", "google-news-trends", "linux.do",
            "trustpilot")


def observation_fetchers(observations: dict) -> dict:
    """Turn {source_name: raw_control_response} into fetchers.

    This is how an MCP-routed lane gets probed at all: the orchestration layer runs the control
    query with the real tool and hands the RAW response here, where it meets exactly the same
    classifier as a locally fetched one. A name absent from the mapping gets no fetcher, so it stays
    `unknown` rather than being judged on a payload nobody produced."""
    out = {}
    for name, payload in (observations or {}).items():
        out[str(name)] = (lambda p: (lambda _query: p))(payload)
    return out


# --------------------------------------------------------------------------- specs and report io

def specs_from_config(cfg) -> list:
    """Specs derived from the operator's enabled `sources`, honoring a per-source `health` block.

    Reader: a config that declares no sources yields no specs and the caller falls back to
    DEFAULT_SPECS. A source with no resolvable kind still gets a spec, and lands in `unknown`."""
    sources = ((cfg or {}).get("sources") or {})
    known = {s["name"]: s for s in DEFAULT_SPECS}
    specs = []
    for name, sc in sources.items():
        sc = sc if isinstance(sc, dict) else {}
        if not sc.get("enabled", True):
            continue
        health = sc.get("health")
        if isinstance(health, dict):
            spec = dict(health)
            spec["name"] = name
        elif name in known:
            spec = dict(known[name])
        else:
            spec = {"name": name, "kind": None}
        specs.append(spec)
    return specs


def default_specs() -> tuple:
    """(specs, note). Config-declared sources merged under the built-in list, config permitting."""
    try:
        cfg = load_config()
        from_cfg = specs_from_config(cfg)
        note = "config sources merged"
    except Exception as e:            # reader: degrade, and SAY the config was not read
        from_cfg = []
        note = "config unavailable (%s); built-in specs only" % type(e).__name__
    if not from_cfg and "unavailable" not in note:
        note = "config declared no sources; built-in specs only"
    merged = {s["name"]: dict(s) for s in DEFAULT_SPECS}
    for s in from_cfg:
        merged.setdefault(s["name"], s)
    return list(merged.values()), note


def load_specs(path) -> list:
    """Read a specs file (a list, or a {name: spec} map). A malformed file is an ERROR: probing
    nothing because the spec file was bad is the exact failure shape being hunted."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("specs", data)
    if isinstance(data, dict):
        return [dict(v, name=k) if isinstance(v, dict) else {"name": k} for k, v in data.items()]
    if isinstance(data, list):
        return [s if isinstance(s, dict) else {"name": str(s)} for s in data]
    raise ValueError("specs file %s is neither a list nor an object" % path)


def report_envelope(summary: dict, wired=None, note: str = "") -> dict:
    """The CLI's machine output. `verdict` and `sources_checked` exist so a clean report and a
    report that checked nothing can never be mistaken for one another, by eye or by grep."""
    results = (summary or {}).get("results") or []
    return {
        "schema_version": 1,
        "checked_at": iso(now_utc()),
        "verdict": verdict(summary),
        "counts": {st: int((summary or {}).get(st) or 0) for st in STATES},
        "sources_checked": sum(1 for r in results if r.get("state") != UNKNOWN),
        "sources_declared": len(results),
        "wired": sorted(str(w) for w in (wired or [])),
        "mcp_only": list(MCP_ONLY),
        "note": note,
        "coverage": coverage_block(summary),
        "results": results,
    }


def write_report(path, envelope: dict) -> Path:
    """WRITER. No try/except, no in-repo default: an IO failure propagates to the caller."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_report(path):
    """READER. Returns None when there is no readable report, which the caller must treat as
    UNMEASURED, never as a clean bill of health."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def text_summary(envelope: dict) -> str:
    """One Chinese line for a human. Loud when anything is down or failing open, because a silently
    dead source no longer being silent is the whole point."""
    c = envelope.get("counts") or {}
    cov = envelope.get("coverage") or {}
    if envelope.get("verdict") == "unchecked":
        return "源健康: 未检查 (%d 个源全部没有探针, 这不等于 clean)" % (
            envelope.get("sources_declared") or 0)
    head = "源健康: 正常 %d / 降级 %d / 挂了 %d / 假成功 %d / 未检查 %d" % (
        c.get(OK, 0), c.get(DEGRADED, 0), c.get(DOWN, 0), c.get(FAIL_OPEN, 0), c.get(UNKNOWN, 0))
    if cov.get("names_fail_open"):
        head += "  [!!] 假成功: %s" % ", ".join(cov["names_fail_open"])
    if cov.get("names_down"):
        head += "  [!] 挂了: %s" % ", ".join(cov["names_down"])
    return head


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Probe each collection source with a control query whose answer is known "
                    "non-empty. Exit 0 all ok, 1 partial, 2 nothing could be checked, 3 something "
                    "is down or failing open, 4 the probe could not be set up.")
    ap.add_argument("--specs", help="JSON file of source specs (a list, or {name: spec})")
    ap.add_argument("--observations",
                    help="JSON file of {source: raw_control_response} captured by the skill "
                         "orchestration layer, for MCP-routed lanes this process cannot call")
    ap.add_argument("--live", action="store_true",
                    help="wire the keyless HTTP adapters and actually hit the network")
    ap.add_argument("--out", help="write the full JSON report here (writer: hard fails)")
    ap.add_argument("--text", action="store_true",
                    help="also print the one-line Chinese summary on stderr")
    args = ap.parse_args(argv)

    # Setting the probe up is a HARD failure with its own exit code. A specs file that will not
    # parse must never degrade into "we probed zero sources and everything looked fine": that is
    # the shape of failure this whole module exists to refuse.
    try:
        if args.specs:
            specs, note = load_specs(args.specs), "specs from %s" % args.specs
        else:
            specs, note = default_specs()

        fetchers: dict = {}
        if args.live:
            fetchers.update(LIVE_FETCHERS)
        if args.observations:
            obs = json.loads(Path(args.observations).read_text(encoding="utf-8"))
            if not isinstance(obs, dict):
                raise ValueError("observations file must be a {source: response} object")
            fetchers.update(observation_fetchers(obs))
    except Exception as e:
        print("sourcehealth: could not set up the probe, nothing was checked: %s: %s"
              % (type(e).__name__, e), file=sys.stderr)
        return EXIT_USAGE

    if not fetchers:
        note = (note + "; " if note else "") + \
               "no fetchers wired (pass --live and/or --observations): nothing was checked"

    summary = probe_all(specs, fetchers)
    env = report_envelope(summary, wired=sorted(fetchers), note=note)
    print(json.dumps(env, ensure_ascii=False))
    if args.text:
        print(text_summary(env), file=sys.stderr)
    if args.out:
        write_report(args.out, env)
    return exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
