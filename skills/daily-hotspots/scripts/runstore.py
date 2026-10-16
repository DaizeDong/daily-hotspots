#!/usr/bin/env python3
"""Keep compact replay inputs and durable claims in a verified PRIVATE companion.

Completed workspaces can be compacted after both replay files and the finalization
claim are preserved. Uncertain or incomplete runs keep their recovery inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

SKILL = "daily-hotspots"

# Handoff and replay promotion accept the same candidate byte limit.
MAX_CANDIDATE_BYTES = 20_000_000
KEEP: dict[str, tuple[tuple[str, ...], int]] = {
    "candidates.json": (("candidates.json",), MAX_CANDIDATE_BYTES),
    "result.json": (("result.json",), 1024 * 1024),
}

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class RunStoreError(RuntimeError):
    """Refusal on a WRITE path. Readers degrade; writers raise (see archive.py for the same seam)."""


def _archive_root() -> Path:
    """Expose the existing archive resolver to both Python and the wrapper."""
    import archive as arch
    from private_storage import prove
    archive_dir = arch.find_archive_dir()
    if archive_dir is None:
        raise RunStoreError(
            "PRIVATE companion is not initialized; set DAILY_HOTSPOTS_CONFIG before running")
    return prove(archive_dir)


def _workspace_root() -> Path:
    """Resolve an explicit run root or the archive's temporary workspace directory."""
    override = os.environ.get("DAILY_HOTSPOTS_RUN_ROOT", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _archive_root() / "workspaces"


def run_dir(run_id: str, create: bool = True) -> Path:
    """Check the resolved PRIVATE destination before creating any run directory."""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        raise RunStoreError("run_id %r is not a safe directory name" % (run_id,))
    from private_storage import prove
    try:
        directory = prove(_workspace_root() / run_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RunStoreError("cannot establish PRIVATE run workspace: " + str(exc)) from exc
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _run_date(run_id: str) -> str:
    m = _DATE_RE.search(run_id or "")
    if not m:
        raise RunStoreError(
            "cannot read a date out of run_id %r, so the promoted slice would have no home.\n"
            "Run ids carry their date (daily-YYYY-MM-DD)." % (run_id,))
    return m.group(1)


def keep_dir(archive_dir, run_id: str) -> Path:
    """`runs/<slug>`, where the slug is the run id minus its `daily-` prefix.

    Deliberately NOT `runs/<date>`. That was the first shape and it silently ate a run: the migration
    of 2026-08-01 had both `daily-2026-08-01` and `daily-2026-08-01-rerun-1214`, both resolved to the
    same date directory, and the second overwrote the first with no error and no report. Two runs
    that happened are two runs to keep. The slug still begins with the date, so the directory still
    sorts chronologically.
    """
    _run_date(run_id)                      # validates that a date is present at all
    slug = run_id[len("daily-"):] if run_id.startswith("daily-") else run_id
    return Path(archive_dir) / "runs" / slug


def promote(src, archive_dir, run_id: str, dry_run: bool = False) -> dict:
    """Copy the allow-listed slice of one run's scratch into the tracked archive.

    Returns a report naming everything promoted AND everything skipped with the reason, because a
    retention policy that quietly drops things is indistinguishable from data loss. Missing scratch
    is a refusal, not a shrug: this runs on the write path.
    """
    from private_storage import exclusive_path
    src = exclusive_path(src, inspect_tree=True)
    if not src.is_dir():
        raise RunStoreError("run scratch does not exist: %s" % src)
    dest = exclusive_path(keep_dir(archive_dir, run_id), inspect_tree=True)
    from config_paths import resolver
    resolver().assert_outside_own_repo(dest, SKILL)

    promoted, skipped = [], []
    for canonical, (aliases, cap) in KEEP.items():
        chosen = None
        for name in aliases:
            p = src / name
            if not p.is_file():
                continue
            size = p.stat().st_size
            if size > cap:
                skipped.append({"name": name, "reason": "over_cap", "size": size, "cap": cap})
                continue
            chosen = (p, size)
            break
        if chosen is None:
            skipped.append({"name": canonical, "reason": "absent",
                            "looked_for": list(aliases)})
            continue
        p, size = chosen
        target = dest / canonical
        # Never overwrite a kept file with different bytes. The archive is the record; a promotion
        # that quietly replaces yesterday's record is the same defect as the digest clobber this
        # remediation already fixed one layer up. Identical bytes are an idempotent re-run and pass.
        if target.is_file() and target.read_bytes() != p.read_bytes():
            skipped.append({"name": canonical, "reason": "would_clobber", "existing": str(target)})
            continue
        if not dry_run:
            from private_storage import authorize_write
            target = authorize_write(target)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        promoted.append({"name": canonical, "from": p.name, "size": size})

    return {"run_id": run_id, "date": _run_date(run_id), "src": str(src), "dest": str(dest),
            "promoted": promoted, "skipped": skipped, "dry_run": bool(dry_run)}


def finalization_dir(archive_dir=None) -> Path:
    """Resolve the durable handoff-claim namespace, separate from delivery claims."""
    from private_storage import prove
    return prove(Path(archive_dir) / 'finalizations' if archive_dir else _archive_root() / 'finalizations')


def _workspace_entries(directory: Path) -> dict:
    """Snapshot ordinary leaves without following links or accepting active markers."""
    from private_storage import exclusive_path
    entries, pending = {}, [directory]
    while pending:
        current = pending.pop()
        for child in current.iterdir():
            exclusive_path(child)
            if child.name.lower() == '.git' or child.name.lower().endswith(('.lock', '.pid')):
                raise RunStoreError('workspace contains repository metadata or an active marker')
            info = child.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RunStoreError('workspace contains an unsupported file type')
            entries[child] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
    return entries


def compact(src, archive_dir, run_id: str, dry_run: bool = False) -> dict:
    """Retire one completed workspace only after its exact replay core is safe."""
    from private_storage import exclusive_path, prove, _repository_root
    from finalize_handoff import prior_claim
    src = exclusive_path(src, inspect_tree=True)
    archive_dir = prove(exclusive_path(archive_dir))
    if (not _RUN_ID_RE.fullmatch(run_id or '') or src.name != run_id or not src.is_dir()
            or archive_dir.is_relative_to(src)):
        raise RunStoreError('compact requires one existing run-id workspace outside its archive core')
    src = prove(src)
    if _repository_root(src) != _repository_root(archive_dir):
        raise RunStoreError('workspace and archive must share the same PRIVATE companion')
    report = {'run_id': run_id, 'src': str(src), 'eligible': False,
              'removed': False, 'dry_run': bool(dry_run)}
    try:
        entries = _workspace_entries(src)
        claim = prior_claim(run_id, finalization_dir(archive_dir))
        if claim is None or claim[1]['state'] != 'completed':
            return {**report, 'reason': 'finalization_missing_or_uncertain'}
        candidate = src/'candidates.json'
        result = next((src/name for name in KEEP['result.json'][0] if (src/name).is_file()), None)
        if (not candidate.is_file() or result is None or candidate.stat().st_size > MAX_CANDIDATE_BYTES
                or result.stat().st_size > KEEP['result.json'][1]):
            return {**report, 'reason': 'core_missing_or_over_cap'}
        data = candidate.read_bytes()
        payload = json.loads(data)
        rows = payload.get('candidates') if isinstance(payload, dict) else payload
        result_data = result.read_bytes()
        outcome = json.loads(result_data)
        if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                or not isinstance(outcome, dict) or outcome.get('run_id') != run_id
                or outcome.get('errors') != [] or hashlib.sha256(data).hexdigest() != claim[1]['candidate_sha256']):
            return {**report, 'reason': 'core_does_not_match_completed_claim'}
    except (RunStoreError, ValueError, TypeError) as exc:
        return {**report, 'reason': str(exc)}
    promotion = promote(src, archive_dir, run_id, dry_run=dry_run)
    if promotion['skipped'] or len(promotion['promoted']) != len(KEEP):
        return {**report, 'reason': 'core_promotion_incomplete', 'promotion': promotion}
    report.update(eligible=True, promotion=promotion)
    if dry_run:
        return report
    destination = Path(promotion['dest'])
    for name, expected in (('candidates.json', data), ('result.json', result_data)):
        preserved = exclusive_path(destination/name)
        if preserved.read_bytes() != expected:
            return {**report, 'eligible': False, 'reason': 'promoted_core_changed'}
    durable = prior_claim(run_id, finalization_dir(archive_dir))
    if durable is None or durable[1] != claim[1] or _workspace_entries(src) != entries:
        raise RunStoreError('workspace or finalization changed before compaction')
    prove(src)
    prove(archive_dir)
    # Remove only the inspected leaves; a newly created entry makes rmdir fail.
    # Recheck each leaf so a replaced file or link is never followed into deletion.
    for path in sorted(entries, key=lambda item: (len(item.parts), str(item)), reverse=True):
        exclusive_path(path)
        info = path.lstat()
        current = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        if stat.S_ISDIR(info.st_mode):
            if current[:3] != entries[path][:3]:
                raise RunStoreError('workspace directory changed during compaction')
            path.rmdir()
        else:
            if current != entries[path]:
                raise RunStoreError('workspace file changed during compaction')
            path.unlink()
    src.rmdir()
    return {**report, 'removed': True}


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PRIVATE run workspaces and verified compaction")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("archive", help="print the proved archive path without creating it")
    p.add_argument("--relative-to", help="return a pathspec inside this same PRIVATE worktree")

    p = sub.add_parser("dir", help="prove and create the PRIVATE workspace for a run id")
    p.add_argument("run_id")
    p.add_argument("--no-create", action="store_true")

    p = sub.add_parser("promote", help="copy the keep-slice of one run into the tracked archive")
    p.add_argument("run_id")
    p.add_argument("--src", default="", help="scratch dir (default: the resolved one for run_id)")
    p.add_argument("--archive-dir", default="")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("compact", help="preserve the replay core and remove one completed workspace")
    p.add_argument("run_id")
    p.add_argument("--src", default="")
    p.add_argument("--archive-dir", default="")
    p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "archive":
        try:
            archive_dir = _archive_root()
            if a.relative_to:
                from private_storage import prove, _repository_root
                companion = prove(a.relative_to)
                existing = archive_dir
                while not existing.exists():
                    existing = existing.parent
                if _repository_root(existing) != _repository_root(companion):
                    raise RunStoreError("archive and commit destination must share the same PRIVATE worktree")
                print(archive_dir.relative_to(companion).as_posix())
            else:
                print(archive_dir)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RunStoreError("cannot establish PRIVATE archive: " + str(exc)) from exc
        return 0
    if a.cmd == "dir":
        print(run_dir(a.run_id, create=not a.no_create))
        return 0

    import archive as arch  # noqa: PLC0415  (reader seam: resolve the companion the one blessed way)
    archive_dir = a.archive_dir or arch.find_archive_dir()
    if not archive_dir:
        raise RunStoreError(
            "the private companion archive is not initialized, so there is nowhere to promote to.\n"
            "Set $DAILY_HOTSPOTS_CONFIG to the companion repo and retry.")
    operation = compact if a.cmd == 'compact' else promote
    rep = operation(a.src or run_dir(a.run_id, create=False), archive_dir, a.run_id, a.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if a.cmd == 'compact':
        return 0 if rep['eligible'] else 4
    # A run whose candidate set did not survive is a run that cannot be replayed later. Say so with
    # an exit code rather than only in prose nobody reads.
    return 0 if any(x["name"] == "candidates.json" for x in rep["promoted"]) else 4


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except RunStoreError as e:
        print("runstore: %s" % e, file=sys.stderr)
        sys.exit(2)
