#!/usr/bin/env python3
"""daily-hotspots shared library — deterministic primitives, stdlib only.

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
        "dedup_cosine_threshold": 0.83,
        "dedup_simhash_hamming": 3,
        "lookback_days": 7,
        "resurface_score_jump": 15,
        "samples_cap": 30,
        "fading_quiet_days": 5,
    },
    "push": {"channel": "discord-relay", "max_per_day": 5},
    "delegation": {"market-intel": {"enabled": True, "scale": "standard", "daily_cap": 4}},
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


def load_config(explicit_path: str | None = None) -> dict:
    """Probe for watchlist.json; deep-merge over DEFAULT_CONFIG. Never raises on absence —
    a missing companion repo degrades to the built-in default set (documented behavior)."""
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
            return _deep_merge(DEFAULT_CONFIG, user)
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
    stop-word filtered, dedup-preserving order, capped. Good enough for a canonical_key —
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
