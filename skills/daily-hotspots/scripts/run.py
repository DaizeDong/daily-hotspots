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
import sys

from lib import (canonical_key, extract_entities, iso, load_config, now_utc,
                 opportunity_id)
from classify import classify
from score import score_opportunity
import dedup as dd
from verify_gate import gate_batch
import push_card as pc
import archive as ar
import digest as dg


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
    }
    return card


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

    # ---- build + distinct-ORIGIN red line ----
    cards, excluded, below_sources = [], [], []
    for cand in candidates:
        card = build_card(cand, cfg, run_id, arms=bandit_arms, seed=bandit_seed)
        if card is None:
            continue
        if card.get("_excluded"):
            excluded.append(card)
            continue
        if card["independent_source_count"] < min_src:
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
    archived = []
    for c in archivable:
        status, detail = ar.archive_card(c, archive_dir, cfg)
        if status == "archived":
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

    # ---- digest (idempotent item + file + deliver) ----
    coverage = {"sources_invoked": "(see SKILL run)", "sources_available": "(see SKILL run)",
                "candidates": len(candidates), "pushed": len(pushed),
                "deepdived": sum(1 for c in cards if c.get("delegated_deepdive"))}
    md = dg.build_markdown(archivable, coverage)
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
