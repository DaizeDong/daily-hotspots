#!/usr/bin/env python3
"""Source ADAPTERS: every lane that turns a vendor's raw response into origin-tagged signals.

Split out of run.py on 2026-08-29. The two halves change for different reasons and never called each
other: an AST call graph over run.py's top-level definitions found ZERO references from this half
into the deterministic core, and exactly six the other way, all of them into three shared helpers
that now live in lib.py. Adapters change when a vendor changes its payload; the core changes when
the methodology changes. They were one file only because they arrived at different times.

What is here: the X roster loop, the community lanes (v2ex, RSS, linux.do), the six demand lanes
(trustpilot, appstore, SEC full text, Federal Register, USAspending, The Muse), their shared
sanitizers, and collect_sources, which is what `run.py --sources` calls.

THE RULE THIS FILE EXISTS TO ENFORCE, and it is the reason parsing happens HERE rather than upstream
in the collecting agent: a failed fetch must become a STRUCTURAL FACT, not an empty list. arctic-shift
returns HTTP 500 on roughly half of all calls (measured 6 of 12), brightdata returns a well formed
EMPTY payload and reports success, and Trustpilot without a stealth proxy returns a 170 byte
"Verifying your connection" page. Every one of those reads as "the source was quiet today" unless
something deliberately tells them apart. That is what the payload_status functions do.
"""
from __future__ import annotations

import html as _html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import roster as rt
from lib import (_handle_origin, failed_pull, iso, is_failed_pull, load_config, now_utc,
                 parse_ts, safe_url, split_pulls)
from lib import clean_text as _clean_text
from classify import keyword_hit

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


# --------------------------------------------------------------------------- how many tries it took
# MEASURED 2026-08-29: twelve sequential calls to the arctic-shift posts/search endpoint returned
# 500 200 200 500 200 500 200 200 500 200 500 500, a 50% failure rate on the SOLE route the reddit
# lane has. At that rate a single attempt loses the lane every other day, while three attempts lose
# it about one day in eight (0.5**3), so retrying is mandatory. It happens OUTSIDE this file:
# run.py does no network of its own, and reference/collect.md is where the retry policy is written.
#
# run.py used to carry a retry_pull / retry_delays SEAM here, a backoff schedule waiting for a caller
# that would hand it a zero-argument fetch callable. Nothing ever did, and nothing could: every fetch
# in this skill happens either in the agent's MCP loop (collect.md) or in sourcehealth.py, which has
# its own FetchOutcome protocol, and every collect_* entry point in this file takes an ALREADY
# FETCHED payload. Deleted 2026-08-29 together with its tests. What remains is the honest half:
# whatever the fetch layer actually did, it reports in its own envelope, and that number is RECORDED
# rather than assumed. Do not reintroduce the seam without a caller, because an unreachable retry
# helper sitting beside the live attempts-reporting path reads as if this lane retries when it does
# not.


def _envelope_attempts(raw) -> int:
    """How many attempts the FETCH layer says it made, from its own envelope. Default 1.

    A fetch layer that reports nothing is credited with exactly one attempt, which is the honest
    floor: something was clearly issued, because we are holding its answer."""
    if isinstance(raw, dict):
        for k in ("attempts", "attempt_count", "tries"):
            v = raw.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v >= 1:
                return int(v)
    return 1


def _envelope_outcome(raw) -> str | None:
    """The terminal verdict the fetch layer reported for a lane, if it reported one."""
    if isinstance(raw, dict):
        v = raw.get("outcome")
        if isinstance(v, str) and v.strip():
            return v.strip()[:60]
    return None


# --------------------------------------------------------------------------- arctic-shift (reddit)
# The reddit lane is 10% of archived cards and arctic-shift is its only route. Until now run.py had
# NO parser for it: unlike v2ex (parse_v2ex) and the RSS lanes (parse_rss), arctic-shift
# normalization happened wherever the orchestration layer felt like doing it, and an HTTP 500 that
# arrives as an error body, a bare list, or nothing at all was indistinguishable from a quiet day.
# community_payload_status only recognizes a failure when the fetch layer VOLUNTEERS an ``errors``
# key, so the honest-failure path existed but nothing on the reddit lane was standing on it. These
# functions put it there deterministically.
_ARCTIC_ERROR_FIELDS = ("error", "errors", "detail", "message")
_HTTP_STATUS_FIELDS = ("status", "status_code", "http_status", "statusCode")


def _decode_payload(raw):
    """``(obj, error)``: a raw response body reduced to the JSON value inside it, or the reason it
    holds none. ONE copy of the two verdicts that were byte identical in three parsers.

    A STRING is a raw HTTP body the fetch layer handed straight through. An empty one and one that
    does not decode are FAILURES, never a quiet empty day: this is the arctic-shift lesson again, the
    dangerous response is the tidy one, and an HTML error page must not read as "the API answered and
    had nothing". A null payload is a failure for the same reason. Anything else is returned
    UNTOUCHED and unjudged.

    What is deliberately NOT here is the type verdict, because the callers genuinely disagree about
    it: _rows_of and arctic_shift_payload_status accept a bare list as already unwrapped rows,
    parse_appstore_rss does not (it needs a ``feed`` object), and roster_payload_status refuses both a
    string body and a bare list. Folding those in behind flags would put three different contracts
    under one name, which is the thing this file keeps having to undo."""
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return None, "no payload (empty body)"
        try:
            decoded = json.loads(t)
        except (ValueError, TypeError):
            return None, "malformed payload: non-JSON body (%d chars)" % len(t)
        # The null verdict must be reached by a body that DECODED to null, not only by a caller
        # that passed None. The first extraction returned here directly, so a body of the literal
        # text "null" stopped reporting "no payload (null response)" and started reporting
        # "malformed payload: expected an object, got NoneType" at all three call sites. Both are
        # failures, so nothing failed open, but the operator-facing string changed inside a change
        # whose own docstring called itself byte identical, and the new table had no row for it.
        if decoded is None:
            return None, "no payload (null response)"
        return decoded, None
    if raw is None:
        return None, "no payload (null response)"
    return raw, None


def _http_status_error(raw: dict) -> str | None:
    """"HTTP <code>" when a payload envelope carries a non-2xx status, else None.

    A ``status`` that is a STRING is read too, because fetch layers write ``"status": "error"`` as
    often as ``"status": 500``; a string that is neither an integer nor an ok-word is itself the
    failure."""
    for f in _HTTP_STATUS_FIELDS:
        if f not in raw:
            continue
        v = raw.get(f)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            code = int(v)
            if not (200 <= code < 300):
                return "HTTP %d" % code
            continue
        if isinstance(v, str):
            t = v.strip()
            if t.isdigit():
                code = int(t)
                if not (200 <= code < 300):
                    return "HTTP %d" % code
                continue
            if t.lower() not in ("ok", "success", "succeeded", ""):
                return "status: %s" % t[:100]
    return None


def arctic_shift_payload_status(raw) -> tuple[list, str | None]:
    """Split a RAW arctic-shift ``/api/posts/search`` response into ``(posts, error)``.

    The success shape is ``{"data": [ ...post objects... ]}``. Everything below is a FAILURE, and the
    reason each is named separately is that every one of them used to reach collect_community_source
    as ``[]``, which is the shape of a real, honest, empty day:

      * a null or absent payload,
      * a JSON body that is neither an object nor an array,
      * a body carrying error / detail / message,
      * a body carrying a non-2xx HTTP status (the measured 50% case),
      * an object with no ``data`` list.

    A bare LIST is accepted as the already-unwrapped ``data`` array, because some fetch layers hand
    back ``resp["data"]``. A STRING is decoded as JSON first (a raw response body), and a body that
    does not decode is a failure that reports its own size, so an HTML error page cannot pass as a
    quiet day."""
    raw, err = _decode_payload(raw)
    if err is not None:
        return [], err
    if isinstance(raw, list):
        return raw, None
    if not isinstance(raw, dict):
        return [], "malformed payload: expected an object, got %s" % type(raw).__name__
    # BOTH halves, when both are present. A 500 body usually carries a generic "Internal Server
    # Error" string, and reporting only that loses the status code, which is the half an operator
    # can act on ("half the calls 500" is a route problem; "auth failed" is not).
    field = None
    for f in _ARCTIC_ERROR_FIELDS:
        v = raw.get(f)
        if v:
            detail = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            field = "%s: %s" % (f, detail[:200])
            break
    st = _http_status_error(raw)
    if st is not None and field is not None:
        return [], "%s (%s)" % (st, field)
    if st is not None:
        return [], st
    if field is not None:
        return [], field
    data = raw.get("data")
    if isinstance(data, list):
        return data, None
    return [], "malformed payload: object with no 'data' list and no error"


def _reddit_permalink(post: dict) -> str:
    """The canonical reddit URL for one arctic-shift post, or "" when it names none.

    ``permalink`` is a site-relative path and is preferred; ``full_link`` / ``url`` are absolute.
    Every candidate is validated (scheme plus host pinned to reddit.com) before it is emitted,
    because an archive row is untrusted input and ``url`` on a link post points at whatever was
    submitted."""
    pl = post.get("permalink")
    if isinstance(pl, str) and pl.strip().startswith("/"):
        return safe_url("https://www.reddit.com" + pl.strip(), ("reddit.com",))
    for f in ("full_link", "link", "url"):
        u = safe_url(post.get(f), ("reddit.com",))
        if u:
            return u
    return ""


def parse_arctic_shift(raw, attempts: int = 1) -> dict:
    """RAW arctic-shift response -> the community-lane ENVELOPE collect_community_source consumes.

    Returns ``{"items", "errors", "attempts", "dropped_malformed", "dropped_no_url"}``. On a failure
    the items list is empty AND ``errors`` is populated, which is precisely the difference between
    "the lane was reached and had nothing" and "the lane was not reached": community_payload_status
    turns the second into a failed_pull instead of a ``pulled=0`` denominator line, so the weekly
    auto-prune can never read a 500 as deadweight.

    Rows that carry no usable permalink are skipped and COUNTED under ``dropped_no_url`` rather than
    emitted with an empty url; a signal with no url can be neither attributed nor de-duplicated."""
    posts, err = arctic_shift_payload_status(raw)
    env: dict = {"items": [], "errors": [err] if err else [],
                 "attempts": max(1, int(attempts or 1)),
                 "dropped_malformed": 0, "dropped_no_url": 0}
    if err:
        return env
    items = []
    for post in posts:
        if not isinstance(post, dict):
            env["dropped_malformed"] += 1
            continue
        url = _reddit_permalink(post)
        if not url:
            env["dropped_no_url"] += 1
            continue
        sub = post.get("subreddit")
        items.append({
            "title": _clean_text(post.get("title")),
            "url": url,
            "category": str(sub) if sub else None,
            "heat": post.get("num_comments"),
            "ts": _epoch_to_iso(post.get("created_utc")),
            "summary": _clean_text(post.get("selftext")),
        })
    env["items"] = items
    return env


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
        # ONE date normalizer for the whole file. parse_rss used to call parsedate_to_datetime
        # alone, so an ISO <pubDate> (2023-01-02T03:04:05Z) yielded ts="". An empty ts is not inert:
        # collect_community_source only applies the staleness drop "if lr is not None and ts",
        # so the item survived the filter, then lib.age_hours("") returned 0.0 and lib.freshness(0.0)
        # returned 1.0. A three-year-old item scored maximum freshness. _norm_date reads epoch, ISO,
        # bare YYYY-MM-DD and RFC 2822, which is what every demand lane already uses.
        ts = _norm_date(_text(item, "pubDate"))
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
            # A LIST of error strings reads back as prose, not as a JSON blob. The operator sees this
            # string in sources_failed and on the digest, and '["HTTP 500 (error: Internal Server
            # Error)"]' buries the one fact that matters (the 500) inside quoting noise.
            if isinstance(errs, str):
                detail = errs
            elif isinstance(errs, list) and all(isinstance(e, str) for e in errs):
                detail = "; ".join(errs)
            else:
                detail = json.dumps(errs, ensure_ascii=False)
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

    raw_payload = items
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

    # How many times the FETCH layer says it issued this pull, and what verdict it reached. Both
    # travel onto the failure record so sources_failed can distinguish a one shot flake from an
    # exhausted retry budget; a fetch layer that reports neither is credited with exactly one
    # attempt, which is the honest floor rather than an assumption.
    n_attempts = _envelope_attempts(raw_payload)
    if lane_error is not None:
        pulls = [failed_pull({"source": source}, lane_error, run_id, now,
                             attempts=n_attempts, outcome=_envelope_outcome(raw_payload))]
    else:
        # A DENOMINATOR line carries ``attempts`` only when it took more than one, because one
        # attempt is what the presence of the line already says. Recording the retried-but-succeeded
        # case matters (a lane that needs three tries every day is a lane about to die); recording
        # "attempts: 1" on every healthy line would only add noise to the ledger yield.py reads.
        pulls = [{"run_id": run_id, "ts": iso(now), "source": source,
                  "pulled": len(items or []), "kept": len(signals)}]
        if n_attempts > 1:
            pulls[0]["attempts"] = n_attempts
    filtered = {"source": source, "pulled": len(items or []), "kept": len(signals),
                "dropped_by_keep": dropped["keep"],
                "dropped_by_drop_category": dropped["drop_category"],
                "dropped_by_drop_keyword": dropped["drop_keyword"],
                "dropped_stale": dropped["stale"],
                "dropped_malformed": dropped["malformed"],
                "attempts": n_attempts,
                "error": lane_error}
    return {"signals": signals, "pulls": pulls, "filtered": filtered}


# ===========================================================================
# NEW DEMAND SOURCES (Lane D). Six verified-reachable sources, each probed with
# real calls before a line of this was written. run.py owns only the
# deterministic normalizer: the fetch stays in the SKILL orchestration layer
# (reference/collect.md recipe) and the config stays in lib.DEFAULT_CONFIG.
#
# Every one of these is a DEMAND lane, so every emitted signal must carry the
# three things the demand gate downstream needs: a VERBATIM pain quote, a
# PERMALINK, and a DATE. An item that cannot supply all three is SKIPPED and
# COUNTED under the reason that skipped it. It is never emitted with an empty
# field, because a signal with an empty quote or an empty url is not a weaker
# signal, it is a signal the gate will silently reject after the collection
# ledger has already counted it as collected, which is how a lane comes to look
# productive while contributing nothing.
#
# Every field arrives as UNTRUSTED TEXT. Nothing here is evaluated, formatted
# into a command, or followed: control characters, zero-width characters and
# bidirectional overrides are stripped (they are how an instruction hides
# inside a quote), HTML is reduced to text with script and style bodies
# removed, and every url is validated against a PINNED host before it is
# emitted, so a review whose "permalink" points somewhere else is dropped
# rather than published under this source's origin.
# ===========================================================================

# Stable per-lane origin = the HOST. The yield engine keys the numerator on
# origin / origin_source / source (yield.origins_of), so all three carry the
# same host and the pulls-log denominator line names that same host.
NEW_SOURCE_ORIGINS = {
    "trustpilot": "trustpilot.com",
    "appstore_rss": "apps.apple.com",
    "sec_fulltext": "sec.gov",
    "federal_register": "federalregister.gov",
    "usaspending": "usaspending.gov",
    "muse_jobs": "themuse.com",
}

# Hosts a lane's permalink is allowed to live on. A match is exact or a
# subdomain of the listed host, never a suffix match on the raw string
# (``evil-sec.gov.attacker.com`` and ``notsec.gov`` both fail).
_NEW_SOURCE_URL_HOSTS = {
    "trustpilot": ("trustpilot.com",),
    "appstore_rss": ("apps.apple.com", "itunes.apple.com"),
    "sec_fulltext": ("sec.gov",),
    "federal_register": ("federalregister.gov",),
    "usaspending": ("usaspending.gov",),
    "muse_jobs": ("themuse.com",),
}

# The full skip vocabulary. EVERY parser returns EVERY key, including the ones
# it cannot produce, so a clean parse prints an explicit all-zero ledger. An
# empty dict would be ambiguous between "nothing was dropped" and "nothing was
# counted", and the whole point of this ledger is that those must not print the
# same thing.
NEW_SOURCE_SKIP_REASONS = ("malformed_item", "not_a_review", "no_quote", "no_url",
                           "bad_url", "no_date", "rating_above_floor")

_DEMAND_MAX_STARS = 2          # 1 and 2 star only: the complaint stream, not the review stream
_TITLE_DISPLAY_CHARS = 120     # display truncation ONLY, mirroring the roster lane; text is never cut
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>",
                              re.IGNORECASE | re.DOTALL)
_BREAKISH_RE = re.compile(r"</?(br|p|div|li|tr|h[1-6])\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def _html_to_text(v) -> str:
    """HTML body (a Muse job description, a Trustpilot review, an EDGAR highlight) to plain text.

    Script and style BODIES are removed outright rather than tag-stripped, because stripping only the
    tags would splice their contents into the quote. Block level tags become spaces so words do not
    weld together, remaining tags are dropped, entities are unescaped ONCE (unescaping twice is how
    ``&amp;lt;script&amp;gt;`` becomes markup again), and the result goes through _clean_text."""
    if not isinstance(v, str) or not v:
        return _clean_text(v)
    t = _SCRIPT_STYLE_RE.sub(" ", v)
    t = _BREAKISH_RE.sub(" ", t)
    t = _TAG_RE.sub(" ", t)
    return _clean_text(_html.unescape(t))


def _norm_date(v) -> str:
    """Any of the six sources' date spellings to ISO-Z, or "" when it cannot be read.

    Accepts epoch seconds, a full ISO timestamp (with or without milliseconds and Z), a bare
    ``YYYY-MM-DD``, and an RFC 2822 date. Returns "" rather than guessing, and the caller then skips
    that item with a counted ``no_date``: a demand card whose evidence has no date cannot be aged,
    ranked for freshness, or checked by a reader."""
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, (int, float)):
        return _epoch_to_iso(v)
    if isinstance(v, dict):                       # the iTunes {"label": "..."} wrapper
        return _norm_date(v.get("label"))
    if not isinstance(v, str):
        return ""
    t = _clean_text(v)
    if not t:
        return ""
    try:
        return iso(parse_ts(t))
    except Exception:
        pass
    try:
        return iso(parsedate_to_datetime(t))
    except Exception:
        return ""


def _rss_label(v) -> str:
    """The text inside an iTunes RSS node, which is ``{"label": "..."}`` about half the time.

    A list of nodes (``content`` is a list when Apple ships both the text and the html rendering)
    resolves to the FIRST node carrying a plain-text label, so the html twin never wins over the
    text one."""
    if isinstance(v, dict):
        return _clean_text(v.get("label"))
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return _clean_text(v)
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                attrs = item.get("attributes")
                if isinstance(attrs, dict) and str(attrs.get("type", "")).lower() == "text":
                    got = _clean_text(item.get("label"))
                    if got:
                        return got
        for item in v:
            got = _rss_label(item)
            if got:
                return got
    return ""


def _as_int(v):
    """An untrusted numeric field to int, or None. A bool is not a rating."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        return _as_int(v.get("label"))
    if isinstance(v, str):
        t = v.strip()
        if t.lstrip("-").isdigit():
            return int(t)
    return None


def _first(d: dict, *names):
    """The first present, non-empty value among several spellings of one field."""
    for n in names:
        if n in d:
            v = d.get(n)
            if v not in (None, "", [], {}):
                return v
    return None


def _new_result(lane: str, signals: list, pulled: int, reasons: dict,
                errors=None) -> dict:
    """The uniform return of every new-source parser.

    ``skipped_reasons`` always carries the FULL vocabulary, zeros included (see
    NEW_SOURCE_SKIP_REASONS), and ``skipped`` is their sum, so ``pulled == kept + skipped`` is an
    invariant a test can assert: nothing may leave a parser uncounted."""
    counts = {k: int(reasons.get(k, 0) or 0) for k in NEW_SOURCE_SKIP_REASONS}
    return {"lane": lane, "origin": NEW_SOURCE_ORIGINS.get(canonical_lane(lane), lane),
            "signals": signals, "pulled": int(pulled), "kept": len(signals),
            "skipped": sum(counts.values()), "skipped_reasons": counts,
            "errors": [str(e)[:300] for e in (errors or [])]}


def _demand_signal(lane: str, url: str, quote: str, ts: str, title: str,
                   signal: str, extra: dict | None = None) -> dict:
    """One origin-tagged DEMAND signal in the shape the existing lanes emit.

    ``text`` and ``pain_evidence`` hold the same verbatim quote: ``text`` because that is the field
    every other lane fills and the clustering layer reads, ``pain_evidence`` because that is the name
    the demand card carries it under (reference/collect.md, Lane D). Neither is truncated."""
    origin = NEW_SOURCE_ORIGINS.get(canonical_lane(lane), lane)
    out = {
        "source": origin,
        "origin": origin,
        "origin_source": origin,
        "lane": lane,
        "side": "demand",
        "url": url,
        "signal": signal,
        "ts": ts,
        "title": title[:_TITLE_DISPLAY_CHARS],
        "text": quote,
        "pain_evidence": quote,
    }
    if extra:
        for k, v in extra.items():
            if v not in (None, "", [], {}):
                out[k] = v
    return out


def _emit_demand(lane, reasons, *, url_raw, quote, date_raw, title, signal, extra=None):
    """Apply the three hard demand requirements to one item and either build its signal or count it.

    Order matters for the ledger: a missing url and a rejected url are DIFFERENT diagnoses (one says
    the source gave us nothing, the other says the source gave us something we refuse to publish), so
    they are counted apart. Returns the signal or None."""
    if not quote:
        reasons["no_quote"] = reasons.get("no_quote", 0) + 1
        return None
    raw = url_raw if isinstance(url_raw, str) else ""
    if not raw.strip():
        reasons["no_url"] = reasons.get("no_url", 0) + 1
        return None
    url = safe_url(raw, _NEW_SOURCE_URL_HOSTS.get(lane))
    if not url:
        reasons["bad_url"] = reasons.get("bad_url", 0) + 1
        return None
    ts = _norm_date(date_raw)
    if not ts:
        reasons["no_date"] = reasons.get("no_date", 0) + 1
        return None
    return _demand_signal(lane, url, quote, ts, title or quote, signal, extra)


def _rows_of(raw, *paths, error_fields=("error", "errors", "detail", "message")):
    """Find the list of records inside a raw response, or report why there is none.

    Returns ``(rows, error)``. ``paths`` are dotted lookups tried in order (``data.reviews``), and a
    bare list is accepted as the rows themselves. A dict that carries an error field, a non-2xx
    status, or ``success: false`` is a FAILURE; a well-formed response whose row list is simply
    absent is an honest EMPTY, because "the API answered and had nothing" is a real day and must not
    be reported as an outage."""
    raw, err = _decode_payload(raw)
    if err is not None:
        return [], err
    if isinstance(raw, list):
        return raw, None
    if not isinstance(raw, dict):
        return [], "malformed payload: expected an object, got %s" % type(raw).__name__
    for f in error_fields:
        v = raw.get(f)
        if v:
            detail = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            return [], "%s: %s" % (f, detail[:200])
    if raw.get("success") is False:
        return [], "success: false"
    st = _http_status_error(raw)
    if st is not None:
        return [], st
    for path in paths:
        node = raw
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list):
            return node, None
        if isinstance(node, dict):
            return [node], None
    return [], None


# --------------------------------------------------------------------------- 1. trustpilot
# https://www.trustpilot.com/review/<DOMAIN>?stars=1&stars=2 via the Firecrawl REST API v2
# /v2/scrape with proxy "stealth". The star filter is SERVER SIDE, so the page IS the complaint
# stream: 35,166 chars in 0.9s, 20 reviews, each with a permalink and same-day freshness (measured).
#
# WITHOUT the stealth proxy roughly HALF of calls return a 153 to 170 byte interstitial reading
# "Verifying your connection". That body parses fine and contains no reviews, so to any parser that
# only asks "how many reviews did I get" it is indistinguishable from a brand with no complaints.
# It is detected here and returned as an ERROR, which is the same fail-open lesson as arctic-shift:
# the dangerous failure is not the one that raises, it is the one that returns a tidy empty answer.
_TP_INTERSTITIAL_RE = re.compile(
    r"verif(y|ying)\s+(your\s+)?(connection|browser)|just a moment|checking your browser",
    re.IGNORECASE)
_TP_INTERSTITIAL_MAX_CHARS = 4000


def _trustpilot_interstitial(raw) -> str | None:
    """The bot-interstitial error for a Trustpilot scrape, or None. Reads whichever body field the
    scrape envelope carries; a SHORT body that matches the challenge wording is the signature."""
    bodies = []
    for node in (raw, raw.get("data") if isinstance(raw, dict) else None):
        if isinstance(node, dict):
            for f in ("markdown", "html", "rawHtml", "content", "text", "body"):
                v = node.get(f)
                if isinstance(v, str) and v.strip():
                    bodies.append(v)
    for b in bodies:
        if _TP_INTERSTITIAL_RE.search(b) and len(b) <= _TP_INTERSTITIAL_MAX_CHARS:
            return ("bot interstitial (%d chars): rerun with proxy=stealth" % len(b))
    return None


def parse_trustpilot(raw, max_stars: int = _DEMAND_MAX_STARS) -> dict:
    """RAW Firecrawl scrape of a Trustpilot 1-2 star business-unit page -> demand signals.

    One review object is expected to carry a permalink, a star rating, a date and a body under any of
    the field spellings seen in the wild. The body is the pain quote and it is kept whole.

    A rating ABOVE ``max_stars`` is counted under ``rating_above_floor`` rather than dropped
    silently, so the day the server side star filter stops being applied, the ledger says so instead
    of the lane quietly filling with 5 star praise. A review with NO rating is kept, because the
    filter is server side and its absence is a field the page does not always render, not evidence
    that the review is positive."""
    lane = "trustpilot"
    reasons: dict = {}
    if isinstance(raw, dict):
        inter = _trustpilot_interstitial(raw)
        if inter:
            return _new_result(lane, [], 0, reasons, [inter])
    rows, err = _rows_of(raw, "reviews", "data.reviews", "data.json.reviews", "data.extract.reviews",
                         "json.reviews", "results")
    if err:
        return _new_result(lane, [], 0, reasons, [err])
    signals = []
    for r in rows:
        if not isinstance(r, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        stars = _as_int(_first(r, "stars", "rating", "starRating", "score"))
        if stars is not None and stars > max_stars:
            reasons["rating_above_floor"] = reasons.get("rating_above_floor", 0) + 1
            continue
        body = _html_to_text(_first(r, "text", "body", "review", "content", "reviewBody") or "")
        heading = _clean_text(_first(r, "title", "heading", "reviewTitle") or "")
        sig = _emit_demand(
            lane, reasons,
            url_raw=_first(r, "permalink", "url", "link", "reviewUrl"),
            quote=body or heading,
            date_raw=_first(r, "date", "publishedDate", "createdAt", "publishedAt", "dates"),
            title=heading or body,
            signal=("%d star" % stars) if stars is not None else "1-2 star review",
            extra={"rating": stars, "author": _clean_text(_first(r, "author", "consumer",
                                                                 "reviewer") or "")})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(rows), reasons)


# --------------------------------------------------------------------------- 2. apple app store rss
# https://itunes.apple.com/us/rss/customerreviews/page=N/id=<TRACKID>/sortby=mostrecent/json
# Keyless plain HTTPS, no MCP, no key, measured 0.10 to 0.59s, exactly 50 reviews per page, 10 pages
# max (page=11 is HTTP 400), untruncated bodies up to 1527 chars. The cheapest source measured.
#
# The feed is NOT star filtered, so the 1-2 star filter is OURS to apply and every review it removes
# is counted. Apple's FIRST entry is the app itself, not a review: it carries no im:rating, and it is
# counted under ``not_a_review`` rather than being mistaken for a malformed review.
def parse_appstore_rss(raw, max_stars: int = _DEMAND_MAX_STARS) -> dict:
    """RAW iTunes customer-reviews RSS json -> demand signals (1-2 star only).

    A well-formed feed with no ``entry`` at all is an honest EMPTY, not a failure: Apple ships that
    for an app with no reviews on the requested page. A payload with no ``feed`` at all is a failure,
    which is what an HTTP 400 past page 10 degrades to."""
    lane = "appstore_rss"
    reasons: dict = {}
    raw, err = _decode_payload(raw)
    if err is not None:
        return _new_result(lane, [], 0, reasons, [err])
    if not isinstance(raw, dict):
        return _new_result(lane, [], 0, reasons,
                           ["malformed payload: expected an object, got %s" % type(raw).__name__])
    for f in ("error", "errorMessage", "errors"):
        if raw.get(f):
            v = raw.get(f)
            detail = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            return _new_result(lane, [], 0, reasons, ["%s: %s" % (f, detail[:200])])
    st = _http_status_error(raw)
    if st is not None:
        return _new_result(lane, [], 0, reasons, [st])
    feed = raw.get("feed")
    if not isinstance(feed, dict):
        return _new_result(lane, [], 0, reasons, ["malformed payload: no 'feed' object"])
    entries = feed.get("entry")
    if entries is None:
        return _new_result(lane, [], 0, reasons)      # honest empty page, the feed answered
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return _new_result(lane, [], 0, reasons,
                           ["malformed payload: 'entry' is %s, not a list"
                            % type(entries).__name__])
    signals = []
    for e in entries:
        if not isinstance(e, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        rating = _as_int(e.get("im:rating"))
        if rating is None:
            reasons["not_a_review"] = reasons.get("not_a_review", 0) + 1
            continue                                  # the app entry Apple puts first
        if rating > max_stars:
            reasons["rating_above_floor"] = reasons.get("rating_above_floor", 0) + 1
            continue
        link = e.get("link")
        href = ""
        if isinstance(link, dict):
            href = ((link.get("attributes") or {}) if isinstance(link.get("attributes"), dict)
                    else {}).get("href", "")
        elif isinstance(link, list):
            for ln in link:
                attrs = ln.get("attributes") if isinstance(ln, dict) else None
                if isinstance(attrs, dict) and attrs.get("href"):
                    href = attrs.get("href")
                    break
        # Apple gives EVERY review on a page the same app-level href, so the raw href is not an
        # identity. Measured 2026-08-29 on a real feed (id 940247939): 46 kept reviews, 46 distinct
        # bodies, ONE distinct url, and run.signal_key joins on the url, so all 46 collapsed into a
        # single reconcilable signal. The per-review id lives in `entry.id.label`, so it is appended
        # as a fragment: the server ignores a fragment, which keeps the link resolving to the real
        # page it always resolved to, while naming WHICH review on that page this signal is. Same
        # reasoning as the constructed EDGAR permalink below, and the id is used only when it is a
        # bare token, never when Apple puts a whole url there (the app entry does).
        rid = _clean_text(_rss_label(e.get("id")))
        if href and rid and re.match(r"^[A-Za-z0-9_-]{1,64}$", rid) and "#" not in href:
            href = "%s#%s" % (href, rid)
        body = _rss_label(e.get("content"))
        heading = _rss_label(e.get("title"))
        author = ""
        au = e.get("author")
        if isinstance(au, dict):
            author = _rss_label(au.get("name"))
        sig = _emit_demand(
            lane, reasons,
            url_raw=href,
            quote=body or heading,
            date_raw=e.get("updated"),
            title=heading or body,
            signal="%d star" % rating,
            extra={"rating": rating, "author": author,
                   "app_version": _rss_label(e.get("im:version"))})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(entries), reasons)


# --------------------------------------------------------------------------- 3. sec edgar full text
# https://efts.sec.gov/LATEST/search-index?q=<PHRASE>&forms=8-K , keyless raw HTTP with a
# User-Agent, measured HTTP 200 in 0.35s. It searches filing TEXT, so pain language ("material
# weakness", "manual", "labor shortage") is findable ACROSS companies instead of per ticker.
#
# The permalink is CONSTRUCTED, never taken from the payload: EDGAR archive urls are a pure function
# of cik plus accession number, so building it is both safer than trusting a field and possible for
# every hit. An accession that does not match the exact EDGAR shape yields no url and the hit is
# counted under bad_url rather than pointed at a guess.
def _edgar_url(accession: str, cik: str, doc: str = "") -> str:
    """The canonical EDGAR archive url for one filing, or "" when the accession is not EDGAR shaped."""
    acc = _clean_text(accession)
    if not _ACCESSION_RE.match(acc):
        return ""
    c = _clean_text(cik).lstrip("0")
    if not c.isdigit():
        return ""
    flat = acc.replace("-", "")
    tail = _clean_text(doc)
    if not tail or "/" in tail or ".." in tail or not re.match(r"^[A-Za-z0-9._-]+$", tail):
        tail = acc + "-index.htm"
    return safe_url("https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (c, flat, tail),
                    _NEW_SOURCE_URL_HOSTS["sec_fulltext"])


def _edgar_matched_text(hit: dict) -> str:
    """The verbatim matched text of one full-text hit, from ``highlight`` or an explicit field.

    Highlight fragments arrive wrapped in ``<em>`` markup, so they go through the html reducer;
    several fragments join with a space, in payload order, and nothing is dropped."""
    hl = hit.get("highlight")
    frags = []
    if isinstance(hl, dict):
        for v in hl.values():
            if isinstance(v, list):
                frags += [x for x in v if isinstance(x, str)]
            elif isinstance(v, str):
                frags.append(v)
    elif isinstance(hl, list):
        frags += [x for x in hl if isinstance(x, str)]
    if frags:
        return _html_to_text(" ".join(frags))
    src = hit.get("_source") if isinstance(hit.get("_source"), dict) else {}
    return _html_to_text(_first(hit, "matched_text", "text") or
                         _first(src, "matched_text", "text", "description") or "")


def parse_sec_fulltext(raw) -> dict:
    """RAW efts.sec.gov full-text search response -> demand signals.

    A hit with no matched text is counted under ``no_quote`` rather than emitted with the company
    name standing in for the quote. That is deliberate and it is loud on purpose: a full-text search
    whose hits carry no matched text is a query that lost its highlighting, and the honest reading of
    that day is "this lane produced nothing", not "this lane produced N nameless filings"."""
    lane = "sec_fulltext"
    reasons: dict = {}
    rows, err = _rows_of(raw, "hits.hits", "hits", "results")
    if err:
        return _new_result(lane, [], 0, reasons, [err])
    signals = []
    for h in rows:
        if not isinstance(h, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        src = h.get("_source") if isinstance(h.get("_source"), dict) else h
        ident = _clean_text(_first(h, "_id", "id") or "")
        accession, _, doc = ident.partition(":")
        accession = _clean_text(_first(src, "adsh", "accession_number") or accession)
        ciks = src.get("ciks")
        cik = ""
        if isinstance(ciks, list) and ciks:
            cik = _clean_text(ciks[0])
        elif isinstance(ciks, (str, int)):
            cik = _clean_text(ciks)
        else:
            cik = _clean_text(_first(src, "cik", "cik_number") or "")
        names = src.get("display_names")
        company = ""
        if isinstance(names, list) and names:
            company = _clean_text(names[0])
        else:
            company = _clean_text(_first(src, "company_name", "entity", "display_name") or "")
        forms = src.get("root_forms")
        form = ""
        if isinstance(forms, list) and forms:
            form = _clean_text(forms[0])
        form = form or _clean_text(_first(src, "file_type", "form", "form_type") or "")
        quote = _edgar_matched_text(h)
        built = _edgar_url(accession, cik, doc)
        if accession and not built:
            # We HAD an accession and refused to build a url from it (it is not EDGAR shaped, or the
            # hit names no cik). That is a different diagnosis from a hit that named no filing at
            # all, so it is counted apart instead of both collapsing into "no url".
            reasons["bad_url"] = reasons.get("bad_url", 0) + 1
            continue
        sig = _emit_demand(
            lane, reasons,
            url_raw=built,
            quote=quote,
            date_raw=_first(src, "file_date", "filed_date", "filing_date", "date"),
            title=("%s %s" % (company, form)).strip() or quote,
            signal=("%s %s" % (form or "filing", company)).strip(),
            extra={"company": company, "form": form, "accession": accession, "cik": cik})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(rows), reasons)


# --------------------------------------------------------------------------- 4. federal register
# https://www.federalregister.gov/api/v1/documents.json , keyless, measured 200 in 0.09 to 0.12s,
# newest RULE published same day, the significant condition honored SERVER SIDE. This is the source
# that supplies the dated, mandatory, industry-wide why_now a demand card needs: a rule with a
# publication date and a comment deadline is a need that arrives on a schedule, not a trend.
def parse_federal_register(raw) -> dict:
    """RAW documents.json -> demand signals. The quote is the ABSTRACT when the document has one and
    the TITLE otherwise; both are the document's own words, which is what a verbatim quote means for
    a rule. ``html_url`` is the permalink and is host pinned like every other lane."""
    lane = "federal_register"
    reasons: dict = {}
    rows, err = _rows_of(raw, "results", "documents")
    if err:
        return _new_result(lane, [], 0, reasons, [err])
    signals = []
    for d in rows:
        if not isinstance(d, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        title = _clean_text(d.get("title"))
        abstract = _clean_text(d.get("abstract"))
        agencies = []
        for a in (d.get("agencies") or []):
            if isinstance(a, dict):
                nm = _clean_text(_first(a, "name", "raw_name", "id") or "")
            else:
                nm = _clean_text(a)
            if nm:
                agencies.append(nm)
        dtype = _clean_text(d.get("type"))
        sig = _emit_demand(
            lane, reasons,
            url_raw=_first(d, "html_url", "url", "pdf_url"),
            quote=abstract or title,
            date_raw=_first(d, "publication_date", "effective_on", "signing_date", "date"),
            title=title or abstract,
            signal=" ".join(x for x in [dtype or "document", ", ".join(agencies)] if x),
            extra={"document_number": _clean_text(d.get("document_number")),
                   "doc_type": dtype, "agencies": agencies,
                   "significant": d.get("significant"),
                   "comments_close_on": _clean_text(d.get("comments_close_on"))})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(rows), reasons)


# --------------------------------------------------------------------------- 5. usaspending
# https://api.usaspending.gov awards search, keyless POST. A demand signal with a BUDGET attached: a
# federal award whose description names the manual work is a customer who has already paid for the
# workaround, and the amount is the size of the pain in dollars.
#
# The permalink is constructed from generated_internal_id, the id usaspending's own award pages use;
# an award row with neither that id nor an explicit usaspending url is counted under no_url rather
# than emitted unattributed, because an award nobody can look up is not evidence.
def _usaspending_url(rec: dict) -> str:
    """The CANDIDATE url for one award: constructed from generated_internal_id when the id is
    well formed, otherwise whatever url the record named. It is deliberately returned unvalidated,
    because _emit_demand does the validation for every lane in one place; a url this function had
    already rejected would reach the ledger as "no url at all" instead of "we refused this url"."""
    gid = _clean_text(_first(rec, "generated_internal_id", "generated_unique_award_id") or "")
    if gid and re.match(r"^[A-Za-z0-9._-]+$", gid):
        return "https://www.usaspending.gov/award/%s/" % gid
    u = _first(rec, "url", "link", "permalink")
    return u if isinstance(u, str) else ""


def _money(v) -> str:
    """An award amount rendered for the signal line, or "" when it is not a number."""
    if isinstance(v, bool) or v is None:
        return ""
    if isinstance(v, str):
        t = v.replace(",", "").replace("$", "").strip()
        try:
            v = float(t)
        except ValueError:
            return ""
    if isinstance(v, (int, float)):
        return "$%s" % format(float(v), ",.2f")
    return ""


def parse_usaspending(raw) -> dict:
    """RAW usaspending awards response -> demand signals. The award DESCRIPTION is the quote: it is
    the government's own words for the work being bought, which is exactly the pain statement."""
    lane = "usaspending"
    reasons: dict = {}
    rows, err = _rows_of(raw, "results", "data")
    if err:
        return _new_result(lane, [], 0, reasons, [err])
    signals = []
    for r in rows:
        if not isinstance(r, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        recipient = _clean_text(_first(r, "Recipient Name", "recipient_name", "recipient") or "")
        desc = _clean_text(_first(r, "Description", "description",
                                  "transaction_description") or "")
        agency = _clean_text(_first(r, "Awarding Agency", "awarding_agency",
                                    "awarding_toptier_agency_name") or "")
        amount = _first(r, "Award Amount", "award_amount", "total_obligation", "amount")
        money = _money(amount)
        sig = _emit_demand(
            lane, reasons,
            url_raw=_usaspending_url(r),
            quote=desc,
            date_raw=_first(r, "Start Date", "start_date", "Action Date", "action_date",
                            "Last Modified Date", "date"),
            title=("%s %s" % (recipient, money)).strip() or desc,
            signal=" ".join(x for x in [money, agency] if x) or "federal award",
            extra={"recipient": recipient, "agency": agency, "amount": amount,
                   "award_id": _clean_text(_first(r, "Award ID", "award_id") or "")})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(rows), reasons)


# --------------------------------------------------------------------------- 6. the muse jobs
# https://www.themuse.com/api/public/jobs , keyless, measured 200, 155 KB, 0.26s. The only reachable
# job source whose DEFAULT population is non-tech (Walmart, CVS, Eaton, Griffith Foods), which is
# the point: Lane D exists to find pain OUTSIDE tech, and every other job API answers with startups.
#
# A company hiring a human full time to do a repetitive task is a pain it already PAYS for, so the
# job DESCRIPTION is the quote. It arrives as HTML and is reduced to text with script and style
# bodies removed.
def parse_muse_jobs(raw) -> dict:
    """RAW Muse public jobs response -> demand signals."""
    lane = "muse_jobs"
    reasons: dict = {}
    rows, err = _rows_of(raw, "results", "jobs")
    if err:
        return _new_result(lane, [], 0, reasons, [err])
    signals = []
    for j in rows:
        if not isinstance(j, dict):
            reasons["malformed_item"] = reasons.get("malformed_item", 0) + 1
            continue
        name = _clean_text(_first(j, "name", "title") or "")
        company = ""
        co = j.get("company")
        if isinstance(co, dict):
            company = _clean_text(_first(co, "name", "short_name") or "")
        elif isinstance(co, str):
            company = _clean_text(co)
        locations = []
        for loc in (j.get("locations") or []):
            nm = _clean_text(loc.get("name")) if isinstance(loc, dict) else _clean_text(loc)
            if nm:
                locations.append(nm)
        refs = j.get("refs") if isinstance(j.get("refs"), dict) else {}
        body = _html_to_text(j.get("contents"))
        sig = _emit_demand(
            lane, reasons,
            url_raw=_first(refs, "landing_page", "url") or _first(j, "landing_page", "url"),
            quote=body or name,
            date_raw=_first(j, "publication_date", "published_date", "date"),
            title=" ".join(x for x in [name, company] if x) or body,
            signal=" ".join(x for x in [company or "employer", ", ".join(locations)] if x),
            extra={"company": company, "locations": locations, "role": name})
        if sig is not None:
            signals.append(sig)
    return _new_result(lane, signals, len(rows), reasons)


NEW_SOURCE_PARSERS = {
    "trustpilot": parse_trustpilot,
    "appstore_rss": parse_appstore_rss,
    "sec_fulltext": parse_sec_fulltext,
    "federal_register": parse_federal_register,
    "usaspending": parse_usaspending,
    "muse_jobs": parse_muse_jobs,
}

# The config spells these lanes with hyphens and two of them under different names entirely.
# Measured 2026-08-29: of the six demand lanes, only trustpilot and usaspending matched by string,
# so `appstore-rss`, `federal-register`, `sec-edgar-fts` and `the-muse` could be enabled in
# watchlist.json and never reach a parser. The unknown-lane branch below records that as a failed
# pull rather than losing it silently, which is why it was findable at all, but a lane that can only
# ever fail is not wired. One lane, one identity: names are normalized before lookup and the two
# genuinely different spellings are aliased here, next to the table they alias into.
_LANE_ALIASES = {
    "sec_edgar_fts": "sec_fulltext",
    "the_muse": "muse_jobs",
    "muse": "muse_jobs",
    "appstore": "appstore_rss",
    "apple_app_store": "appstore_rss",
}


def canonical_lane(lane) -> str:
    """The registry key a config or payload name refers to. Case and separator insensitive."""
    key = str(lane or "").strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    return _LANE_ALIASES.get(key, key)


def lane_parser(lane):
    """The parser for a lane name in any accepted spelling, or None if there is genuinely none."""
    return NEW_SOURCE_PARSERS.get(canonical_lane(lane))


def collect_new_source(lane: str, raw, run_id: str | None = None, now=None,
                       attempts: int | None = None) -> dict:
    """One new demand lane: parse the RAW response, emit origin-tagged signals and ONE pulls record.

    The pulls record names the lane's HOST origin, which is the same string every emitted signal
    carries, so the yield engine's numerator and denominator key on the same name. On a FAILED lane
    it is a failed_pull (error, observed False, attempts, outcome) and therefore goes to the
    pull-errors ledger, never to the denominator.

    ``last_run`` staleness is deliberately NOT applied here. A durable unmet pain does not expire on
    a news half life (the same reason demand_freshness_mode defaults to neutral), so dropping a two
    week old 1 star review as "stale" would delete most of what this lane exists to find. Freshness
    is scored downstream, from the ``ts`` every signal is required to carry.

    There is deliberately NO ``cfg`` parameter. One used to exist, collect_sources passed ``cfg=cfg``
    into it, and the function never read it once: the string appeared exactly twice in the whole
    file, in the signature and at that call site. Removed 2026-08-29. The star floor the demand
    parsers apply is ``_DEMAND_MAX_STARS``, reachable as each parser's explicit ``max_stars``
    argument; it is NOT a config knob, because no key for it exists in lib.DEFAULT_CONFIG, in
    CONFIG.md, or in verify_config.py. If it should become one, add it in all of those places at
    once, rather than restoring a parameter here that would be the only route to a setting nothing
    documents and nothing validates."""
    now = now or now_utc()
    run_id = run_id or ("daily-%s" % now.date().isoformat())
    parser = lane_parser(lane)
    origin = NEW_SOURCE_ORIGINS.get(canonical_lane(lane), lane)
    if parser is None:
        err = "unknown source lane: %s" % str(lane)[:60]
        return {"signals": [],
                "pulls": [failed_pull({"source": origin, "lane": str(lane)[:60]}, err,
                                      run_id, now, attempts=1, outcome="unknown_lane")],
                "filtered": {"source": origin, "lane": str(lane)[:60], "pulled": 0, "kept": 0,
                             "skipped": 0, "skipped_reasons": {}, "error": err}}
    res = parser(raw)
    n_attempts = attempts if attempts is not None else _envelope_attempts(raw)
    if res["errors"]:
        detail = "; ".join(res["errors"])[:300]
        pulls = [failed_pull({"source": origin, "lane": lane}, detail, run_id, now,
                             attempts=n_attempts, outcome=_envelope_outcome(raw))]
        signals = []
    else:
        detail = None
        signals = res["signals"]
        pulls = [{"run_id": run_id, "ts": iso(now), "source": origin, "lane": lane,
                  "pulled": res["pulled"], "kept": res["kept"]}]
        if n_attempts > 1:                       # same rule as the community lanes above
            pulls[0]["attempts"] = n_attempts
    filtered = {"source": origin, "lane": lane, "pulled": res["pulled"], "kept": len(signals),
                "skipped": res["skipped"], "skipped_reasons": res["skipped_reasons"],
                "attempts": n_attempts, "error": detail}
    return {"signals": signals, "pulls": pulls, "filtered": filtered}


def collect_sources(roster=None, roster_responses: dict | None = None,
                    community: dict | None = None, cfg: dict | None = None, last_run=None,
                    run_id: str | None = None, now=None,
                    new_sources: dict | None = None) -> dict:
    """Run the roster loop + every community lane, returning the combined origin-tagged signals and
    the full pulls-log batch (the yield denominator). Additive to the broad keyword search (kept in
    the SKILL layer; its candidate clusters still arrive via process()). ``community`` maps a source
    name to its NORMALIZED items, e.g. ``{"v2ex": parse_v2ex(raw), "linux.do": parse_rss(xml)}``.

    ``new_sources`` maps a NEW demand lane name (NEW_SOURCE_PARSERS) to its RAW response, e.g.
    ``{"trustpilot": <firecrawl scrape>, "appstore_rss": <itunes json>}``. Those lanes are parsed
    here rather than upstream, so their failures are structural facts instead of an empty list."""
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
    new_sources = new_sources if isinstance(new_sources, dict) else {}
    for lane, raw in new_sources.items():
        n = collect_new_source(lane, raw, run_id=run_id, now=now)
        signals += n["signals"]
        pulls += n["pulls"]
        filtered[str(lane)] = n["filtered"]
    return {"signals": signals, "pulls": pulls, "run_id": run_id, "filtered": filtered}
