#!/usr/bin/env python3
"""Privacy core — redact-on-ingest + egress DLP. Stdlib only, PURE, deterministic.

SYNCED PAIR: this file is a vendored copy of
    demand-mining/skills/demand-mining/scripts/redact.py
The Tier1/Tier2 core — redact() / has_pii() / pseudonymize() and every pattern below — MUST stay
byte-for-byte in step with that sibling (the ONLY deliberate divergence is the pseudonym-salt env
prefix: DEMAND_MINING_ -> DAILY_HOTSPOTS_). If you fix a matcher here, port it there and vice
versa. The daily-hotspots-only addition is the egress helper block at the BOTTOM of this file
(scrub_egress / redact_egress); it does NOT exist in the sibling and must never drift into the
shared core above the "daily-hotspots egress DLP" banner.

ACTIVE PATH IN THIS SKILL (read before trusting the shared-core prose below): daily-hotspots
collects PUBLIC frontier signals (GitHub / HN / PulseMCP / npm / HF), not private conversations, so
its run.py does NOT call redact() at ingest. The sole load-bearing privacy guarantee here is the
EGRESS DLP — scrub_egress() / redact_egress() at the bottom of this file — applied in
push_card.deliver() to the Discord digest in the last instant before it leaves the machine. The
shared Tier1/Tier2 core (redact / has_pii / pseudonymize) is vendored byte-for-byte with the sibling
so a matcher fix ports cleanly either way, and the egress helpers reuse its patterns; it is present
and importable should ingest redaction ever be wired here, but today it is NOT on the runtime path.
(In the demand-mining sibling the core IS the ingest guard — see that file's run.py — which is where
the "redaction before the model sees the message" rule below actually holds.)

Layers (cost-ascending; Tier1/Tier2 are pure-stdlib and always on):
  * Tier1 — deterministic regex + checksum: emails, phones, credit cards (Luhn-verified),
            Discord user-id / @handle / invite link, URLs, IPs.
  * Tier2 — entropy: long high-entropy tokens (API keys / secrets) → [SECRET_n].
  * Tier3 — NER (Presidio, LOCAL-only, never a third-party PII API) for names/addresses: a hook
            point (apply_ner) the skill can wire in v0.2; absent => Tier1/2 still redact.

Two anti-patterns this file exists to kill:
  1. Unified placeholders that COLLAPSE distinct entities (one "[EMAIL]" for two addresses loses who
     said what). We mint UNIQUE, stable-within-a-message placeholders: [EMAIL_1], [PHONE_2]...
     (NOTE: names/addresses are the Tier3 v0.2 NER hook and are NOT redacted yet — structured PII
     only. Do not rely on this to strip a person's name; wire apply_ner or keep raw names out.)
  2. A consistent author pseudonym that is reversible. `pseudonymize()` = HMAC-SHA256(salt, id):
     same person → same token across messages (a real clustering signal) but not invertible. The
     salt is read from secrets/env at call time and NEVER hardcoded or echoed; salt-in-repo would
     make the pseudonym as good as plaintext.

(Sibling-only: in demand-mining the need pool stores ONLY redacted, distilled items. daily-hotspots
has no such pool; its equivalent guarantee is the egress scrub on the pushed digest, see below.)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sys
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Confusable dot/at punctuation that NFKC does NOT fold (ideographic full stops etc.). Mapped to
# ASCII BEFORE NFKC so full-width / homoglyph obfuscation cannot smuggle structured PII past the
# Tier-1 regexes (e.g. bob@host。com, ｊｏｈｎ＠ｅｖｉｌ．ｃｏｍ). NFKC handles the U+FF00 full-width block.
_CONFUSABLE_PUNCT = str.maketrans({"。": ".", "｡": ".", "︒": ".", "﹒": ".", "･": ".", "‧": "."})


def _normalize(text: str) -> str:
    """Canonicalize text so obfuscated structured PII is matchable: fold confusable dots, then NFKC
    (full-width -> ASCII). Applied only inside redact()/has_pii(); the redacted output is the
    normalized form (acceptable for the distilled pool; CJK content is unchanged by NFKC)."""
    return unicodedata.normalize("NFKC", (text or "").translate(_CONFUSABLE_PUNCT))

# --------------------------------------------------------------------------- Tier-1 patterns

# Order matters: more specific patterns first so an email is not partly eaten by the URL rule.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_DISCORD_MENTION = re.compile(r"<@!?(\d{15,21})>")            # <@123...> / <@!123...>
_DISCORD_ID = re.compile(r"\b\d{17,20}\b")                    # bare snowflake (user/channel id)
_INVITE = re.compile(r"\b(?:https?://)?(?:discord\.gg|discord(?:app)?\.com/invite)/\S+",
                     re.IGNORECASE)
_URL = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_HANDLE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{2,32})\b")    # @handle (not an email local-part)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 — full 8-group form OR any "::"-compressed form (architecture Tier1 lists "IPs"). Guarded in
# the substituter so plain decimal times/ratios (colons but no "::" and not 8 hex groups) are never
# eaten. Lookaround stops partial matches inside larger word/colon runs.
_IPV6 = re.compile(
    r"(?<![\w:.])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"                                   # 8 full groups
    r"|(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}"  # :: compressed
    r")(?![\w:.])")
# phone: loose international-ish; validated by digit count to avoid eating ordinary numbers
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)")
_CCARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
# high-entropy token (Tier2): a long run of base64/hex-ish chars with no spaces
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")

# A bare ISO calendar date (YYYY-MM-DD) and a pure run of 4-digit years look like a loose phone
# (8+ digits joined by '-'/space) but are NEVER contact numbers. The phone substituter skips them so
# a date header or a '2020-2026' range is not mislabeled [PHONE_*] — which would also make the
# fail-closed has_pii() gate abort an otherwise-clean digest. A real phone survives both guards.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_year_run(v: str) -> bool:
    """True if v is nothing but 4-digit calendar years (1900-2099) joined by phone-ish separators —
    e.g. '2020-2026', '2019 2020 2021 2022'. Such a value is a date range/list in prose, not a phone;
    a real number's groups (area 3 / exchange 3 / line 4) are not all 4-digit years, so it is kept."""
    groups = re.findall(r"\d+", v)
    return bool(groups) and all(len(g) == 4 and 1900 <= int(g) <= 2099 for g in groups)


def _luhn_ok(num: str) -> bool:
    ds = [int(c) for c in re.sub(r"\D", "", num)]
    if not (13 <= len(ds) <= 19):
        return False
    s, alt = 0, False
    for d in reversed(ds):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    from collections import Counter
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


class _Minter:
    """Mints unique, stable-within-a-call placeholders per entity TYPE and per distinct VALUE.
    The same value seen twice in one message gets the same placeholder (preserves co-reference);
    two different values get [TYPE_1] / [TYPE_2] (never collapsed)."""

    def __init__(self):
        self._by_type: dict[str, dict[str, str]] = {}

    def get(self, kind: str, value: str) -> str:
        table = self._by_type.setdefault(kind, {})
        if value not in table:
            table[value] = f"[{kind}_{len(table) + 1}]"
        return table[value]


def redact(text: str, salt: bytes | None = None) -> dict:
    """Redact one message. PURE (no clock/network). Returns:
        {redacted: str, placeholders: {placeholder: type}, found: {type: count}}
    `salt` only affects pseudonymize() (handles), not the structural redaction. Email is redacted
    before @handle so an email local-part is never mistaken for a handle."""
    found: dict[str, int] = {}
    mint = _Minter()
    text = _normalize(text or "")  # fold full-width/homoglyph obfuscation before Tier-1 matching

    def bump(k):
        found[k] = found.get(k, 0) + 1

    # 1) invite links (before generic URL), 2) emails, 3) discord mentions/ids, 4) urls,
    # 5) credit cards (Luhn), 6) phones, 7) ipv4, 8) handles, 9) Tier2 secret tokens.
    def sub_invite(m):
        bump("INVITE"); return mint.get("INVITE", m.group(0))
    text = _INVITE.sub(sub_invite, text or "")

    def sub_email(m):
        bump("EMAIL"); return mint.get("EMAIL", m.group(0))
    text = _EMAIL.sub(sub_email, text)

    def sub_mention(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(1))
    text = _DISCORD_MENTION.sub(sub_mention, text)

    def sub_url(m):
        bump("URL"); return mint.get("URL", m.group(0))
    text = _URL.sub(sub_url, text)

    def sub_cc(m):
        v = m.group(0)
        if _luhn_ok(v):
            bump("CARD"); return mint.get("CARD", re.sub(r"\D", "", v))
        return v
    text = _CCARD.sub(sub_cc, text)

    def sub_phone(m):
        v = m.group(1)
        # date/year-safe: an ISO date or a pure year range/list is never a phone (see _ISO_DATE /
        # _is_year_run) — skip so a date header / '2020-2026' is not flagged by redact()/has_pii().
        if _ISO_DATE.fullmatch(v) or _is_year_run(v):
            return v
        if len(re.sub(r"\D", "", v)) >= 8:
            bump("PHONE"); return mint.get("PHONE", re.sub(r"\D", "", v))
        return v
    text = _PHONE.sub(sub_phone, text)

    def sub_ipv6(m):
        v = m.group(0)
        # require a real "::" or the full 8-group form, and at least one hex digit — so a bare
        # "::" or a decimal time/ratio is left untouched (fail-safe against over-redaction).
        if "::" not in v and v.count(":") != 7:
            return v
        if not re.search(r"[0-9A-Fa-f]", v):
            return v
        bump("IP"); return mint.get("IP", v)
    text = _IPV6.sub(sub_ipv6, text)

    def sub_ip(m):
        bump("IP"); return mint.get("IP", m.group(0))
    text = _IPV4.sub(sub_ip, text)

    def sub_handle(m):
        bump("HANDLE"); return mint.get("HANDLE", m.group(1))
    text = _HANDLE.sub(sub_handle, text)

    def sub_id(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(0))
    text = _DISCORD_ID.sub(sub_id, text)

    def sub_token(m):
        v = m.group(0)
        if _entropy(v) >= 3.5 and any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
            bump("SECRET"); return mint.get("SECRET", v)
        return v
    text = _TOKEN.sub(sub_token, text)

    placeholders = {ph: kind for kind, table in mint._by_type.items() for ph in table.values()}
    return {"redacted": text, "placeholders": placeholders, "found": found}


# --------------------------------------------------------------------------- pseudonyms

def _load_salt() -> bytes:
    """Salt discovery (NEVER hardcoded; salt-in-repo == pseudonym-in-clear). Order:
      1) DAILY_HOTSPOTS_PSEUDONYM_SALT env (raw value),
      2) the companion repo's secrets/pseudonym_hmac_salt file (gitignored, Mode B),
      3) a process-ephemeral random salt (tests/offline; pseudonyms then NOT cross-run-stable).
    The value is read but never logged/echoed."""
    v = os.environ.get("DAILY_HOTSPOTS_PSEUDONYM_SALT")
    if v:
        return v.encode("utf-8")
    d = os.environ.get("DAILY_HOTSPOTS_CONFIG")
    if d:
        p = os.path.join(os.path.expanduser(d), "secrets", "pseudonym_hmac_salt")
        try:
            if os.path.isfile(p):
                return open(p, "rb").read().strip()
        except Exception:
            pass
    # ephemeral: stable within ONE process run only (good enough for offline tests/--dry-run)
    return os.urandom(32)


_EPHEMERAL_SALT = None


def pseudonymize(user_id: str, salt: bytes | None = None) -> str:
    """author_pseudo = HMAC-SHA256(salt, user_id)[:16]. Same person → same token (a clustering
    signal); not invertible (no reverse table). right-to-erasure = forward-delete by this hash."""
    global _EPHEMERAL_SALT
    if salt is None:
        if _EPHEMERAL_SALT is None:
            _EPHEMERAL_SALT = _load_salt()
        salt = _EPHEMERAL_SALT
    mac = hmac.new(salt, (user_id or "").encode("utf-8"), hashlib.sha256).hexdigest()
    return "u_" + mac[:16]


def has_pii(text: str) -> bool:
    """Cheap egress check (DLP): True if any Tier1/Tier2 pattern still matches — used fail-closed
    before anything leaves the machine (push, delegation query). A True here BLOCKS egress.

    NOTE: has_pii treats URL and @HANDLE as PII, so it is a FAIL-CLOSED gate suitable for a raw
    conversation pool (demand-mining) — NOT for the daily-hotspots pushed digest, whose headlines
    legitimately carry evidence links and handles. For that egress path use scrub_egress() below,
    which redacts ONLY the dangerous structured types in place and leaves URL/HANDLE intact."""
    r = redact(text or "")
    return bool(r["found"])


# ===========================================================================================
# daily-hotspots egress DLP  (NOT in the demand-mining sibling — see SYNCED PAIR note at top)
# ===========================================================================================
# Why a second, narrower gate: the sibling's has_pii() flags URL and @HANDLE as PII and BLOCKS.
# That is correct for a raw private conversation pool, but daily-hotspots headlines INTENTIONALLY
# carry evidence links wrapped in <...> and may carry @handles as legitimate CONTENT — a has_pii
# egress here would block EVERY message. So the pushed-digest policy is:
#   * REDACT-IN-PLACE, never abort: one stray email in a summary is swapped for [EMAIL_1] and the
#     rest of the digest still ships (a real person's contact must not leak, but an honest day of
#     signal must not be dropped either).
#   * Scrub ONLY the dangerous structured types: EMAIL, PHONE, CARD (Luhn), SECRET (high-entropy
#     token), IP (v4/v6), DISCORD_ID (mention + bare snowflake), INVITE.
#   * LEAVE URL and @HANDLE untouched — they are content, not a leak.
#
# Collateral-damage guard: evidence URLs are STASHED before the dangerous matchers run, because a
# real link (e.g. a tweet permalink https://x.com/a/status/1234567890123456789) embeds a 19-digit
# id that the bare-snowflake DISCORD_ID rule would otherwise mangle. @handles are likewise stashed
# so a long handle is never eaten by the SECRET/token matcher. Both are restored verbatim, so a
# clean headline is returned byte-identical (no NFKC rewrite of full-width spaces either — see
# redact_egress: a message with nothing dangerous is returned as the ORIGINAL, un-normalized).

# The dangerous structured types the pushed digest must strip (URL/HANDLE deliberately excluded).
_EGRESS_TYPES = ("INVITE", "EMAIL", "DISCORD_ID", "CARD", "PHONE", "IP", "SECRET")
# _ISO_DATE / _is_year_run live in the shared core above (the phone matcher there skips dates/year
# ranges too); the egress phone rule below reuses them so a date header or '2020-2026' is never
# mislabeled [PHONE_*].


def redact_egress(text: str) -> dict:
    """Egress DLP for the pushed digest. Redact-in-place of the dangerous structured PII types only
    (see _EGRESS_TYPES); URL and @handle are left intact as legitimate headline content. PURE.

    Returns {redacted: str, found: {type: count}, changed: bool}. When nothing dangerous is found
    the ORIGINAL text is returned verbatim (changed=False) — no NFKC normalization is applied, so a
    clean full-width-spaced headline is byte-identical on the way out. Only when something IS
    scrubbed does the (normalized, redacted) form flow — acceptable, we are already altering it."""
    original = text or ""
    found: dict[str, int] = {}
    mint = _Minter()
    work = _normalize(original)  # fold full-width/homoglyph obfuscation before matching

    def bump(k):
        found[k] = found.get(k, 0) + 1

    # 1) invites BEFORE the URL stash so a discord invite (which IS a url) is scrubbed, not protected.
    def sub_invite(m):
        bump("INVITE"); return mint.get("INVITE", m.group(0))
    work = _INVITE.sub(sub_invite, work)

    # 2) emails next (an email local-part is never mistaken for a handle; emails can't sit inside an
    #    http(s) url, so stashing urls after this is safe).
    def sub_email(m):
        bump("EMAIL"); return mint.get("EMAIL", m.group(0))
    work = _EMAIL.sub(sub_email, work)

    # 3) discord MENTIONS (<@123..>) BEFORE the handle stash, else the handle regex would match the
    #    @123.. inside the mention and "protect" a real leaked id.
    def sub_mention(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(1))
    work = _DISCORD_MENTION.sub(sub_mention, work)

    # 4) STASH the content we must leave untouched, so no dangerous matcher can chew into it:
    #      * evidence urls  — protects e.g. a 19-digit tweet-status id from the DISCORD_ID rule
    #      * @handles       — protects a long handle from the SECRET/token rule
    #    Sentinels are NUL-delimited with a tiny index, so they can never look like any PII type.
    stash: list[str] = []

    def _stash(m):
        stash.append(m.group(0))
        return f"\x00H{len(stash) - 1}\x00"

    work = _URL.sub(_stash, work)
    work = _HANDLE.sub(_stash, work)

    # 5) dangerous structured types, on the URL/handle-free remainder.
    #    ORDER differs DELIBERATELY from the shared core redact() above: here IPv6 / IPv4 and the bare
    #    discord-snowflake run BEFORE the (loose) phone matcher, so a dotted IP or a 17-20 digit id is
    #    typed as [IP_*] / [DISCORD_ID_*] (an accurate egress-scrub log line) instead of being swept
    #    up as a generic [PHONE_*]. The core keeps phone-first for a raw ingest pool; the egress path
    #    wants precise typing plus the date-safe phone rule below. Redaction still happens either way
    #    — this only sharpens the TYPE, so nothing dangerous ever leaks under a wrong label.
    def sub_cc(m):
        v = m.group(0)
        if _luhn_ok(v):
            bump("CARD"); return mint.get("CARD", re.sub(r"\D", "", v))
        return v
    work = _CCARD.sub(sub_cc, work)

    def sub_ipv6(m):
        v = m.group(0)
        if "::" not in v and v.count(":") != 7:
            return v
        if not re.search(r"[0-9A-Fa-f]", v):
            return v
        bump("IP"); return mint.get("IP", v)
    work = _IPV6.sub(sub_ipv6, work)

    def sub_ip(m):
        bump("IP"); return mint.get("IP", m.group(0))
    work = _IPV4.sub(sub_ip, work)

    def sub_id(m):
        bump("DISCORD_ID"); return mint.get("DISCORD_ID", m.group(0))
    work = _DISCORD_ID.sub(sub_id, work)

    def sub_phone(m):
        v = m.group(1)
        # date-safe: skip a value shaped exactly like an ISO calendar date (see _ISO_DATE) so the
        # digest's date header is never mangled to [PHONE_*]; IP / snowflake are already gone above.
        # year-safe: skip a pure year range/list (see _is_year_run) — '2020-2026' is not a phone.
        if _ISO_DATE.fullmatch(v) or _is_year_run(v):
            return v
        if len(re.sub(r"\D", "", v)) >= 8:
            bump("PHONE"); return mint.get("PHONE", re.sub(r"\D", "", v))
        return v
    work = _PHONE.sub(sub_phone, work)

    def sub_token(m):
        v = m.group(0)
        if _entropy(v) >= 3.5 and any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
            bump("SECRET"); return mint.get("SECRET", v)
        return v
    work = _TOKEN.sub(sub_token, work)

    # 6) restore the stashed url/handle spans verbatim (reverse order is unnecessary — unique keys).
    for i, span in enumerate(stash):
        work = work.replace(f"\x00H{i}\x00", span)

    if not found:
        # nothing dangerous -> hand back the ORIGINAL untouched (no NFKC rewrite, no false "changed")
        return {"redacted": original, "found": {}, "changed": False}
    return {"redacted": work, "found": found, "changed": True}


def scrub_egress(text: str) -> str:
    """Thin string helper: the pushed-digest egress scrub, returning just the redacted text.

    Redacts ONLY dangerous structured PII (EMAIL/PHONE/CARD/SECRET/IP/DISCORD_ID/INVITE) in place
    and leaves evidence URLs and @handles intact. A clean message is returned byte-identical, so a
    caller can detect a scrub with a simple `scrub_egress(m) != m`. See redact_egress for the dict
    form (found types + changed flag) used when the caller wants to log WHAT was scrubbed."""
    return redact_egress(text)["redacted"]


def main() -> int:
    """CLI: stdin {text, user_id?} → {redacted, found, placeholders, author_pseudo?}."""
    data = json.loads(sys.stdin.read() or "{}")
    out = redact(data.get("text", ""))
    if data.get("user_id"):
        out["author_pseudo"] = pseudonymize(data["user_id"])
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
