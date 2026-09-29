#!/usr/bin/env python3
"""Retain complete run workspaces in a verified PRIVATE versioned companion.

Raw captures, helper scripts, logs and handoff snapshots are runtime DATA. The default
workspace is archive/workspaces/<run-id>, included by the wrapper's archive commit.
An explicit run-root override must also pass the PRIVATE repository proof.

promote() keeps the existing named, size-capped replay view in archive/runs without
discarding the complete workspace. Explicit legacy scratch cleanup remains separate
and refuses Git worktrees; versioned run history is never automatically pruned.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SKILL = "daily-hotspots"

# The slice worth keeping forever, and the size past which "this is the small curated record" stops
# being true. Aliases exist because the run's own report has been written under several names by
# different orchestration passes; the first one present wins and lands under the canonical name.
KEEP: dict[str, tuple[tuple[str, ...], int]] = {
    "candidates.json": (("candidates.json",), 4 * 1024 * 1024),
    "result.json": (("result.json", "run_out.json", "dry.json", "dryrun_out.json"), 1024 * 1024),
}

DEFAULT_RETENTION_DAYS = 14
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class RunStoreError(RuntimeError):
    """Refusal on a WRITE path. Readers degrade; writers raise (see archive.py for the same seam)."""


_datadir_mod = None


def _datadir():
    """Load the pinned ``guards/tools/datadir.py`` submodule resolver for real output.

    Found by walking up, and loaded under a PRIVATE module name, exactly the way archive.py and
    roster.py load it. Kept local rather than imported from either of them on purpose: each writer
    proves its own destination, and neither writer's boundary check can be broken by renaming a
    private helper in another module (see roster.py, which carries the same note).

    The MECHANISM matters and used to differ here. This did `sys.path.insert` plus a bare
    ``import datadir``, which is two defects the other two writers do not have. A bare import
    registers a top-level name ``datadir`` that anything else on sys.path can shadow, so the module
    deciding where real data goes was resolvable by name collision; and it was unmemoized, so every
    ``run_dir``/``promote`` call re-entered the walk and, on a cold sys.modules, re-ran module init.
    Absence is a hard failure on the write path: without this resolver the module cannot prove a
    destination is outside the tool repo, and an unprovable destination is exactly how real data
    ended up back in a public checkout.
    """
    global _datadir_mod
    if _datadir_mod is not None:
        return _datadir_mod
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "guards" / "tools" / "datadir.py"
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("daily_hotspots_runstore_guard", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            from private_storage import bind_consumer
            bind_consumer(mod, parent)
            _datadir_mod = mod
            return mod
    raise RunStoreError(
        "cannot locate guards/tools/datadir.py above %s.\n"
        "It is the only resolver allowed to decide where real-run output goes; without it this\n"
        "writer cannot prove its destination is outside the tool repo, so it refuses to write.\n"
        "From the consumer root, initialize the pinned kit with:\n"
        "    git submodule update --init --recursive -- guards\n"
        "Then retry; do not replace the pinned kit with a vendored copy." % here)


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
    """Resolve an explicit run root or the archive's retained workspace directory."""
    override = os.environ.get("DAILY_HOTSPOTS_RUN_ROOT", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _archive_root() / "workspaces"


def run_dir(run_id: str, create: bool = True) -> Path:
    """Prove the resolved PRIVATE destination before creating any run directory."""
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


def _inside_any_worktree(p: Path) -> bool:
    """Detect worktrees at the canonical destination, including directory junctions."""
    try:
        cur = p.resolve()
    except OSError as exc:
        raise RunStoreError("cannot inspect cleanup destination") from exc
    for node in (cur, *cur.parents):
        try:
            if os.path.lexists(node / ".git"):
                return True
        except OSError as exc:
            raise RunStoreError("cannot inspect cleanup worktree boundary") from exc
    return False


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
    _datadir().assert_outside_own_repo(dest, SKILL)

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
            from private_storage import prove
            target = prove(target)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        promoted.append({"name": canonical, "from": p.name, "size": size})

    return {"run_id": run_id, "date": _run_date(run_id), "src": str(src), "dest": str(dest),
            "promoted": promoted, "skipped": skipped, "dry_run": bool(dry_run)}


def _inspect_cleanup_tree(directory: Path, boundary: Path) -> None:
    """Prove the complete deletion subtree without following links or hiding scan errors."""
    pending = [directory]
    try:
        while pending:
            current = pending.pop()
            if current.resolve() != current or not current.is_relative_to(boundary):
                raise RunStoreError("refusing linked or escaped cleanup subtree: %s" % current)
            if _inside_any_worktree(current):
                raise RunStoreError("refusing versioned cleanup subtree: %s" % current)
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.name.lower() == '.git':
                        raise RunStoreError("refusing cleanup subtree containing a Git worktree: %s" % path)
                    if entry.is_symlink() or path.resolve() != path:
                        raise RunStoreError("refusing linked cleanup descendant: %s" % path)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
    except OSError as exc:
        raise RunStoreError("cannot inspect complete cleanup subtree: %s" % directory) from exc


def prune(root=None, retention_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> dict:
    """Delete scratch older than the retention window. Reports what it removed and what it kept.

    Only dated directories under one canonical boundary are eligible. Every planned deletion
    subtree must be fully inspected before any deletion; versioned descendants and scan errors
    refuse cleanup, including dry runs.
    """
    if root is None:
        raise RunStoreError("prune requires an explicit legacy scratch root; PRIVATE workspaces are retained")
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return {"root": str(root), "removed": [], "kept": [], "skipped": [], "existed": False}
    if _inside_any_worktree(root):
        raise RunStoreError("refusing to prune %s: it is inside a git worktree" % root)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(0, int(retention_days)))
    planned, kept, skipped = [], [], []
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        raise RunStoreError("cannot enumerate cleanup root: %s" % root) from exc
    for child in children:
        if not child.is_dir():
            continue
        if child.resolve() != child or _inside_any_worktree(child):
            raise RunStoreError("refusing to prune linked or versioned scratch: %s" % child)
        m = _DATE_RE.search(child.name)
        if not m:
            skipped.append({"name": child.name, "reason": "no date in name"})
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            skipped.append({"name": child.name, "reason": "unparseable date"})
            continue
        if d < cutoff:
            _inspect_cleanup_tree(child, root)
            planned.append(child)
        else:
            kept.append(child.name)
    if not dry_run:
        for child in planned:
            # Recheck at the deletion boundary after the all-subtree preflight.
            _inspect_cleanup_tree(child, root)
            shutil.rmtree(child, ignore_errors=False)
    removed = [child.name for child in planned]
    return {"root": str(root), "cutoff": cutoff.isoformat(), "removed": removed,
            "kept": kept, "skipped": skipped, "existed": True, "dry_run": bool(dry_run)}


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PRIVATE run workspaces, compact replay copies and explicit legacy cleanup")
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

    p = sub.add_parser("prune", help="delete scratch older than the retention window")
    p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.add_argument("--root", required=True)
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
    if a.cmd == "prune":
        print(json.dumps(prune(a.root, a.days, a.dry_run), ensure_ascii=False, indent=2))
        return 0

    import archive as arch  # noqa: PLC0415  (reader seam: resolve the companion the one blessed way)
    archive_dir = a.archive_dir or arch.find_archive_dir()
    if not archive_dir:
        raise RunStoreError(
            "the private companion archive is not initialized, so there is nowhere to promote to.\n"
            "Set $DAILY_HOTSPOTS_CONFIG to the companion repo and retry.")
    rep = promote(a.src or run_dir(a.run_id, create=False), archive_dir, a.run_id, a.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    # A run whose candidate set did not survive is a run that cannot be replayed later. Say so with
    # an exit code rather than only in prose nobody reads.
    return 0 if any(x["name"] == "candidates.json" for x in rep["promoted"]) else 4


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except RunStoreError as e:
        print("runstore: %s" % e, file=sys.stderr)
        sys.exit(2)
