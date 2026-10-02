#!/usr/bin/env python3
"""Append-only opportunity archive (Acceptance Gate T6 mechanical 宁缺毋滥).

Writes to the PRIVATE companion config repo's archive/, resolved by the one shared resolver
(``guards/tools/datadir.py``) or by an explicit --archive-dir. There is no default and no fallback: an
uninitialized install raises ArchiveDirNotInitialized rather than inventing a home for the ledger.
What lands there:
  * opportunities.jsonl, canonical append-only store (git history = backup)
  * dedup-state.json, fingerprint -> {first_seen,last_seen,push_count,cluster_id}
  * digests/YYYY/YYYY-MM-DD.md is written by digest.py, not here.

archive_card() re-asserts the quality gate (distinct ORIGIN >= 2 AND score >= min_score_to_archive)
before any write, a low-quality card is mechanically refused, returning ("refused", reason). This
is the deterministic backstop to the verify gate; nothing low-quality reaches disk.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from lib import iso, load_config, now_utc, opportunity_id

SKILL = "daily-hotspots"


class ArchiveDirNotInitialized(RuntimeError):
    """No private companion config resolved, so there is nowhere legitimate to write.

    This is a HARD FAILURE on the write path, on purpose. The predecessor returned
    ``Path.home() / ".daily-hotspots-config" / "archive"`` and ``archive_card`` then called
    ``mkdir(parents=True)`` on it, so an uninitialized machine did not fail: it CONJURED a
    companion config out of thin air and started an opportunity ledger inside it. Two things were
    wrong with that. The ledger was real run output filed at a scattered $HOME path with no
    remote, no history and no backup, which is the one place the data boundary says it must never
    live; and "uninitialized" and "initialized at the default path" became the same observable
    state, so nobody could tell a working install from a silent one. A writer with nowhere to
    write has exactly two options and only one of them is honest.
    """


_datadir_mod = None


def _datadir():
    """Load the pinned guard resolver and bind it to this consumer repository.

    Discovery starts from this module location, so installed directory aliases
    resolve to the same guard. Missing submodule content is a hard failure.
    """
    global _datadir_mod
    if _datadir_mod is not None:
        return _datadir_mod
    here = Path(__file__).resolve()
    for anc in here.parents:
        cand = anc / "guards" / "tools" / "datadir.py"
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("daily_hotspots_archive_guard", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            from private_storage import bind_consumer
            bind_consumer(mod, anc)
            _datadir_mod = mod
            return mod
    raise ArchiveDirNotInitialized(
        "cannot locate guards/tools/datadir.py above %s.\n"
        "That file is the ONLY resolver allowed to decide where real-run output goes; without it\n"
        "this writer cannot prove its destination is outside the tool repo, so it refuses to write.\n"
        "Initialize the pinned guards submodule and retry." % here)


def find_archive_dir(explicit: str | None = None) -> Path | None:
    """Resolve the archive dir, or return None when the tool is UNINITIALIZED. Creates nothing.

    The READER seam. A reader may legitimately degrade (no companion config yet means no history
    yet, and reporting an empty history is a correct answer); a writer may not, so writers call
    ``resolve_archive_dir`` and take the exception.
    """
    dd = _datadir()
    if explicit:
        p = Path(explicit).expanduser()
        # An explicit --archive-dir used to bypass every boundary check by construction. It does
        # not any more: the same refusal that covers the resolved path covers the passed one.
        dd.assert_outside_own_repo(p, SKILL)
        return p
    base = dd.resolve_data_dir(SKILL)
    if base is None:
        return None
    return Path(base) / "archive"


def resolve_archive_dir(explicit: str | None = None) -> Path:
    """The WRITE seam: the archive dir, or a hard failure that says how to initialize.

    Raises ArchiveDirNotInitialized (no companion config) or datadir.DataDirInsideOwnRepo (the
    destination is inside this public repo). Neither is recoverable by guessing a path.
    """
    p = find_archive_dir(explicit)
    if p is None:
        raise ArchiveDirNotInitialized(
            "daily-hotspots has no private companion config, so it has nowhere to put an\n"
            "opportunity ledger. This is the correct state for a freshly cloned public skill:\n"
            "it ships as an uninitialized tool. Initialize it, do not let it invent a home:\n"
            "    python scripts/init_config.py --out <path>/daily-hotspots-config\n"
            "    set DAILY_HOTSPOTS_CONFIG to that directory (a private git repo)\n"
            "    or pass --archive-dir explicitly for a one-off run\n"
            "Real-run output NEVER goes back into the tool repo, and it does not get filed at a\n"
            "scattered $HOME path either: the private companion repo is where it belongs, with a\n"
            "remote, a history and a backup.")
    return p


ARCHIVE_SCHEMA_VERSION = 2


def _jsonl_record(card: dict) -> dict:
    """Project the allow-listed card fields into the durable ledger.

    Schema 2 retains demand attribution and corroboration detail. Readers treat
    optional fields absent from older rows as unknown."""
    ck = card["canonical_key"]
    return {
        "opportunity_id": card.get("opportunity_id") or opportunity_id(ck),
        "canonical_key": ck,
        "cluster_id": card.get("cluster_id", ""),
        "first_seen": card.get("first_seen") or iso(now_utc()),
        "last_seen": iso(now_utc()),
        "status": card.get("status", "new"),
        "title": card.get("title", ""),
        "summary": card.get("summary", ""),
        "track": card.get("track"),
        "focus_tags": card.get("focus_tags", []),
        "machine_type": card.get("machine_type", []),
        "score": card.get("final_score"),
        "grade": card.get("grade"),
        "score_breakdown": card.get("score_breakdown", {}),
        "raw_score_breakdown": card.get("raw_score_breakdown", card.get("score_breakdown", {})),
        "score_inputs": card.get("score_inputs", {}),
        "why_now": card.get("why_now", ""),
        "contrarian_insight": card.get("contrarian_insight", ""),
        "action": card.get("action", ""),
        "evidence": card.get("evidence", []),
        "independent_source_count": int(card.get("independent_source_count", 0)),
        "pushed": bool(card.get("pushed", False)),
        "push_count": int(card.get("push_count", 0)),
        "delegated_deepdive": card.get("delegated_deepdive"),
        "lifecycle_stage": card.get("lifecycle_stage", ""),
        "run_id": card.get("run_id", ""),
        # --- the demand triple: without these the lane cannot be measured after the fact ---
        "side": str(card.get("side", "supply")).strip().lower() or "supply",
        "crowdedness": card.get("crowdedness"),
        "pain_evidence": card.get("pain_evidence", ""),
        # --- corroboration detail, so a past ranking can be re-justified rather than re-guessed ---
        "confidence": card.get("confidence"),
        "source_set": card.get("source_set", []),
        "schema_version": ARCHIVE_SCHEMA_VERSION,
    }


def archive_card(card: dict, archive_dir: str | None = None,
                 cfg: dict | None = None, dry_run: bool = False) -> tuple[str, str]:
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    from verify_gate import validate_card
    ok, errors = validate_card(card, cfg)
    if not ok:
        return ('refused', '; '.join(errors))
    floor_key = 'min_score_to_surface_demand' if card.get('side', 'supply') == 'demand' else 'min_score_to_archive'
    floor = float(sc.get(floor_key, 60 if card.get('side') == 'demand' else 55))
    if float(card['final_score']) < floor:
        return ('refused', 'score is below ' + floor_key)

    # dry_run re-asserts the quality gate above (so preview surfaces exactly what WOULD persist)
    # but writes nothing, mirrors push/ledger/digest dry_run semantics. Critical: a test or preview
    # run that has $DAILY_HOTSPOTS_CONFIG set must NOT leak fake cards into the real archive.
    if dry_run:
        return ("would-archive", card.get("opportunity_id") or opportunity_id(card.get("canonical_key", "")))

    base = resolve_archive_dir(archive_dir)
    from private_storage import prove
    prove(base / 'opportunities.jsonl')
    prove(base / 'dedup-state.json')
    base.mkdir(parents=True, exist_ok=True)
    rec = _jsonl_record(card)

    # Read and validate all existing state before appending anything to the ledger.
    state_path = base / 'dedup-state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if not isinstance(state, dict) or any(not isinstance(entry, dict) for entry in state.values()):
        raise ValueError('dedup state must map keys to records')
    ck = rec['canonical_key']
    entry = dict(state.get(ck, {}))
    count = entry.get('push_count', 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('dedup push_count is invalid')
    entry.setdefault('first_seen', rec['first_seen'])
    entry.update(last_seen=rec['last_seen'], push_count=count + (1 if card.get('pushed') else 0),
                 cluster_id=rec['cluster_id'] or entry.get('cluster_id', ''), opportunity_id=rec['opportunity_id'])
    state[ck] = entry
    import uuid
    temporary = base / ('.dedup-state-' + uuid.uuid4().hex + '.tmp')
    prove(temporary)
    with temporary.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    # Any append or replace failure propagates, retaining the prior state and prepared file.
    with (base / 'opportunities.jsonl').open('a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    prove(state_path)
    os.replace(temporary, state_path)
    return ("archived", rec["opportunity_id"])


def main() -> int:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace") or "{}")
    cards = data if isinstance(data, list) else [data]
    out = []
    for c in cards:
        status, detail = archive_card(c)
        out.append({"title": c.get("title", "?"), "status": status, "detail": detail})
    print(json.dumps(out, ensure_ascii=False))
    return 1 if any(item["status"] == "refused" for item in out) else 0


if __name__ == "__main__":
    sys.exit(main())
