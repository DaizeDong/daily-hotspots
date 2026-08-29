#!/usr/bin/env python3
"""daily-hotspots shared library, deterministic primitives, stdlib only.

Everything here is a PURE function (no clock, no network) unless explicitly noted, so the
acceptance-gate pytest suite can byte-compare outputs (T1/T2/T3). Network/MCP collection lives
in the SKILL.md orchestration layer (the LLM), not here.

Contents: config discovery + defaults, entity normalization, canonical_key, SimHash + Hamming,
Jaccard, freshness/confidence math, the untrusted-input gate (safe_url + the invisible-character
class every text sanitizer strips), small time helpers.
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
        # Explicit catch-all lane. A candidate that matches no track keyword used to be silently
        # filed under whatever track happened to sit first in this list (tracks[0], ai-agents), which
        # made "we could not classify this" indistinguishable from "this really is an AI-agents item".
        # `unclassified` carries NO keywords (it can never win the keyword contest, so it never steals
        # a real match) and weight 1.0 (neutral: an unclassified card is neither promoted nor buried).
        # It exists so the pipeline has a truthful place to put an unmatched card. MUST stay LAST:
        # classify.py's no-match fallback is tracks[0], and putting the catch-all first would file
        # every unmatched candidate under it before the owning module opts in.
        {"id": "unclassified", "label": "Unclassified", "weight": 1.0,
         "keywords": [], "enabled": True},
    ],
    "focus_topics": ["open-source replacing paid API", "solo-founder-doable",
                     "underpriced arbitrage"],
    "exclude": ["crypto pump", "memecoin", "mlm", "nsfw", "giveaway airdrop"],
    "machine_types": ["tool-saas", "marketplace", "media", "service", "hardware",
                      "arbitrage", "oss-monetization"],
    # ------------------------------------------------------------------ source rows (public default)
    # PUBLIC DEFAULT source rows. The operator's live watchlist.json deep-merges OVER this block, so
    # every value here is a public floor the private config may retune; nothing here is ever written
    # into the private repo. The older lanes (hackernews, gdelt, twitterapi, linux.do, v2ex,
    # cn-feeds, reddit, product-hunt, arxiv) keep their rows in that private watchlist and are not
    # restated here. This block adds the six DEMAND sources probed live on 2026-08-27, plus one
    # quarantine row for the fetcher that was found lying.
    #
    # HOW A ROW GETS PROBED, and why the keys look the way they do.
    # scripts/sourcehealth.py owns the control queries. sourcehealth.specs_from_config walks the
    # ENABLED rows below and, for each one, resolves a probe spec in this order:
    #     row["health"] if present   ->  that block IS the spec, wholesale
    #     else sourcehealth.DEFAULT_SPECS[<this row's key>]  ->  joined BY NAME
    #     else {"kind": None}        ->  state "unknown", which is the honest answer
    # So THE KEY OF EACH ROW BELOW IS THE JOIN KEY, byte for byte identical to the `name` in
    # sourcehealth.DEFAULT_SPECS. Rename one side only and the source silently becomes "unknown",
    # never "ok", which is the safe direction but still a bug: rename both.
    # The control query itself is defined ONCE, in sourcehealth.CONTROLS, and is NOT copied here.
    # Each row carries a `_control` POINTER that names the control and says in one line what it
    # asserts, so a person reading this config knows what proves the source alive without having to
    # keep a second machine-readable copy in sync. sourcehealth.py is authoritative; if `_control`
    # and CONTROLS ever disagree, CONTROLS is right and `_control` is stale prose. A row that needs
    # a DIFFERENT control declares a `health` block, which replaces the joined spec wholesale.
    #
    # The states those probes return, and why "ok" is hard to earn:
    #     transport raised, or a non-2xx status              -> "down"
    #     transport ok AND the control's assertions hold      -> "ok"
    #     transport ok AND the payload is empty or contentless-> "fail_open_suspected"  (never "ok")
    #     content returned but an assertion missed, or slow   -> "degraded"
    #     no fetcher, no control, or a control that asserts
    #       nothing at all                                    -> "unknown"  (never zeros)
    #
    # `_cost` says what one control probe plus one real pull costs. A probe nobody can afford to run
    # daily is a probe that gets switched off and then forgotten, which is the failure this whole
    # block exists to prevent.
    "sources": {
        # ---------------------------------------------------------------- demand: verbatim pain
        # Trustpilot 1 and 2 star business unit pages, via the Firecrawl REST API v2 /v2/scrape.
        # The ?stars=1&stars=2 filter is applied SERVER SIDE, so the fetched page IS the complaint
        # stream; there is no client-side filter step that could be skipped or faked.
        # Measured 2026-08-27: 35166 chars in 0.9s, 20 reviews, each with a permalink, same-day
        # freshness. Densest measured source of verbatim pain carrying dollar figures and contract
        # terms, which is why it is worth a credential.
        # OFF by default. It is the only one of the six that needs a key, and a public default
        # cannot assume the operator has a Firecrawl account. With no key the health probe reports
        # "unknown", which is a different word from "ok" on purpose.
        "trustpilot": {
            "enabled": False,
            "weight": 1.2,
            "side": "demand",
            "fetch": "firecrawl-v2-scrape",
            "requires_credential": "FIRECRAWL_API_KEY",
            "endpoint": "https://api.firecrawl.dev/v2/scrape",
            "proxy": "stealth",
            "url_template": "https://www.trustpilot.com/review/{domain}?stars=1&stars=2",
            # A block page is not an empty result. Measured 2026-08-27: without proxy=stealth about
            # half of the calls return a 153 to 170 byte body containing "Verifying your
            # connection". Anything matching this signature is DOWN plus retry, never "this vendor
            # has no complaints". sourcehealth.looks_interstitial catches it by shape.
            "block_signature": {"contains": "Verifying your connection", "byte_range": [153, 170]},
            "_control": "sourcehealth.CONTROLS['trustpilot_scrape']: scrape https://example.com "
                        "through the SAME Firecrawl endpoint with the same proxy=stealth, and "
                        "assert the body contains 'Example Domain'. It deliberately does not hit "
                        "Trustpilot, because a 1 and 2 star page can legitimately be thin while "
                        "example.com cannot. An empty body means the fetcher is lying.",
            "_cost": "one Firecrawl scrape credit per business unit per run, plus one credit per "
                     "control probe. Budget the probe: it is cheaper than a silent outage.",
        },
        # Apple App Store customer reviews RSS. Keyless plain HTTPS, no MCP in the path at all, so
        # there is no MCP layer that can swallow an error and hand back an empty success.
        # Measured 2026-08-27: 0.10s to 0.59s per page, exactly 50 reviews per page, 10 pages
        # maximum, untruncated review bodies up to 1527 chars, same-day 1 and 2 star reviews.
        # Cheapest source measured.
        # THE CAP IS UPSTREAM, NOT A BUDGET WE CHOSE: page=11 returns HTTP 400. When a pull reaches
        # page 10 the run must SAY the cap was reached rather than returning 500 reviews as if that
        # were all that exists.
        #
        # SHIPPED OFF, 2026-08-29. The 50-reviews-per-page measurement above DID NOT REPRODUCE on
        # independent re-probe, and the way it failed is the exact shape this whole round is about:
        # GET .../page=1/id=504370616/sortby=mostrecent/json returned HTTP 200 in 0.51s with an
        # 873 byte body and ZERO entries. The track id is not the problem: the search endpoint
        # confirms 504370616 is Buildertrend. So the lane is alive at the transport layer and empty
        # at the content layer, which is brightdata's failure mode wearing a different hostname, and
        # a lane wired ON in that state would quietly contribute nothing while looking configured.
        # sourcehealth's appstore_rss control asserts feed.entry is non-empty, so the day this comes
        # back the probe says ok and this flag can be flipped with evidence rather than with hope.
        # Do NOT re-enable on the strength of the 2026-08-27 numbers; re-probe first.
        "appstore-rss": {
            "enabled": False,
            "weight": 1.1,
            "side": "demand",
            "fetch": "https",
            "url_template": "https://itunes.apple.com/us/rss/customerreviews/page={page}/"
                            "id={track_id}/sortby=mostrecent/json",
            "lookup_template": "https://itunes.apple.com/search?term={name}&entity=software"
                               "&country=us",
            "max_pages": 10,
            "reviews_per_page": 50,
            "hard_cap_reason": "page=11 returns HTTP 400 (measured 2026-08-27). Report cap_reached "
                               "when page 10 is consumed; never truncate silently.",
            "_control": "sourcehealth.CONTROLS['appstore_rss']: page 1 of a known app id, "
                        "asserting feed.entry holds at least one entry. A 200 carrying an empty "
                        "entry list is the fail-open shape and must not read as ok.",
            "_cost": "free, keyless, no account. One GET per page, ten pages maximum per app.",
        },
        # ---------------------------------------------------------------- demand: dated why_now
        # SEC EDGAR full text search, keyless raw HTTP with a descriptive User-Agent.
        # Measured 2026-08-27: HTTP 200 in 0.35s. It searches filing TEXT, so pain language such as
        # "material weakness", "manual" or "labor shortage" is findable ACROSS companies instead of
        # one ticker at a time, which is what makes it a demand source rather than a finance source.
        # The User-Agent is REQUIRED by SEC and is read from the private config. Never hardcode a
        # person's contact details here: a real address in an SEC User-Agent is exactly the leak
        # this fleet has already had to rewrite git history to remove.
        "sec-edgar-fts": {
            "enabled": True,
            "weight": 1.0,
            "side": "demand",
            "fetch": "https",
            "url_template": "https://efts.sec.gov/LATEST/search-index?q={phrase}&forms={forms}",
            "default_forms": "8-K",
            "user_agent_required": True,
            # SEC rejects a User-Agent that is not "Name contact-address"; measured 2026-08-29,
            # a descriptive-but-contactless UA and a browser UA both got HTTP 403, while
            # "DailyHotspots Research research@example.com" got 200 with 60095 bytes. The real
            # address is supplied at run time from PRIVATE config or the environment and is NEVER
            # written here: a contact address baked into a public repo is the leak this fleet has
            # already had to rewrite git history to remove, and it entered through an SEC UA.
            # This lives in the dict rather than in a comment above it so that a reader of the
            # config, human or program, can see the rule without reading the source file.
            "user_agent_source": "private config DAILY_HOTSPOTS_SEC_UA, else env SEC_USER_AGENT",
            "user_agent_example": "Example Research research@example.com",
            "pain_phrases": ["material weakness", "manual", "labor shortage"],
            "_control": "sourcehealth.CONTROLS['sec_fts']: q=\"material weakness\" against 8-K, "
                        "asserting hits.hits holds at least one hit. That phrase is always present "
                        "in the 8-K corpus, so an empty hit list indicts the endpoint, not the "
                        "corpus.",
            "_cost": "free, keyless. One GET per phrase. SEC throttles anonymous bursts, so keep "
                     "the phrase list short and let the retry back off rather than widening it.",
        },
        # Federal Register API, keyless. Measured 2026-08-27: 200 in 0.09s to 0.12s, the newest RULE
        # was published the same day, and the significant condition is honored SERVER SIDE.
        # This is the source that supplies a demand card's why_now: a rule with a publication date
        # and a compliance obligation, dated and mandatory and industry wide, rather than "it
        # trended today".
        "federal-register": {
            "enabled": True,
            "weight": 1.0,
            "side": "demand",
            "fetch": "https",
            "url_template": "https://www.federalregister.gov/api/v1/documents.json",
            "query": {"conditions[type][]": "RULE", "conditions[significant]": "1",
                      "order": "newest", "per_page": 20},
            "_control": "sourcehealth.DEFAULT_SPECS['federal-register'], which overrides the "
                        "json_list_api control with documents.json?per_page=1&order=newest and "
                        "asserts results holds at least one row. The register publishes on every "
                        "business day, so an empty results list is the API failing, not the "
                        "government going quiet.",
            "_cost": "free, keyless, no registration. One GET per run.",
        },
        # ---------------------------------------------------------------- demand: budget attached
        # USAspending.gov awards API, keyless POST. A verified real record from 2026-08-27:
        # EAGLE HARBOR LLC, 79023098.38 USD, for "DATA ENTRY, IMAGING, INDEXING, IT SUPPORT
        # SERVICES". A near eighty million dollar contract paying for manual data entry is a demand
        # signal with a budget already attached to it, which is the strongest form this lane has.
        "usaspending": {
            "enabled": True,
            "weight": 0.9,
            "side": "demand",
            "fetch": "https-post",
            "endpoint": "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            "_control": "sourcehealth.DEFAULT_SPECS['usaspending'], which overrides the "
                        "json_list_api control with the keyless GET on "
                        "/api/v2/references/toptier_agencies/ and asserts results holds at least "
                        "one row. A 200 with an empty list means the API is broken, not that the "
                        "federal government stopped awarding contracts.",
            "_cost": "free, keyless. One POST per query shape.",
        },
        # ---------------------------------------------------------------- demand: paid-for schlep
        # The Muse public jobs API, keyless. Measured 2026-08-27: 200, 155 KB, 0.26s. It is the only
        # reachable job source whose DEFAULT population is non-tech (Walmart, CVS, Eaton, Griffith
        # Foods), which is precisely the corner Lane D keeps failing to reach. A company hiring a
        # full time human for a repetitive task is a pain it already PAYS for.
        # 155 KB per page is large enough to matter: slice the fields you need before handing the
        # payload to the cross-source merge, and never drop pages without saying so.
        "the-muse": {
            "enabled": True,
            "weight": 0.9,
            "side": "demand",
            "fetch": "https",
            "url_template": "https://www.themuse.com/api/public/jobs?page={page}",
            "_control": "sourcehealth.DEFAULT_SPECS['the-muse'], which overrides the json_list_api "
                        "control with jobs?page=1 and asserts results holds at least one row. "
                        "Measured at 155 KB, so a tiny body is a truncated or blocked response, "
                        "not a thin job market.",
            "_cost": "free, keyless. One GET per page, 155 KB per page on the wire.",
        },
        # ---------------------------------------------------------------- quarantined fetcher
        # NOT a content lane. brightdata is a FETCHER, and it has a row here so its state is written
        # down where the collection config is read, because the way it failed is invisible from any
        # lane that uses it.
        # CONTROL PROBES, 2026-08-27: scrape_as_markdown on https://example.com returned a
        # completely empty content block, and search_engine for "weather today" returned
        # {"organic":[],"current_page":1}. Both were well formed, neither carried an error, both
        # carried zero data. It FAILS OPEN. It was the first hop of the retrieval chain and the sole
        # route for the linux.do lane, 11 percent of archived cards, so its silence read as "nothing
        # was posted" every single day.
        # enabled=false is a QUARANTINE, not a deletion. Do not flip it back without re-running both
        # control probes and recording the date and the payloads, the way this note does.
        # NOTE it is disabled here and STILL PROBED: sourcehealth.DEFAULT_SPECS carries `brightdata`
        # (search) and `brightdata-scrape` (scrape) as separate lanes, because the two tools fail
        # independently. Quarantining a source must never be the thing that stops it being checked.
        "brightdata": {
            "enabled": False,
            "role": "fetcher",
            "suspect_since": "2026-08-27",
            "suspect_reason": "fails open: scrape_as_markdown on https://example.com returned an "
                              "empty content block and search_engine returned "
                              "{\"organic\":[],\"current_page\":1}, both without an error",
            "_control": "sourcehealth.CONTROLS['web_scrape'] (example.com must contain 'Example "
                        "Domain') and CONTROLS['web_search'] (\"weather today\" must return at "
                        "least one organic result). A tool that cannot fetch example.com is "
                        "broken. These two probes are what caught it, so these two are what must "
                        "pass before it is re-promoted.",
            "_cost": "one brightdata call per probe. Cheap. It was never the cost that stopped "
                     "anyone from checking, it was that nothing ever asked.",
        },
    },
    "scoring": {
        # SUPPLY (basic hotspots): hotness-first, timing is the top weight, this lane is breadth.
        "weights": {"track_fit": 0.20, "timing": 0.25, "feasibility": 0.20,
                    "competition": 0.15, "executability": 0.20},
        # DEMAND (quality opportunities): pain-first. timing is de-emphasized (a durable unmet need
        # does not need to be trending today), competition/feasibility/executability carry the weight
        # (blue ocean + can you actually build and reach it). Used by score_opportunity(side="demand").
        "demand_weights": {"track_fit": 0.10, "timing": 0.10, "feasibility": 0.25,
                           "competition": 0.30, "executability": 0.25},
        # ------------------------------------------------------------------ demand-lane knobs
        # 2026-08-27 DEMAND-PARITY RETUNE. As shipped, the demand lane could not clear its own floor:
        # over 45 days it produced ZERO archived demand cards while producing well-evidenced demand
        # candidates daily. The raw (weighted-dimension) scores were comparable across sides, e.g. on
        # 2026-08-27 demand raw 65.6..79.3 vs supply raw 65.3..83.4, but demand alone paid TWO outside
        # haircuts (a crowdedness multiplier down to 0.545, and full news-half-life freshness decay)
        # and was then held to a floor 5 points HIGHER than supply. Result: the best demand card of the
        # day (raw 79.3, better than five of the seven supply cards) finished at 50.2 and was dropped,
        # while the worst supply card (raw 65.3) finished at 55.0 and was archived. Two fixes:
        #
        # crowdedness_mode. Crowdedness is a COMPETITION signal, and `competition` (reverse-scored:
        #   bluer ocean = higher) already carries 0.30 of the demand weight vector. Applying it AGAIN
        #   as an outside multiplier double counted it, and did so with more authority than the
        #   dimension itself: a 0.30-weight dimension can move the score by at most 30 points, while
        #   crowdedness_penalty 0.7 moved it by up to ~55. "dimension" (the default) folds the crowd
        #   signal INTO the competition dimension, where its authority is bounded by that weight, and
        #   the outside multiplier is retired. Never both, that is the defect being fixed.
        #   "legacy_multiplier" replays the OLD, double-counting behavior and exists ONLY so
        #   tests/test_demand_parity.py can calibrate new-vs-old on real history; it is not a
        #   supported production setting.
        # crowdedness_blend. How much of the (folded) competition dimension the numeric crowdedness
        #   estimate speaks for, vs the LLM's own competition judgement:
        #     competition_effective = (1 - blend) * competition + blend * (100 - crowdedness)
        #   0.0 = ignore crowdedness entirely, 1.0 = crowdedness IS the competition dimension.
        #   0.5 splits the say evenly. Only used when crowdedness_mode == "dimension".
        # crowdedness_penalty. Legacy outside-multiplier strength. Only used when crowdedness_mode
        #   == "legacy_multiplier"; inert under the shipped default.
        #
        # demand_freshness_mode. A durable unmet pain does not expire on a news half-life, that was
        #   always the stated intent. The shipped "floor" implementation did not deliver it: real
        #   demand evidence (a months-old complaint thread) landed at freshness 0.68..0.88, ABOVE the
        #   0.6 floor, so the floor never bound and demand simply paid full news decay. "neutral"
        #   (the default) sets demand freshness to exactly 1.0: no news decay at all, which is what
        #   "judge durable pain on the pain, not on recency" actually means. "floor" replays the old
        #   max(floor, decay) behavior for calibration. demand_freshness_floor is only read in
        #   "floor" mode.
        #
        # min_score_to_surface_demand is the (higher) bar a demand card must clear. It is only
        #   coherent if it is higher on a scale demand can REACH: 60 = min_score_to_archive (55) +
        #   demand_floor_premium (5). _clamp_guardrails enforces that relationship so a config can
        #   never restore an unreachable bar, and REPORTS it in scoring.guardrail_notes when it bites.
        "crowdedness_mode": "dimension",
        "crowdedness_blend": 0.5,
        "crowdedness_penalty": 0.7,
        "demand_freshness_mode": "neutral",
        "demand_freshness_floor": 0.6,
        "min_score_to_surface_demand": 60,
        "demand_floor_premium": 5,
        "max_demand_floor_premium": 10,
        "min_score_to_archive": 55,
        "min_score_to_push": 70,
        "min_score_to_deepdive": 80,
        # A whole platform agreeing with itself is ONE channel, not N. Per-handle origins stay
        # (the roster exists to catch a founder by identity), but each platform contributes at most
        # this many toward the independent count, so six x.com accounts echoing one narrative stop
        # buying the top confidence multiplier. 0 disables the cap. Measured need: 8 of 197 archived
        # cards cleared the red line on x.com alone, with counts up to 6.
        "max_origins_per_platform": 2,
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
    # Every clamp that actually BITES appends a human-readable line here, so "the config was checked
    # and nothing needed changing" (empty list) and "a rail silently rewrote your config" (non-empty)
    # are different, inspectable outputs. Reset first, so re-running is idempotent and never
    # accumulates duplicate notes.
    notes: list[str] = []
    sc["guardrail_notes"] = notes
    # safety-critical numeric floors: a user value is accepted only if it is >= the built-in default
    for k in ("min_independent_sources", "min_score_to_archive", "min_score_to_push"):
        try:
            sc[k] = max(float(sc.get(k, d[k])), float(d[k]))
        except (TypeError, ValueError):
            sc[k] = d[k]
    # ints stay ints (min_independent_sources is a count)
    sc["min_independent_sources"] = int(sc["min_independent_sources"])

    # ---- demand-bar reachability rail (2026-08-27) --------------------------------------------
    # This one clamps in the OPPOSITE direction from the rails above, and deliberately so. The rails
    # above stop a config from WEAKENING a safety floor. This one stops a config from raising the
    # demand bar so far above the supply bar that the demand lane can no longer clear it, which is
    # not a stricter filter but a silent, permanent outage: the skill shipped 45 days with a demand
    # bar it could not reach and reported each of those days as "今日无合格需求机会", indistinguishable
    # from an honestly empty day. A bar must be higher on a scale the lane can actually reach.
    #   lower bound: min_score_to_archive        (demand is the QUALITY column, never an easier bar)
    #   upper bound: min_score_to_archive + max_demand_floor_premium
    # Both ends are config-tunable (raise max_demand_floor_premium to buy a wider premium on
    # purpose), and any clamp that bites is recorded in guardrail_notes rather than applied silently.
    try:
        _cap_prem = float(sc.get("max_demand_floor_premium", d["max_demand_floor_premium"]))
    except (TypeError, ValueError):
        _cap_prem = float(d["max_demand_floor_premium"])
        notes.append("max_demand_floor_premium malformed, reset to shipped default "
                     f"{_cap_prem}")
    _cap_prem = max(0.0, _cap_prem)
    sc["max_demand_floor_premium"] = _cap_prem
    _arch = float(sc["min_score_to_archive"])
    try:
        _bar = float(sc.get("min_score_to_surface_demand", d["min_score_to_surface_demand"]))
    except (TypeError, ValueError):
        _bar = float(d["min_score_to_surface_demand"])
        notes.append("min_score_to_surface_demand malformed, reset to shipped default "
                     f"{_bar}")
    if _bar < _arch:
        notes.append(f"min_score_to_surface_demand {_bar} was BELOW min_score_to_archive {_arch}; "
                     f"raised to {_arch} (demand is the quality column, never the easier bar)")
        _bar = _arch
    elif _bar > _arch + _cap_prem:
        notes.append(f"min_score_to_surface_demand {_bar} exceeded min_score_to_archive {_arch} + "
                     f"max_demand_floor_premium {_cap_prem}; lowered to {_arch + _cap_prem} "
                     f"(an unreachable demand bar is a silent outage, not a stricter filter)")
        _bar = _arch + _cap_prem
    sc["min_score_to_surface_demand"] = _bar
    sc["demand_floor_premium"] = round(_bar - _arch, 6)
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


# --------------------------------------------------------------------------- untrusted input

# ONE url gate and ONE invisible-character class for the whole pipeline, because there used to be
# two url checks and the WEAKER one guarded the push. safe_url parses the authority, but it was
# reached only by the six demand parsers in run.py; everything the agent collected went to Discord
# through digest._clean_url, a shape-only check that accepted "https://good.example@evil.example/x"
# (the request goes to evil.example), a host carrying a bidi override, and an unbounded-length url.
# Both sides call safe_url now, so there is exactly one answer to "may this url be emitted".

MAX_URL_CHARS = 2048

# Characters that carry no meaning in a title or a quote and DO carry meaning to whatever reads them
# next: C0/C1 controls, zero-width joiners and spaces, the bidi override family, and the BOM.
# Stripped from every ingested text field and refused outright inside every url. Written as \u
# escapes so this source file itself stays free of invisible characters.
INVISIBLE_RE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
WS_RE = re.compile(r"\s+")

# The two characters a url must never carry into the pushed markdown: they end an inline span and
# open a tag. digest._clean_url refused them before this gate replaced it, so they stay refused.
_URL_MARKUP_CHARS = "<>"


def strip_invisible(s: str) -> str:
    """Remove the invisible/control class from a string field, leaving whitespace alone.

    Kept separate from the whitespace collapse because the ORDER matters and each caller collapses
    differently: Python's \\s does NOT match the Cf category, so a sanitizer that only collapses
    whitespace passes a zero-width space or a bidi override straight through to the reader. Strip
    first, collapse second."""
    return INVISIBLE_RE.sub("", s)


def clean_text(v) -> str:
    """One untrusted field to a single-line plain string, with NOTHING truncated.

    Invisible and control characters are removed and whitespace runs collapse to one space. The
    length is deliberately left alone: a pain quote is the evidence, and a quote cut at N characters
    is a quote whose ending nobody can check. Only the DISPLAY title is shortened, and only where the
    existing lanes already shorten it.

    Non-strings are refused rather than coerced, EXCEPT a plain int or float, which the JSON feeds do
    hand over for an id or a rating. ``True`` is not a rating, so bool is refused ahead of the
    numeric branch: ``isinstance(True, int)`` is true, and without that guard a boolean field would
    render as the text "True".

    Lives here, beside INVISIBLE_RE and safe_url, because the collection side (run.py) and the
    render side (digest._inline) must strip the same class of characters. They did not always: the
    renderer collapsed whitespace only, the regex whitespace class does not match the Cf category, and a
    zero-width space rode an LLM-supplied title into the pushed message."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if not isinstance(v, str):
        if isinstance(v, (int, float)):
            v = str(v)
        else:
            return ""
    return WS_RE.sub(" ", INVISIBLE_RE.sub("", v)).strip()


def safe_url(u, allowed_hosts=None) -> str:
    """Validate an untrusted url and return it, or return "" when it must not be emitted.

    Rules, all of them refusals rather than repairs: it must be a string; it must carry no control or
    invisible characters (``https://trustpilot.com\u202e...`` reads as a different host to a human
    than to a parser); it must carry no ``<`` or ``>`` (the url is pasted into markdown that gets
    pushed to Discord); the scheme must be http or https (so no ``javascript:`` and no ``data:``); it
    must have a host; it must carry no ``userinfo@`` (``https://trustpilot.com@evil.example`` is a
    request to evil.example); and it must be at most 2048 characters, refused rather than truncated.

    ``allowed_hosts`` PINS the host: the url's host must equal one of them or be a subdomain of one.
    That is the control that keeps an untrusted review body from publishing an arbitrary link under
    this source's origin tag, and it is compared on parsed host segments, never on the raw string.
    A caller rendering an already-attributed link (the digest) passes no hosts and still gets every
    other rule; the six demand parsers pin their own source's host."""
    if not isinstance(u, str):
        return ""
    t = u.strip()
    if not t or len(t) > MAX_URL_CHARS:
        return ""
    if INVISIBLE_RE.search(t) or WS_RE.search(t):
        return ""
    if any(ch in t for ch in _URL_MARKUP_CHARS):
        return ""
    low = t.lower()
    for scheme in ("https://", "http://"):
        if low.startswith(scheme):
            rest = t[len(scheme):]
            break
    else:
        return ""
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not authority or "@" in authority:
        return ""
    host = authority.split(":", 1)[0].rstrip(".").lower()
    if not host or "/" in host:
        return ""
    if allowed_hosts:
        ok = False
        for h in allowed_hosts:
            h = str(h).strip(".").lower()
            if host == h or host.endswith("." + h):
                ok = True
                break
        if not ok:
            return ""
    return t


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

# --------------------------------------------------------------------------- pull records (shared)
# These live here rather than in run.py or collect.py because BOTH legs need them and neither owns
# them: the collection leg writes pull records, the deterministic core reads and routes them. Moved
# out of run.py verbatim on 2026-08-29 when the collection leg became its own module; the bodies are
# byte-identical to what run.py carried, and `rt` is roster, imported below as run.py imported it.
import roster as rt

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

def failed_pull(unit: dict, error: str, run_id: str, now,
                attempts: int = 1, outcome: str | None = None) -> dict:
    """One failed-pull record: the unit that was attempted, why it failed, and NOT an observation.

    ``attempts`` is how many times the pull was actually issued before it was given up on, and
    ``outcome`` is the terminal verdict (default ``failed_after_<attempts>_attempts``). Both travel
    into ``sources_failed`` so the digest can tell "we tried once and the API was down" apart from
    "we tried three times with backoff and it is still down", which is the difference between a
    flake and a lane that needs a new fetch route. arctic-shift returns HTTP 500 on roughly half of
    all calls (measured 6 of 12 sequential pulls), so a single-attempt failure there is close to a
    coin flip and must not read the same as an exhausted retry budget."""
    rec = {"run_id": run_id, "ts": iso(now)}
    rec.update(unit)
    rec["error"] = str(error)[:300]
    rec["observed"] = False
    try:
        n = max(1, int(attempts))
    except (TypeError, ValueError):
        n = 1
    rec["attempts"] = n
    rec["outcome"] = str(outcome) if outcome else f"failed_after_{n}_attempts"
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
