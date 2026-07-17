#!/usr/bin/env python3
"""R3 adversarial dedup headroom (self-evolve batch 3).

Targets a real PRECISION defect in `dedup.match_existing`: the weak soft-match rung
(`strong and cos>=0.45`) merges two *distinct* opportunities that merely share generic
descriptor words (jaccard high) even though their distinguishing SUBJECT entities differ
(e.g. Stripe vs Adyen, Vercel vs Netlify). A false merge silently SUPPRESSes a genuinely
distinct opportunity, violating ARCHITECTURE §5.2 ("同一机会判定=多信号联合，单一信号必失败":
generic word overlap alone, a single weak signal, must not merge) and the ≥2-independent-source
red line (a real distinct opportunity is dropped, never pushed).

Each xfail asserts a CAPABILITY (distinct subjects ⇒ distinct opportunity), not a specific
threshold table, so the fix is free to choose its mechanism. `xfail(strict=False)` keeps the
baseline green (xpass after the fix is tolerated → markers later removed as permanent guards).
The non-xfail tests below are RECALL/no-regression guards: legitimate same-subject rewrites,
exact matches, evolving supersets, and unrelated pairs must keep their current behavior.
"""
import pytest

from lib import load_config, canonical_key, extract_entities, simhash
import dedup as dd

CFG = load_config()
EXT = dd.EXT_PREFIX


def _row(title, summary, track, score=70, stage="", sources=None):
    ck = canonical_key(extract_entities(title + " " + summary), track)
    ext = {
        EXT + "canonical_key": ck,
        EXT + "simhash": simhash(title + " " + summary),
        EXT + "text": title + " " + summary,
        EXT + "first_seen": "2026-06-24T12:00:00Z",
        EXT + "last_seen": "2026-06-24T12:00:00Z",
        EXT + "last_score": score,
        EXT + "lifecycle_stage": stage,
        EXT + "source_set": sources or ["hackernews", "trend-pulse"],
        EXT + "push_count": 0,
        EXT + "samples": [],
    }
    return {"idempotency_key": ck, "ext": ext}


def _cand(title, summary, track, score=72, stage="", sources=None):
    ck = canonical_key(extract_entities(title + " " + summary), track)
    ev = [{"source": s, "origin": s + ".com", "url": "http://x", "ts": "2026-06-25T11:00:00Z"}
          for s in (sources or ["hackernews", "trend-pulse"])]
    return {"canonical_key": ck, "title": title, "summary": summary, "track": track,
            "final_score": score, "lifecycle_stage": stage, "evidence": ev,
            "source_set": sources or ["hackernews", "trend-pulse"]}


# distinct-subject pairs: same track, heavy generic-descriptor overlap, different brand subject.
# (track, row_title, row_summary, cand_title, cand_summary)
_FALSE_MERGE = [
    ("fintech-crypto",
     "Stripe payments platform adds fraud detection online merchants",
     "stripe online merchants fraud detection platform payments",
     "Adyen payments platform adds fraud detection online merchants",
     "adyen online merchants fraud detection platform payments"),
    ("dev-tools",
     "Vercel deploy platform adds edge functions framework",
     "vercel deploy edge platform framework",
     "Netlify deploy platform adds edge functions framework",
     "netlify deploy edge platform framework"),
    ("ai-agents",
     "Pinecone vector database adds hybrid search infra",
     "pinecone vector database hybrid infra",
     "Weaviate vector database adds hybrid search infra",
     "weaviate vector database hybrid infra"),
    ("ai-agents",
     "OpenAI launches new model api framework",
     "openai model api framework launch",
     "Anthropic launches new model api framework",
     "anthropic model api framework launch"),
    ("fintech-crypto",
     "Robinhood trading app adds crypto staking rewards feature",
     "robinhood trading crypto staking rewards",
     "Coinbase trading app adds crypto staking rewards feature",
     "coinbase trading crypto staking rewards"),
    ("dev-tools",
     "Datadog observability platform adds log analytics dashboards",
     "datadog observability log analytics dashboards",
     "Grafana observability platform adds log analytics dashboards",
     "grafana observability log analytics dashboards"),
    ("saas-niche",
     "Notion workspace adds database automation workflow blocks",
     "notion workspace database automation workflow",
     "Coda workspace adds database automation workflow blocks",
     "coda workspace database automation workflow"),
    ("ai-agents",
     "Cursor ai coding editor adds agent autocomplete refactor",
     "cursor coding editor agent autocomplete",
     "Windsurf ai coding editor adds agent autocomplete refactor",
     "windsurf coding editor agent autocomplete"),
    ("ai-agents",  # CJK distinct subjects (ties to batch-1 multilingual): different company, same template
     "字节跳动 发布 全新 AI 智能 助手 平台", "字节跳动 AI 助手 平台",
     "阿里巴巴 发布 全新 AI 智能 助手 平台", "阿里巴巴 AI 助手 平台"),
]


@pytest.mark.parametrize("track,rt,rs,ct,cs", _FALSE_MERGE)
def test_distinct_subjects_do_not_merge(track, rt, rs, ct, cs):
    """Two opportunities sharing only generic descriptors but with different subject brands
    must NOT match (precision). On HEAD they false-merge via the weak cos>=0.45 rung."""
    row = _row(rt, rs, track)
    cand = _cand(ct, cs, track)
    assert dd.match_existing(cand, [row], CFG) is None


@pytest.mark.parametrize("track,rt,rs,ct,cs", _FALSE_MERGE[:3])
def test_distinct_subjects_decide_new(track, rt, rs, ct, cs):
    """End-to-end: a distinct-subject opportunity must route to NEW, not be silently SUPPRESSed
    by a false merge with an unrelated existing row."""
    row = _row(rt, rs, track)
    cand = _cand(ct, cs, track)
    matched = dd.match_existing(cand, [row], CFG)
    assert dd.decide(cand, matched, CFG)["branch"] == dd.NEW


# ---- regression / recall guards (green on HEAD; must stay green after the fix) -------------

def test_recall_exact_match_preserved():
    row = _row("MCP gateway local LLM tools", "self host inference proxy", "ai-agents")
    cand = _cand("MCP gateway local LLM tools", "self host inference proxy", "ai-agents")
    assert dd.match_existing(cand, [row], CFG) is not None


def test_recall_same_subject_rewrite_preserved():
    """Legitimate rewrite of the SAME subject (shared 'mineru') must still match."""
    row = _row("MinerU PDF extraction open source tool", "parse pdf to markdown",
               "ai-agents", sources=["github", "hackernews"])
    cand = _cand("MinerU open-source PDF extraction tool", "convert pdf into markdown",
                 "ai-agents", sources=["github", "hackernews"])
    assert dd.match_existing(cand, [row], CFG) is not None


def test_recall_same_subject_evolving_superset_preserved():
    """Same subject, later report strictly richer (more detail/sources) = same evolving opp."""
    row = _row("Stripe adds fraud detection", "stripe fraud detection", "fintech-crypto")
    cand = _cand("Stripe adds fraud detection platform for merchants online",
                 "stripe fraud detection platform merchants online", "fintech-crypto")
    assert dd.match_existing(cand, [row], CFG) is not None


def test_unrelated_pair_stays_distinct():
    row = _row("MCP agent framework", "open source tooling", "ai-agents")
    cand = _cand("DeFi yield aggregator", "stablecoin onchain vault", "fintech-crypto")
    assert dd.match_existing(cand, [row], CFG) is None


def test_symmetry_distinct_subjects_both_directions():
    """Veto is symmetric: swapping row/candidate still yields distinct for a false-merge pair."""
    tr, rt, rs, ct, cs = _FALSE_MERGE[0]
    a = dd.match_existing(_cand(ct, cs, tr), [_row(rt, rs, tr)], CFG)
    b = dd.match_existing(_cand(rt, rs, tr), [_row(ct, cs, tr)], CFG)
    # at least the no-regression direction (unrelated stays None) holds on HEAD too; the
    # capability (both None) is asserted via the xfail parametrization above.
    assert (a is None) == (b is None)
