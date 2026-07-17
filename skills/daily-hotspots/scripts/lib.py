#!/usr/bin/env python3
"""daily-hotspots shared library, deterministic primitives, stdlib only.

Everything here is a PURE function (no clock, no network) unless explicitly noted, so the
acceptance-gate pytest suite can byte-compare outputs (T1/T2/T3). Network/MCP collection lives
in the SKILL.md orchestration layer (the LLM), not here.

Contents: config discovery + defaults, entity normalization, canonical_key, SimHash + Hamming,
Jaccard, freshness/confidence math, small time helpers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # BOM-safe stdout on Windows GBK consoles
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------- config

# The single tunable surface lives in the companion config repo (watchlist.json). Discovery
# probe order mirrors market-intel's companion convention so daily-hotspots-config can reuse it.
CONFIG_ENV = "DAILY_HOTSPOTS_CONFIG"
CONFIG_FALLBACKS = ["~/.daily-hotspots-config", "~/.config/daily-hotspots-config"]

DEFAULT_CONFIG = {
    "schema_version": 1,
    "tracks": [
        {"id": "ai-agents", "label": "AI agents / dev tooling", "weight": 1.3,
         "keywords": ["agent", "agents", "mcp", "llm", "rag", "vibe coding", "copilot",
                      "fine-tune", "inference", "prompt", "vector", "embedding"], "enabled": True},
        {"id": "dev-tools", "label": "Developer tools", "weight": 1.1,
         "keywords": ["sdk", "cli", "framework", "devtool", "ci", "observability",
                      "database", "api", "open source", "self-host"], "enabled": True},
        {"id": "saas-niche", "label": "Vertical SaaS", "weight": 1.0,
         "keywords": ["saas", "workflow", "automation", "crm", "billing", "compliance",
                      "vertical", "b2b"], "enabled": True},
        {"id": "fintech-crypto", "label": "Fintech / Crypto", "weight": 1.0,
         "keywords": ["defi", "yield", "stablecoin", "onchain", "wallet", "payments",
                      "fintech", "trading", "tokeniz"], "enabled": True},
        {"id": "consumer-social", "label": "Consumer / social", "weight": 0.9,
         "keywords": ["creator", "social", "consumer", "mobile app", "community",
                      "marketplace"], "enabled": True},
        {"id": "hardware-iot", "label": "Hardware / IoT", "weight": 0.9,
         "keywords": ["hardware", "iot", "device", "robot", "sensor", "wearable",
                      "edge"], "enabled": True},
    ],
    "focus_topics": ["open-source replacing paid API", "solo-founder-doable",
                     "underpriced arbitrage"],
    "exclude": ["crypto pump", "memecoin", "mlm", "nsfw", "giveaway airdrop"],
    "machine_types": ["tool-saas", "marketplace", "media", "service", "hardware",
                      "arbitrage", "oss-monetization"],
    "scoring": {
        "weights": {"track_fit": 0.20, "timing": 0.25, "feasibility": 0.20,
                    "competition": 0.15, "executability": 0.20},
        "min_score_to_archive": 55,
        "min_score_to_push": 70,
        "min_score_to_deepdive": 80,
        "min_independent_sources": 2,
        "freshness_half_life_h": 72,
        "freshness_gravity": 1.8,
        # Lifecycle window-closed downweight (R4): a peak/declining/fading opportunity has a
        # narrower remaining window than an emerging one (ARCHITECTURE §3.2 / §5.5). Tunable in
        # watchlist.json; an unknown/absent stage stays neutral (1.0).
        "lifecycle_weights": {"emerging": 1.0, "peak": 0.9, "declining": 0.75, "fading": 0.55},
        # Weight-retuning regression gate (R2): re-weighting is a live tuning surface (§3.3/§8.3),
        # so a proposed weight change is re-ranked against the current one and adjudicated by the
        # deterministic gate. Drift within budget auto-passes; beyond it goes to human review; a
        # catastrophic reorder/churn blocks. All four thresholds are config-tunable.
        "weight_regression": {"max_tau": 0.25, "max_push_churn_frac": 0.20,
                              "catastrophic_tau": 0.6, "catastrophic_churn_frac": 0.5},
        # Track exploration-exploitation bandit (R6): each track is a Beta-Bernoulli arm whose
        # posterior is learned from realized reward (pushed/archived/blocked). A deterministic
        # Thompson draw yields a BOUNDED exploration-adjusted track weight in
        # [explore_weight_lo, explore_weight_hi], fed into score_opportunity(track_weight=...) which
        # re-folds it at half strength, so a promising-but-under-sampled track gets occasional lift
        # without ever overriding the evidence-driven score. Priors + bounds + rewards are tunable.
        "bandit": {"prior_alpha": 1.0, "prior_beta": 1.0,
                   "explore_weight_lo": 0.5, "explore_weight_hi": 1.5,
                   "reward_pushed": 1.0, "reward_archived": 0.6, "reward_blocked": 0.0},
        "dedup_cosine_threshold": 0.83,
        "dedup_simhash_hamming": 3,
        "lookback_days": 7,
        "resurface_score_jump": 15,
        "samples_cap": 30,
        "fading_quiet_days": 5,
    },
    "push": {"channel": "discord-relay", "max_per_day": 5},
    "delegation": {"market-intel": {"enabled": True, "scale": "standard", "daily_cap": 4}},
    # Signal-yield engine thresholds (yield.py, design spec s8/s9). Methodology is constant; every
    # knob here is tunable. floor is a weekly CONTRIBUTION count (default 0 = dead weight); a rostered
    # handle at/below it for prune_after_weeks consecutive fully-observed weeks is auto-pruned
    # (enabled=false, reversible). Report-only until min_history_days of real history (honest
    # cold-start). A missing pulls-log entry is yield=unknown, NOT 0, and is excluded from pruning.
    "yield": {"window_days": 30, "floor": 0, "prune_after_weeks": 2, "min_history_days": 7,
              "propose_add_min_count": 2, "pre_viral_faves_threshold": 500,
              "noisy_pull_min": 10, "noisy_yield_max": 0.1},
}


def find_config_dir() -> Path | None:
    p = os.environ.get(CONFIG_ENV)
    if p and Path(p).expanduser().is_dir():
        return Path(p).expanduser()
    for cand in CONFIG_FALLBACKS:
        d = Path(cand).expanduser()
        if d.is_dir():
            return d
    return None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _clamp_guardrails(cfg: dict) -> dict:
    """Guardrails only TIGHTEN, never loosen (信条 / audit LOW#1).

    A user watchlist.json deep-merges over the defaults and could otherwise *relax* a safety rail
, drop min_independent_sources to 0, blank out `exclude`, or push the score floors down to flood
    the channel. We re-impose the built-in defaults as a FLOOR: the user may make a rail stricter
    (raise a threshold, add excludes) but can never weaken it below the shipped baseline. Idempotent.
    """
    d = DEFAULT_CONFIG["scoring"]
    sc = cfg.setdefault("scoring", {})
    # safety-critical numeric floors: a user value is accepted only if it is >= the built-in default
    for k in ("min_independent_sources", "min_score_to_archive", "min_score_to_push"):
        try:
            sc[k] = max(float(sc.get(k, d[k])), float(d[k]))
        except (TypeError, ValueError):
            sc[k] = d[k]
    # ints stay ints (min_independent_sources is a count)
    sc["min_independent_sources"] = int(sc["min_independent_sources"])
    # exclude list is UNION (never lose a built-in exclusion); user may add, never remove
    user_excl = cfg.get("exclude") or []
    if not isinstance(user_excl, list):
        user_excl = []
    cfg["exclude"] = sorted(set(DEFAULT_CONFIG["exclude"]) | set(str(x) for x in user_excl))

    # §9 anti-self-deception yield rails only TIGHTEN, never loosen (same invariant as the scoring
    # rails, audit HARDEN). A user watchlist.json deep-merges over the defaults and could otherwise
    # GUT the roster in one --apply run, set yield.floor:1000 (every pulled handle reads "dead"),
    # prune_after_weeks:1 (prune on a single week), or min_history_days:0 (nullify the cold-start
    # guard). We re-impose the built-in defaults as a SAFE bound in the anti-mass-prune direction:
    #   * floor, higher floor = more handles counted dead  -> CAP at the default (0)
    #   * prune_after_weeks, fewer weeks = faster prune                 -> FLOOR at the default (2)
    #   * min_history_days, less history = weaker cold-start guard     -> FLOOR at the default (7)
    #   * window_days, the reach of the §1/§9 pre-viral prune guard; a shorter window blinds it
    #                        while decide_prune still prunes (audit HARDEN r3). The guard's unique
    #                        protection is for catches OLDER than the prune window, so it must be floored
    #                        at max(shipped default 30, prune span 7*prune_after_weeks), NOT merely at
    #                        the prune span (which would neuter it). yield._clamp_yield_guardrails
    #                        re-imposes the same rail at the engine boundary; kept here so the loaded
    #                        config never even carries a guard-blinding window.
    # The user may still make each STRICTER (prune slower / require more history / a LARGER window).
    # The remaining knobs (propose_add_min_count, human-gated, noisy_*, pre_viral) stay tunable both
    # ways. Idempotent; a malformed value resets to the shipped default.
    yd = DEFAULT_CONFIG["yield"]
    y = cfg.get("yield")
    if not isinstance(y, dict):
        y = {}
    cfg["yield"] = y
    _fl = y.get("floor", yd["floor"])
    try:
        y["floor"] = _fl if float(_fl) <= float(yd["floor"]) else yd["floor"]
    except (TypeError, ValueError):
        y["floor"] = yd["floor"]
    _pw = y.get("prune_after_weeks", yd["prune_after_weeks"])
    try:
        y["prune_after_weeks"] = _pw if float(_pw) >= float(yd["prune_after_weeks"]) \
            else yd["prune_after_weeks"]
    except (TypeError, ValueError):
        y["prune_after_weeks"] = yd["prune_after_weeks"]
    _mh = y.get("min_history_days", yd["min_history_days"])
    try:
        y["min_history_days"] = _mh if float(_mh) >= float(yd["min_history_days"]) \
            else yd["min_history_days"]
    except (TypeError, ValueError):
        y["min_history_days"] = yd["min_history_days"]
    # window_days floored at max(shipped default, prune span 7*prune_after_weeks) so the §1/§9
    # pre-viral guard can never be blinded from below; a larger window is honored.
    try:
        _floor = max(int(yd["window_days"]), 7 * int(float(y["prune_after_weeks"])))
        _wd = y.get("window_days", yd["window_days"])
        y["window_days"] = _wd if float(_wd) >= _floor else _floor
    except (TypeError, ValueError, KeyError):
        y["window_days"] = yd["window_days"]
    return cfg


def load_config(explicit_path: str | None = None) -> dict:
    """Probe for watchlist.json; deep-merge over DEFAULT_CONFIG. Never raises on absence ,
    a missing companion repo degrades to the built-in default set (documented behavior).
    Safety-critical rails are clamped to their built-in floor (guardrails only tighten)."""
    path = None
    if explicit_path:
        path = Path(explicit_path).expanduser()
    else:
        d = find_config_dir()
        if d:
            cand = d / "watchlist.json"
            if cand.is_file():
                path = cand
    if path and path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            return _clamp_guardrails(_deep_merge(DEFAULT_CONFIG, user))
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


# --------------------------------------------------------------------------- entities

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*|[一-鿿぀-ヿ가-힯]+")
_ALIAS = {
    "opendatalab-mineru": "mineru",
    "gpt4": "gpt-4", "gpt-4o": "gpt-4", "gpt4o": "gpt-4",
    "claude-3": "claude", "claude3": "claude",
    "llm": "llm", "llms": "llm",
    "agents": "agent",
}
_ENTITY_STOP = set(
    "the a an of to for and or in on with show hn ask new release launch open source how why "
    "is are be it its your you this that we i my our using use used can will just now today "
    "vs via from into out up down get got make made build built app tool".split()
)


def slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = _ALIAS.get(s, s)
    return s


def extract_entities(text: str, max_n: int = 8) -> list[str]:
    """Deterministic, dependency-free NER stand-in: lowercase content tokens, alias-folded,
    stop-word filtered, dedup-preserving order, capped. Good enough for a canonical_key ,
    the heavy lifting is the multi-signal dedup (entities + semantic + time)."""
    toks = _TOKEN_RE.findall((text or "").lower())
    out, seen = [], set()
    for t in toks:
        if (t.isascii() and len(t) < 3) or t in _ENTITY_STOP:
            continue
        t = slug(t)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_n:
            break
    return out


def canonical_key(entities: list[str], track: str) -> str:
    """Content-pure dedupe key = sorted unique entity slugs ⊕ track. NEVER includes a timestamp
    or tracking param (replay-safe). Used directly as the schedule-reminder idempotency_key."""
    ents = sorted(set(slug(e) for e in entities if e))
    base = "|".join(ents) + "::" + slug(track or "")
    return base


def opportunity_id(canonical: str) -> str:
    return "op-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- similarity

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _hash64(token: str) -> int:
    h = hashlib.md5(token.encode("utf-8")).digest()[:8]
    return int.from_bytes(h, "big")


def simhash(text: str) -> int:
    """64-bit SimHash over content tokens. Deterministic (md5-seeded), no external deps."""
    toks = [t for t in _TOKEN_RE.findall((text or "").lower())
            if not (t.isascii() and len(t) < 3) and t not in _ENTITY_STOP]
    if not toks:
        return 0
    v = [0] * 64
    for t in toks:
        hv = _hash64(slug(t))
        for i in range(64):
            v[i] += 1 if (hv >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------- scoring math

def freshness(age_hours: float, half_life_h: float = 72.0, gravity: float = 1.8) -> float:
    """Monotone non-increasing in age, range (0,1]. Exponential half-life is the spine (≈1 when
    very fresh, 0.5 at one half-life, decaying smoothly) so a strong fresh opportunity is not
    crushed before any multiplier stack. `gravity` adds a mild high-frequency tilt that slightly
    rewards the first hours and slightly steepens late decay, without tanking same-day items."""
    age_hours = max(0.0, float(age_hours))
    half = 0.5 ** (age_hours / float(half_life_h))           # 1.0 @0, 0.5 @half_life
    grav = (24.0 / (age_hours + 24.0)) ** (float(gravity) / 6.0)  # gentle: ~0.97 @4h, ~0.79 @72h
    return round(min(1.0, max(0.0, 0.8 * half + 0.2 * grav)), 6)


def confidence(n_sources: int, min_sources: int = 2) -> float:
    """Independent-source confidence multiplier. HARD-GATED upstream (n < min => culled), here
    only the multiplier mapping. Monotone non-decreasing in n_sources."""
    n = int(n_sources)
    if n <= 1:
        return 0.5      # below the red line; callers gate this out, multiplier is a floor
    if n == 2:
        return 0.8
    return 1.0


# --------------------------------------------------------------------------- dual-track routing
#
# Design §7. The pipeline splits every candidate into two tracks:
#   Track 1, opportunity card: >=2 independent origins AND score >= gate (the scored radar, and
#             the ONLY thing that becomes a scored card). Unchanged.
#   Track 2, community pulse: a SINGLE-origin signal that is (a) from a configured community source
#             (linux.do / v2ex / cn-feeds ...), (b) fresh, (c) a real track-keyword hit, and (d) not
#             excluded, is surfaced as a lightweight rumor (link + one-liner, NO score) instead of
#             being silently dropped. Everything else single-origin stays a below-source GAP.
# These are the PURE predicates the routing turns on (verify_gate.route_below_gate composes them,
# run.py wires them). Methodology is constant; every threshold here is config-tunable (信条).

# Community source classes/tags whose single-origin signals are Track-2 eligible. Config-driven via
# community_pulse.community_sources; this is the fallback when config is silent, the design's named
# lanes plus the concrete origin_source tags the collect layer emits (qbitai for the cn-feeds lane).
DEFAULT_COMMUNITY_SOURCES = ("linux.do", "v2ex", "cn-feeds", "qbitai")

# "Fresh enough to surface as a rumor" window, in hours (community_pulse.max_age_hours).
DEFAULT_PULSE_MAX_AGE_H = 72.0


def community_source_set(cfg: dict | None) -> set:
    """Lowercased set of source/origin labels that qualify a single-origin signal for Track 2.

    Reads ``community_pulse.community_sources`` (a list) when present + non-empty; otherwise the
    built-in default lane set, so recognition works even on DEFAULT_CONFIG (no community_pulse
    block)."""
    cp = ((cfg or {}).get("community_pulse") or {})
    srcs = cp.get("community_sources")
    if isinstance(srcs, list):
        got = {str(s).strip().lower() for s in srcs if str(s).strip()}
        if got:
            return got
    return set(DEFAULT_COMMUNITY_SOURCES)


def evidence_origin_labels(evidence) -> set:
    """Every lowercased origin label an evidence list carries, ``origin_source`` (the community
    attribution tag) then ``source`` then ``origin``, so a community item is recognizable no matter
    which attribution field the collector populated."""
    out: set = set()
    for e in (evidence or []):
        if not isinstance(e, dict):
            continue
        for k in ("origin_source", "source", "origin"):
            v = e.get(k)
            if v:
                out.add(str(v).strip().lower())
    return out


def is_community_signal(evidence, cfg: dict | None) -> bool:
    """True when at least one evidence item is from a configured community source (§7)."""
    return bool(evidence_origin_labels(evidence) & community_source_set(cfg))


def pulse_max_age_hours(cfg: dict | None) -> float:
    cp = ((cfg or {}).get("community_pulse") or {})
    try:
        v = float(cp.get("max_age_hours", DEFAULT_PULSE_MAX_AGE_H))
        return v if v > 0 else DEFAULT_PULSE_MAX_AGE_H
    except (TypeError, ValueError):
        return DEFAULT_PULSE_MAX_AGE_H


def is_fresh_for_pulse(age_hours_val, cfg: dict | None) -> bool:
    """Freshness gate for Track 2: the signal's age must fall within the pulse window. A missing/
    unparseable age is treated as fresh (0h) so an undated community item is not unfairly buried ,
    mirroring the renderer's neutral-freshness handling; the collect lane already dropped anything
    older than last_run, so this is a second, tunable belt."""
    try:
        a = float(age_hours_val) if age_hours_val is not None else 0.0
    except (TypeError, ValueError):
        a = 0.0
    return max(0.0, a) <= pulse_max_age_hours(cfg)


def community_pulse_eligible(card: dict, cfg: dict | None) -> bool:
    """Track-2 predicate (§7). Applied ONLY to a candidate that already FAILED the >=2-independent-
    source red line (the caller, verify_gate.route_below_gate / run.process, owns that gate): such
    a single-origin candidate becomes a community-pulse rumor iff it is (a) from a community source,
    (b) fresh, (c) a genuine track-keyword hit (``track_matched``, not the classifier's default
    fallback), and (d) not excluded. Pure, no clock, no network."""
    if not isinstance(card, dict):
        return False
    if card.get("excluded") or card.get("_excluded"):
        return False
    if not card.get("track_matched"):
        return False
    if not is_community_signal(card.get("evidence"), cfg):
        return False
    return is_fresh_for_pulse(card.get("age_hours"), cfg)


# --------------------------------------------------------------------------- time

def now_utc() -> datetime:
    """Clock seam: SCHEDULE_NOW / DAILY_HOTSPOTS_NOW override for deterministic tests/replay."""
    for var in ("DAILY_HOTSPOTS_NOW", "SCHEDULE_NOW"):
        v = os.environ.get(var)
        if v:
            return parse_ts(v)
    return datetime.now(timezone.utc)


def parse_ts(s: str) -> datetime:
    s = (s or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def age_hours(ts: str, ref: datetime | None = None) -> float:
    ref = ref or now_utc()
    try:
        return max(0.0, (ref - parse_ts(ts)).total_seconds() / 3600.0)
    except Exception:
        return 0.0
