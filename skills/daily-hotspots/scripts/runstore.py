#!/usr/bin/env python3
"""Where a run's scratch lives, and the thin slice of it that is worth keeping forever.

THE PROBLEM THIS SOLVES. The orchestration agent needs somewhere to dump raw captures while it
works. Nothing ever told it where, and the wrapper's working directory was the private companion
repo, so it invented `.run-YYYY-MM-DD/` and dumped there. Measured 2026-08-28: 32 such trees, 1716
files, 1.5 GB, against 2.2 MB of curated tracked data. Three filenames were 1.23 GB of that
(`roster_raw_responses.json` 435 MB, `roster_responses.json` 422 MB, `sources.json` 370 MB), all raw
timeline dumps with no value past the hour they were fetched. They sat untracked AND unignored, so
nothing backed them up and `git status` was too noisy to read. Scratch had no home, so it moved into
the archive.

THE SHAPE. Two locations, one rule each.

  SCRATCH   outside every git worktree, pruned on a timer, never committed, never depended on.
            Resolved by `run_dir()`. This is where the agent works.
  KEEP      a small allow-listed slice promoted into the private companion repo and committed.
            Written by `promote()`.

WHAT IS WORTH KEEPING, and why it is exactly this. The weekly self-evolve pass already has its
numerator (`archive/opportunities.jsonl`) and its denominator (`archive/pulls-YYYY-MM.jsonl`), and a
human has `archive/digests/`. The one thing missing was the ability to ask "what would TODAY's code
have decided about LAST month's inputs", and that needs the candidate set as it entered the
deterministic tail. This is not hypothetical: the demand-lane defect of 2026-08-27 was diagnosed and
its fix calibrated by replaying exactly this file across real days. So `candidates.json` is kept, and
`result.json` with it, because a replay is only meaningful next to what the run actually decided.

Everything else is reproducible, superseded, or raw third-party text that nobody should carry in
version control forever. Cost of the slice: about 35 KB per day, roughly 13 MB per year, against
roughly 47 MB PER DAY for the trees as they were.

THE ALLOW LIST IS THE POINT. `promote` copies named files under a size cap and refuses everything
else, so this can never silently grow back into a 1.5 GB archive. A file that is too large or not on
the list is REPORTED as skipped, never dropped in silence.
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
    """Load the vendored ``tools/datadir.py``, the ONE resolver allowed to say where real output goes.

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
        cand = parent / "tools" / "datadir.py"
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("daily_hotspots_datadir", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _datadir_mod = mod
            return mod
    raise RunStoreError(
        "cannot locate tools/datadir.py above %s.\n"
        "It is the only resolver allowed to decide where real-run output goes; without it this\n"
        "writer cannot prove its destination is outside the tool repo, so it refuses to write.\n"
        "Re-vendor it with the fleet's guard installer and retry." % here)


def _scratch_root() -> Path:
    """The scratch base: an explicit override, else a per-user cache dir. Never a repo, never $HOME.

    LOCALAPPDATA on Windows and XDG_CACHE_HOME elsewhere are the places an OS already sweeps and a
    backup already skips, which is what scratch wants. `$HOME` itself is deliberately not a fallback:
    a bare home directory is how the previous generation of this bug scattered real data.
    """
    override = os.environ.get("DAILY_HOTSPOTS_RUN_ROOT", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    # TEMP first, and this is a HARD constraint rather than a preference. The orchestration agent may
    # run under codex `exec -s workspace-write`, whose write sandbox is scoped to the working
    # directory plus temp, and the working directory must stay the private companion repo so the
    # collector can write the archive at all (a 2026-07-30 run answered ok=True and wrote nothing
    # because its cwd was System32). Scratch therefore has exactly one home the sandbox permits and
    # that is not a repo: temp. It is also swept by the OS, which is the right lifecycle for scratch.
    for var in ("TMPDIR", "TEMP", "TMP"):
        base = os.environ.get(var)
        if base:
            return Path(os.path.expanduser(base)) / SKILL / "runs"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(os.path.expanduser(base)) / SKILL / "runs"
    return Path.home() / ".cache" / SKILL / "runs"


def run_dir(run_id: str, create: bool = True) -> Path:
    """Scratch for ONE run. Guaranteed outside every git worktree, or it raises.

    The guarantee is the whole point, so it is checked rather than assumed: an operator who points
    DAILY_HOTSPOTS_RUN_ROOT at a checkout would otherwise recreate the exact bug this replaces, and
    would recreate it silently.
    """
    if not _RUN_ID_RE.match(run_id or ""):
        raise RunStoreError("run_id %r is not a safe directory name" % (run_id,))
    d = _scratch_root() / run_id
    _datadir().assert_outside_own_repo(d, SKILL)
    if _inside_any_worktree(d):
        raise RunStoreError(
            "refusing to use %s as run scratch: it is inside a git worktree.\n"
            "Scratch must live outside every repo. That is the entire reason this module exists:\n"
            "1.5 GB of raw captures once accumulated inside the private companion repo because the\n"
            "agent's working directory was a checkout. Point DAILY_HOTSPOTS_RUN_ROOT somewhere else."
            % d)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _inside_any_worktree(p: Path) -> bool:
    """True when p or any ancestor holds a .git entry. Pure filesystem, no subprocess, never raises."""
    try:
        cur = p if p.is_absolute() else p.resolve()
    except OSError:
        return False
    for node in (cur, *cur.parents):
        try:
            if (node / ".git").exists():
                return True
        except OSError:
            continue
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
    src = Path(src)
    if not src.is_dir():
        raise RunStoreError("run scratch does not exist: %s" % src)
    dest = keep_dir(archive_dir, run_id)
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
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
        promoted.append({"name": canonical, "from": p.name, "size": size})

    return {"run_id": run_id, "date": _run_date(run_id), "src": str(src), "dest": str(dest),
            "promoted": promoted, "skipped": skipped, "dry_run": bool(dry_run)}


def prune(root=None, retention_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> dict:
    """Delete scratch older than the retention window. Reports what it removed and what it kept.

    Only ever touches directories under the scratch root whose name carries a parseable date, so a
    misconfigured root cannot turn this into a recursive delete of something else.
    """
    root = Path(root) if root else _scratch_root()
    if not root.is_dir():
        return {"root": str(root), "removed": [], "kept": [], "skipped": [], "existed": False}
    if _inside_any_worktree(root):
        raise RunStoreError("refusing to prune %s: it is inside a git worktree" % root)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(0, int(retention_days)))
    removed, kept, skipped = [], [], []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
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
            if not dry_run:
                shutil.rmtree(child, ignore_errors=False)
            removed.append(child.name)
        else:
            kept.append(child.name)
    return {"root": str(root), "cutoff": cutoff.isoformat(), "removed": removed,
            "kept": kept, "skipped": skipped, "existed": True, "dry_run": bool(dry_run)}


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="run scratch location, promotion and retention")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dir", help="print the scratch dir for a run id (creating it)")
    p.add_argument("run_id")
    p.add_argument("--no-create", action="store_true")

    p = sub.add_parser("promote", help="copy the keep-slice of one run into the tracked archive")
    p.add_argument("run_id")
    p.add_argument("--src", default="", help="scratch dir (default: the resolved one for run_id)")
    p.add_argument("--archive-dir", default="")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("prune", help="delete scratch older than the retention window")
    p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.add_argument("--root", default="")
    p.add_argument("--dry-run", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "dir":
        print(run_dir(a.run_id, create=not a.no_create))
        return 0
    if a.cmd == "prune":
        print(json.dumps(prune(a.root or None, a.days, a.dry_run), ensure_ascii=False, indent=2))
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
