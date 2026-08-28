#!/usr/bin/env python3
"""Per-date COMPLETENESS of the digest archive: which DAYS are missing, named.

WHY THIS EXISTS
---------------
Nothing in this fleet asked that question. Two mechanisms look like they do:

  * The external task-health monitor watches the newest-descendant mtime of the archive tree. That
    is LIVENESS. It answers "did something happen recently", and the moment one good day lands,
    every older hole stops being reported forever. Measured 2026-08-28: 31 digests across a 45 day
    span, 14 days with no digest at all, and the monitor green the whole time.
  * wrapper.ps1's artifact probe asked whether archive/pulls-*.jsonl carried today's run_id. That
    ledger is written by `run.py --sources`, which returns before process() ever runs, so it proves
    collection BEGAN and nothing else. 2026-07-22: 142 pulls lines, no digest, "run end rc=0".

Liveness cannot see a hole behind it and a per-run probe cannot see a hole beside it. Completeness
is a third question and it needs its own answer, its own artifact and its own scheduled entry.

THE ONE INVARIANT
-----------------
"complete" and "did not check anything" are DIFFERENT OUTPUTS with DIFFERENT EXIT CODES. Every
degenerate input that a naive glob-and-diff scanner reports as clean is routed to CANNOT CHECK
instead: no archive, no digests/ tree, an unreadable digests/ tree, zero digests with no operator
supplied range to anchor on, an inverted range, a range that enumerates zero days. A scanner whose
silence is indistinguishable from a clean bill of health is worse than no scanner, because the
silence gets read as a verdict.

READER, mostly. Enumerating the archive is a read and it degrades honestly: it reports CANNOT CHECK
rather than inventing a range or an archive location. `--report` is the one WRITE path and it
hard-fails: the report is the artifact a monitor binds to, so a report that was not written must
never leave behind a zero exit code claiming everything is fine.

EXIT CODES
    0  COMPLETE      every day in the range has a non-empty digest. The day count is always printed.
    2  CANNOT CHECK  the archive could not be enumerated, or no range could be established. NOT a
                     pass, NOT a list of holes.
    3  HOLES         at least one day in the range has no digest, or has an empty one. Every missing
                     date is named on its own line.
    4  REPORT FAILED the scan itself completed but --report could not be written.
    1  is deliberately unused as a verdict, so that an interpreter level crash (bad import, syntax
       error) stays distinguishable from every answer this tool is capable of giving.

USAGE
    python completeness.py --archive-dir <path/to/archive> [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                           [--json] [--report <path>]

    With no --archive-dir it resolves the private companion repo through the same reader seam the
    rest of the skill uses (archive.find_archive_dir -> tools/datadir.py). With no --start it
    anchors on the EARLIEST digest present, which is the first day this install ever published.
    With no --end it stops at YESTERDAY (UTC), because today's digest is legitimately not written
    until the daily run finishes and flagging it would make the scanner cry wolf every morning.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_COMPLETE = 0
EXIT_CANNOT_CHECK = 2
EXIT_HOLES = 3
EXIT_REPORT_FAILED = 4

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
DIGEST_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


class CannotCheck(Exception):
    """The scan did not happen. Carries the reason so the operator is told WHY, not just told no.

    Separate from "found holes" on purpose and never collapsed into it: reporting every date in the
    range as missing when the archive is simply absent is technically true, operationally useless,
    and it trains whoever reads the alert to stop reading it.
    """


def _now_utc() -> datetime:
    """The clock, honouring DAILY_HOTSPOTS_NOW so the suite is deterministic.

    lib.now_utc is the fleet's clock and is used when importable. It is imported lazily and its
    absence is not fatal here: this scanner is a diagnostic that has to keep working on a machine
    where something else in the skill is broken, and the only thing it needs the clock for is the
    default end date.
    """
    try:
        from lib import now_utc  # type: ignore
        return now_utc()
    except Exception:
        raw = os.environ.get("DAILY_HOTSPOTS_NOW")
        if raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.now(timezone.utc)


def _parse_date(s: str, what: str) -> date:
    m = DATE_RE.match((s or "").strip())
    if not m:
        raise CannotCheck("%s is not a YYYY-MM-DD date: %r" % (what, s))
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as e:
        raise CannotCheck("%s is not a real calendar date: %r (%s)" % (what, s, e))


def resolve_archive(explicit: str | None) -> Path:
    """The archive directory, or CannotCheck saying which resolution failed.

    An explicit path is honoured as given (it is a diagnostic run over a directory the operator is
    pointing at). With nothing explicit it goes through archive.find_archive_dir, the same reader
    seam every other reader in this skill uses, so this tool cannot end up looking at a different
    archive than the pipeline writes to. An uninitialized install resolves to nothing and that is
    CANNOT CHECK, never "complete": a tool that has never been configured has not published zero
    holes, it has published nothing.
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise CannotCheck("archive dir does not exist: %s" % p)
        if not p.is_dir():
            raise CannotCheck("archive dir is not a directory: %s" % p)
        return p
    try:
        from archive import find_archive_dir
    except Exception as e:
        raise CannotCheck("cannot import the archive resolver (%s); pass --archive-dir" % e)
    try:
        p = find_archive_dir(None)
    except Exception as e:
        raise CannotCheck("the archive resolver refused to resolve a dir: %s" % e)
    if p is None:
        raise CannotCheck(
            "daily-hotspots has no private companion config, so there is no archive to scan. "
            "This is the correct state for a freshly cloned public skill. Set "
            "DAILY_HOTSPOTS_CONFIG, or pass --archive-dir for a one-off run. It is NOT a clean "
            "bill of health and is not reported as one.")
    p = Path(p)
    if not p.exists():
        raise CannotCheck("the resolved archive dir does not exist: %s" % p)
    if not p.is_dir():
        raise CannotCheck("the resolved archive dir is not a directory: %s" % p)
    return p


def scan_digests(archive: Path):
    """Enumerate archive/digests/<yyyy>/<date>.md. Returns (present, empty).

    `present` is the set of dates whose digest exists AND carries bytes. `empty` is the sorted list
    of dates whose digest file exists at zero length; those are counted as HOLES, not as days. An
    existence check is always one truncation away from certifying an empty day, and this tool is an
    existence check by nature, so the size is looked at rather than assumed.

    Any OSError while walking raises CannotCheck. A partially readable archive cannot answer the
    question it was asked, and answering it anyway with whatever was readable is how a scan of half
    a directory gets reported as a scan of all of it.
    """
    root = archive / "digests"
    if not root.exists():
        raise CannotCheck(
            "no digests/ tree under %s. Nothing has ever been published here, or this is not the "
            "archive dir. Either way no day was examined." % archive)
    if not root.is_dir():
        raise CannotCheck("%s exists but is not a directory, so it cannot be enumerated" % root)

    present: set[date] = set()
    empty: list[str] = []
    unreadable: list[str] = []
    try:
        year_dirs = sorted(root.iterdir())
    except OSError as e:
        raise CannotCheck("cannot list %s: %s" % (root, e))
    for ydir in year_dirs:
        try:
            if not ydir.is_dir():
                continue
            entries = sorted(ydir.iterdir())
        except OSError as e:
            raise CannotCheck("cannot list %s: %s" % (ydir, e))
        for f in entries:
            m = DIGEST_RE.match(f.name)
            if not m:
                continue
            try:
                size = f.stat().st_size
            except OSError as e:
                unreadable.append("%s (%s)" % (f, e))
                continue
            if size <= 0:
                empty.append(m.group(1))
                continue
            try:
                present.add(_parse_date(m.group(1), "digest filename"))
            except CannotCheck:
                # 2026-02-30.md and friends: a name that parses as the shape but not as a day.
                # Reported, not silently dropped, because a directory full of those would otherwise
                # scan as an empty archive.
                unreadable.append("%s (filename is not a real calendar date)" % f)
    if unreadable:
        raise CannotCheck(
            "%d digest file(s) could not be read or interpreted, so the archive was only partly "
            "examined: %s" % (len(unreadable), "; ".join(sorted(unreadable)[:10])))
    return present, sorted(set(empty))


def choose_range(present: set[date], start: str | None, end: str | None):
    """Establish the range to check, or CannotCheck.

    Defaults: start at the EARLIEST digest present (the first day this install published), end
    YESTERDAY in UTC (today's digest is legitimately absent until the daily run finishes, and a
    scanner that flags it every morning is a scanner that gets muted).

    With zero digests and no explicit --start there is nothing to anchor on. That is CANNOT CHECK.
    Deriving a start from thin air would let an empty archive print either verdict depending on the
    invented number, which is the same thing as printing neither.
    """
    if start:
        d0 = _parse_date(start, "--start")
    else:
        if not present:
            raise CannotCheck(
                "the archive holds no digests and no --start was given, so there is no range to "
                "check against. Zero days examined is not zero holes.")
        d0 = min(present)
    if end:
        d1 = _parse_date(end, "--end")
    else:
        d1 = (_now_utc().date() - timedelta(days=1))
    if d0 > d1:
        raise CannotCheck(
            "the range is inverted (start %s is after end %s), so it enumerates zero days"
            % (d0.isoformat(), d1.isoformat()))
    return d0, d1


def enumerate_days(d0: date, d1: date):
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def write_report(path: Path, doc: dict) -> None:
    """Write the JSON report ATOMICALLY. WRITER: hard-fails, no fallback location.

    This file is what a task-health entry binds to, so the two dishonest outcomes are (a) a write
    that half happened, leaving a monitor parsing a truncated document, and (b) a write that did
    not happen while the process still exits 0, leaving the monitor looking at yesterday's report
    next to today's green. (a) is answered by temp-file plus os.replace in the same directory; (b)
    is answered by letting the failure reach the caller, which turns it into EXIT_REPORT_FAILED.

    The parent directory is created, but only as a directory: if something non-directory is sitting
    at that path the mkdir raises and the run reports the failure rather than picking somewhere
    else to write. There is no second location; a report filed where nobody is looking is the same
    as no report, minus the alert.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / ("." + path.name + "." + str(os.getpid()) + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="completeness.py",
        description="Name the days with no daily-hotspots digest. Exit 0 complete, 3 holes, "
                    "2 could not check, 4 report write failed.")
    p.add_argument("--archive-dir", default=None,
                   help="the archive dir (the one holding digests/). Default: resolved through "
                        "the private companion repo, same as every other reader.")
    p.add_argument("--start", default=None,
                   help="first date to require, YYYY-MM-DD. Default: the earliest digest present.")
    p.add_argument("--end", default=None,
                   help="last date to require, YYYY-MM-DD. Default: yesterday, UTC.")
    p.add_argument("--json", action="store_true", help="emit the report as JSON on stdout.")
    p.add_argument("--report", default=None,
                   help="also write the JSON report to this path, atomically. A write failure is "
                        "reported as exit 4 and never as success.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        archive = resolve_archive(args.archive_dir)
        present, empty = scan_digests(archive)
        d0, d1 = choose_range(present, args.start, args.end)
        days = enumerate_days(d0, d1)
        if not days:
            raise CannotCheck("the range enumerated zero days, so nothing was examined")
    except CannotCheck as e:
        # stderr, and the word CANNOT, so a human skimming a log cannot mistake it for a pass.
        sys.stderr.write("daily-hotspots completeness: CANNOT CHECK: %s\n" % e)
        if args.json:
            sys.stdout.write(json.dumps(
                {"status": "cannot_check", "reason": str(e), "days_checked": 0,
                 "missing": None}, ensure_ascii=False, indent=2) + "\n")
        return EXIT_CANNOT_CHECK

    empty_in_range = [d for d in empty
                      if d0.isoformat() <= d <= d1.isoformat()]
    missing = [d.isoformat() for d in days if d not in present]
    doc = {
        "status": "holes" if missing else "complete",
        "archive_dir": str(archive),
        "range_start": d0.isoformat(),
        "range_end": d1.isoformat(),
        "days_checked": len(days),
        "days_present": len(days) - len(missing),
        "missing": missing,
        "empty_digests": empty_in_range,
        "generated_at": _now_utc().isoformat(),
    }

    if args.json:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    else:
        out = sys.stdout
        out.write("daily-hotspots completeness\n")
        out.write("  archive: %s\n" % archive)
        out.write("  range:   %s .. %s (%d days checked)\n"
                  % (d0.isoformat(), d1.isoformat(), len(days)))
        out.write("  present: %d    missing: %d\n" % (len(days) - len(missing), len(missing)))
        for d in missing:
            note = "  (digest file exists but is empty)" if d in empty_in_range else ""
            out.write("MISSING %s%s\n" % (d, note))
        if missing:
            out.write("RESULT: HOLES (%d of %d days have no digest)\n" % (len(missing), len(days)))
        else:
            out.write("RESULT: COMPLETE (%d of %d days have a digest)\n" % (len(days), len(days)))
        out.flush()

    if args.report:
        try:
            write_report(Path(args.report).expanduser(), doc)
        except Exception as e:
            sys.stderr.write(
                "daily-hotspots completeness: REPORT WRITE FAILED at %s: %s\n"
                "The scan itself ran (status=%s), but the artifact a monitor binds to was not\n"
                "written, so this run does not get to exit successfully.\n"
                % (args.report, e, doc["status"]))
            return EXIT_REPORT_FAILED

    return EXIT_HOLES if missing else EXIT_COMPLETE


if __name__ == "__main__":
    sys.exit(main())
