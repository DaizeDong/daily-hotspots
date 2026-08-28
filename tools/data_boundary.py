#!/usr/bin/env python3
"""data_boundary -- a public skill repo is an UNINITIALIZED TOOL. Enforced by construction.

WHY THIS EXISTS, AND WHY pii_guard WAS NOT ENOUGH
-------------------------------------------------
pii_guard is a sieve at the exit: it reads what you are about to publish and looks for things that
smell private. It works, and it has caught real leaks. But it is the wrong primary control, because
it accepts the premise that real data is flowing toward the exit at all.

The 2026-07 audit found real-run output sitting in PUBLIC repos -- a research skill's verdict ledger,
a shopping skill's purchase records, a social skill's posting account -- not because anyone pasted it
into a doc, but because the skills WROTE it there during real runs. `metrics/*.jsonl` was append-only
telemetry of the operator's actual life, git-tracked, on GitHub. (What it recorded was his; the point
here is the mechanism, so the specifics stay out of this file.)

These repos already had a private-companion-config boundary. It only ever covered INPUTS -- the
credentials, the mailboxes, the account slugs. Nothing covered OUTPUTS: what the skill LEARNED from a
real run. That is the door every remaining leak walked out of, and no amount of content scanning
fixes a door.

So: every path in a public repo belongs to exactly one class, declared in `.dataclass.json`.

  TOOL     code, SKILL.md, docs.                 Public. Hand-written. Contains no data at all.
  FIXTURE  tests, goldens, examples.             Public, but SYNTHETIC ONLY, and PRODUCED BY A
                                                 GENERATOR -- never a copy of a real record. The
                                                 generator is the proof: a hand-pasted real email
                                                 cannot be regenerated, so it fails here.
  DATA     anything a real run produced:         PRIVATE companion repo. Physically absent from the
           telemetry, real goldens, calibration, public repo. The loader resolves it from outside.
           caches, verdicts, config.             The public repo ships only a `*.example` schema.

THE POINT IS NOT THAT THE DATA IS HIDDEN. It is that an agent writing the public repo has NOTHING
REAL WITHIN REACH to reuse. You cannot copy a convenient example out of a file that is not there.
That closes the artifact leak completely -- which is more than a scanner can promise.

WHAT IT DOES NOT CLOSE
----------------------
Prose. An agent that is reading the operator's inbox can still type a real employer's name into a
CHANGELOG from memory. No boundary reaches that; deleting a file does not make anyone forget. That
is what pii_guard is FOR, and it is why it stays -- demoted from primary control to backstop.

CHECKS
  1. no DATA-class path is git-tracked                       (the door)
  2. every FIXTURE path is byte-identical to what tools/make_fixtures.py produces  (the copy-paste)
  3. every DATA path has a `<path>.example` schema in the repo (so the tool is usable uninitialized)
  4. no git-tracked file has the SHAPE of real-run output unless it is declared  (the empty manifest)

`data_sealed` is a fourth, narrower declaration: a path that USED to hold real data, has been
purged, and must stay dead. Checked like DATA in (1), exempt from (3) -- a dead path is not owed a
schema; shipping one would advertise a path the tool no longer uses.

WHY CHECK 4 EXISTS (2026-07-31)
-------------------------------
Checks 1..3 only ever look at what the manifest DECLARES. A manifest that declares nothing therefore
enforces nothing: an independent verifier cloned six public repos whose `data` list was empty,
committed `metrics/live-runs.jsonl` and `runs/transcript.json` into each, and this script exited 0
on all six. Each of those manifests carried a careful prose note concluding that the skill writes its
real-run output outside the repo. The notes were largely accurate. They were also inert: PROSE IS
NOT A CONTROL, and every one of those skills can still be pointed at a repo-relative path by a flag
or an env var (`--archive-dir`, `--state-path`, `--status-json`, `LLMCALL_LEDGER`, `SCHEDULE_DB_PATH`),
so "the default resolver points elsewhere" was never the same statement as "nothing can land here".

So the empty declaration now has to survive a mechanical question instead of an argument: does this
repo TRACK anything shaped like the output a real run produces? The shapes below are not a guess at
what data looks like -- that is pii_guard's losing game -- they are the literal places and names this
fleet's skills write to. A repo with nothing to declare passes because the repo is genuinely empty of
run output, and it starts failing the moment that stops being true.

An exemption is possible and it is per-path: list the file under `tool` in the manifest with a
reason. That is an allowlist entry visible in the diff, which is the opposite of a paragraph.

  python data_boundary.py [--repo .]     exit 0 clean / 1 violation
Stdlib only.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

MANIFEST = ".dataclass.json"

# A file whose name says "this is a published SHAPE, not a record". Exempt from the shape check, in
# either naming convention, because check 3 REQUIRES a schema next to every declared DATA path and
# `metrics/live-runs.jsonl.example` must not therefore become a violation of check 4.
SHAPE_EXEMPT = re.compile(r"\.(example|sample|tmpl|template)(\.|$)", re.I)

# The shapes real runs actually leave behind in this fleet. Each entry is (why, pattern).
#
# CALIBRATED 2026-08-27 AGAINST A REAL RUN, WHICH IS THE ONLY WAY THIS LIST MEANS ANYTHING.
# The predecessor list was written from the fleet's OTHER skills (metrics/*.jsonl, runs/, a handful
# of ledger names) and never once tested against what THIS skill puts on disk. Measured: it matched
# 0 of the 116 files a single real daily run of daily-hotspots produced, and 3 of the 40 files in
# the live archive. It printed green every day because it was being fed a repo that contains no run
# output, so nothing ever exercised it -- "clean" and "nothing to check" were the same output, on
# the primary control, which is the exact failure mode this file's own docstring names.
#
# A real run of this skill writes, into the PRIVATE companion repo:
#   .run-YYYY-MM-DD/            a whole scratch tree per run (and .run-YYYY-MM-DD-rerun-HHMM/):
#                               raw third-party captures, per-handle and per-subreddit shards,
#                               fetch logs, stderr dumps, one-off scraper scripts, intermediate
#                               candidate/card json, and a backup copy of the day's digest
#   archive/opportunities.jsonl the canonical ledger  (plus qualified copies, e.g.
#                               opportunities.after-interactive-run.jsonl)
#   archive/pulls-YYYY-MM.jsonl the month-sharded denominator log
#   archive/dedup-state.json    cross-day dedup state
#   archive/digests/YYYY/YYYY-MM-DD.md    the day's digest
#   archive/identity-sweep-YYYY-MM.json   the monthly sweep record
#   roster.json, roster-review.md         the roster the skill evolves from real runs
#
# Each shape below names something from that list. Verified after the fact: 116/116 run-tree files
# and 39/40 archive files now match (the one that does not is `archive/.gitkeep`, which is a
# directory placeholder and correctly not run output), while the 99 tracked files of this public
# repo produce exactly the matches enumerated in .dataclass.json and no others.
RUN_SHAPES = (
    ("a jsonl ledger under metrics/ -- the exact shape of the 2026-07 leak",
     re.compile(r"(^|/)metrics/.*\.(jsonl|ndjson)$", re.I)),
    ("anything under a runs/ directory -- per-run output",
     re.compile(r"(^|/)runs/", re.I)),
    ("a DATED RUN DIRECTORY -- one whole tree per real run, scratch and captures alike",
     re.compile(r"(^|/)\.?runs?[-_]\d{4}-\d{2}-\d{2}", re.I)),
    ("a dated file under an output directory -- a dated file is a record of a day, not a tool",
     re.compile(r"(^|/)(reports?|archive|digests?|logs?|out|output|state|snapshots?)/"
                r".*(\d{4}-\d{2}-\d{2}|\d{4}-\d{2}(?!\d)|\d{8})", re.I)),
    ("a jsonl ledger under an output directory",
     re.compile(r"(^|/)(archive|logs?|state|out|output|runs|reports?)/.*\.(jsonl|ndjson)$", re.I)),
    ("a DATE-STAMPED or MONTH-SHARDED record -- the date is there because a run happened",
     re.compile(r"(^|/)[A-Za-z0-9_.-]*[-_]\d{4}-\d{2}(-\d{2})?"
                r"([-_.][A-Za-z0-9_.-]*)?\.(jsonl|ndjson|json|md|txt|csv|log|rss|html|py)$", re.I)),
    ("a filename this fleet uses for a live ledger",
     re.compile(r"(^|/)(ledger|live-runs|events|dry-run|verdicts|opportunities|history|transcript"
                r"|pulls-\d{4}-\d{2})([-_.][A-Za-z0-9_.-]+)?\.(jsonl|ndjson)$", re.I)),
    ("a filename this fleet uses for real-run state",
     re.compile(r"(^|/)(escalation_state|fleet-check-status|bandit-state|throttle-state"
                r"|dedup-state)\.json$", re.I)),
    # `roster-evolution.md` is the DESIGN NOTE for the roster and must not match; `roster-review.md`
    # is the report a real run emits and must. Hence the .md arm is a name, not a wildcard: a shape
    # broad enough to swallow the documentation would be turned off within a week.
    ("a ROSTER -- this skill evolves it from real runs, so it is output, not configuration",
     re.compile(r"(^|/)roster([-_][A-Za-z0-9_.-]*)?\.(json|jsonl)$"
                r"|(^|/)roster[-_]review\.md$", re.I)),
    ("a RAW THIRD-PARTY CAPTURE -- whatever a source returned on the day it was polled",
     re.compile(r"(^|/)(raw|reddit_raw|captures?|parts\d*|shards?|chunks?|_d)/"
                r"|(^|/)raw[-_][A-Za-z0-9_.-]+\.(json|jsonl|ndjson|txt|html)$"
                r"|[-_](raw|out|log|err|dump)\.(json|jsonl|ndjson|txt|log|md)$", re.I)),
    ("a COLLECTION INTERMEDIATE -- the pipeline's own working set for one run",
     re.compile(r"(^|/)(candidates?|signals?|clusters?|cards|supply_cards|demand_cards|demand_\w+"
                r"|all_jobs|raw_jobs|sources|sources_result|result|run_out|roster_plan"
                r"|roster_raw_\d+|roster_shard_\d+|dry)\.(json|jsonl|ndjson)$", re.I)),
    ("a database file -- nobody hand-writes one, so it came from a run",
     re.compile(r"\.(db|sqlite|sqlite3)$", re.I)),
)


class GitError(RuntimeError):
    """A git invocation this check depends on did not succeed.

    Raised, never swallowed. See _run for why an exception and not an empty string.
    """


def _run(args, cwd):
    """Run a git command and return its stdout.

    THIS USED TO FAIL OPEN, and on the PRIMARY control that is worse than on the backstop.
    The old body was `return p.stdout if p.returncode == 0 else ""`. `tracked()` read that
    empty string as an ANSWER (no tracked files), every per-file check then iterated zero
    times, and main() printed "data_boundary: clean (... 0 tracked files ...)" and exited 0
    having examined nothing. Anything that breaks git produced that: a shell .git directory
    (a shape that has actually occurred on this machine), a directory that is not a repo,
    git missing from PATH, an index.lock, a dubious-ownership refusal.

    pii_guard and dash_guard were hardened against exactly this and this file was the
    outlier, which meant the two scanners disagreed about the same broken environment:
    pii_guard --tree exited 2 saying NOTHING was examined while data_boundary next to it
    printed clean and exited 0. install.py:86 calls this file the PRIMARY control, so the
    control was the one lying.

    A nonzero exit now RAISES, carrying git's own stderr. A clean report requires that the
    scan actually happened.

    encoding="utf-8" is load bearing, not decoration: without it text=True decodes with the
    locale codepage (cp936 here) while git emits UTF-8, and errors="replace" silently turns
    a repo path containing non-ASCII into mojibake. The `git ls-files` that follows then
    runs in a directory that does not exist.
    """
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except (OSError, ValueError) as e:
        raise GitError("cannot execute `%s` in %s: %s\n"
                       "  git must be runnable for this check to mean anything."
                       % (" ".join(args), cwd, e)) from e
    if p.returncode != 0:
        raise GitError("`%s` exited %d in %s\n  %s"
                       % (" ".join(args), p.returncode, cwd,
                          (p.stderr or "").strip() or "(no stderr)"))
    return p.stdout


def _repo_root(start):
    """Resolve the work tree root, or raise.

    The old body ended in `or start`, so a directory that is not a work tree quietly became
    its own "repo root". Everything downstream then scanned a non-repo and reported clean.
    That is the same defect pii_guard was fixed for; refusing here is the whole point.
    """
    root = _run(["git", "rev-parse", "--show-toplevel"], start).strip()
    if not root:
        raise GitError("git named no toplevel for %s (is it a work tree?)" % start)
    return root


def load_manifest(root):
    p = os.path.join(root, MANIFEST)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def tracked(root):
    """Every git-tracked path, NUL-separated so non-ASCII names survive.

    Why -z: `git ls-files` without it renders any path containing a non-ASCII byte as a
    C-quoted escape string (quotes included, e.g. "ä¸­...md"). Those strings are
    not paths. They do not open, and every regex anchored on a real suffix misses them.
    Callers read this list as an ANSWER, so such a file got counted in the tracked total and
    then silently excluded from every per-file match. Measured 2026-08-19: a tracked
    metrics/<CJK>-live-runs.jsonl produced "clean (... 3 tracked files carry no real-run
    shape)" with rc=0, while byte-identical content under an ASCII name produced rc=1.
    -z makes git emit raw bytes with NUL separators, so the name round-trips.
    """
    return {p for p in _run(["git", "ls-files", "-z"], root).split("\0") if p}


def _covered(rel, pats):
    """Is `rel` the declared path itself, or under it when the declaration names a directory?"""
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in pats)


def check_data_not_tracked(root, m, files, out):
    """A DATA path in the index means the skill wrote the operator's real life into a public repo.

    `data_sealed` is the same rule for a path that is DEAD: it held real data once, the data has
    been moved out and purged from history, and it must never come back. .gitignore already covers
    it, but .gitignore is advisory -- `git add -f` walks straight through, and an agent that wants
    a file tracked will find that flag. This makes the seal enforceable. It differs from `data`
    only in that a dead path is not owed a schema: publishing one would advertise a path the tool
    no longer uses.
    """
    pats = m.get("data", []) + m.get("data_sealed", [])
    for rel in sorted(files):
        if _covered(rel, pats):
            out.append(("DATA-TRACKED", rel,
                        "real-run output must live in the private companion config, not here"))


def check_data_has_schema(root, m, out):
    """An uninitialized tool must still be USABLE: ship the shape, never the contents.

    Without this, "keep real data out of the repo" degrades into "the repo no longer explains what it
    expects", and the next person to wire it up guesses -- or, far more likely, an agent recreates a
    convenient real-looking file to work against. The schema is what makes the empty tool honest.

    Both conventions are accepted: `x.jsonl.example` and the older `config.example.json`. A DATA path
    ending in `/` is a whole output directory; there is no single shape to publish for it.
    """
    for pat in m.get("data", []):
        if pat.endswith("/"):
            continue
        stem, ext = os.path.splitext(pat)
        if any(os.path.isfile(os.path.join(root, c))
               for c in (pat + ".example", stem + ".example" + ext)):
            continue
        out.append(("NO-SCHEMA", pat + ".example",
                    "publish the schema so the tool is usable uninitialized"))


def check_fixtures_are_generated(root, m, out):
    """The whole reason fixtures are generated: a real record CANNOT be regenerated.

    Hand-pasting a real email into a golden file is the single move that produced most of the 2026-07
    leaks. Requiring byte-equality with a deterministic generator makes that move fail loudly at
    commit time, instead of relying on someone noticing, months later, that a sender address in a
    test fixture was somebody's actual inbox.
    """
    fixtures = m.get("fixture", [])
    if not fixtures:
        return
    gen = os.path.join(root, "tools", "make_fixtures.py")
    if not os.path.isfile(gen):
        out.append(("NO-GENERATOR", "tools/make_fixtures.py",
                    "fixtures are declared but nothing can regenerate them -- so nothing proves "
                    "they are synthetic"))
        return
    with tempfile.TemporaryDirectory() as td:
        p = subprocess.run([sys.executable, gen, "--out", td], cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            out.append(("GENERATOR-FAILED", "tools/make_fixtures.py",
                        (p.stderr or "").strip().splitlines()[-1] if p.stderr else "non-zero exit"))
            return
        for rel in fixtures:
            live = os.path.join(root, rel)
            fresh = os.path.join(td, os.path.basename(rel))
            if not os.path.isfile(fresh):
                out.append(("NOT-GENERATED", rel, "generator does not produce this fixture"))
                continue
            if not os.path.isfile(live):
                out.append(("MISSING", rel, "declared fixture is absent; run make_fixtures.py"))
                continue
            a = open(live, "rb").read().replace(b"\r\n", b"\n")
            b = open(fresh, "rb").read().replace(b"\r\n", b"\n")
            if a != b:
                out.append(("HAND-EDITED", rel,
                            "does not match the generator -- a real record cannot be regenerated. "
                            "Change the SCHEMA, then run: python tools/make_fixtures.py"))


def check_empty_data_is_audited(root, m, out):
    """An empty `data` list has to be a conclusion someone reached, not a default.

    THIS IS THE CHECK THAT MAKES `_audited` MEAN ANYTHING. The convention of recording an
    `_audited` (or `_armed`) note next to an empty data list has been followed for a long time:
    measured 2026-08-20, ten of the eleven manifests with an empty list carried one. And until
    that date NOTHING READ THEM. `grep -rn "_armed|_audited"` over the guard sources returned
    zero hits, so the entire evidence that "this skill genuinely produces no in-repo data" was a
    sentence no program had ever looked at. data_boundary is the primary control, and its own
    docstring says PROSE IS NOT A CONTROL; this was prose.

    An empty list is the single most consequential value in this file, because every per-file
    check below iterates it. Empty means "assert nothing", and it is also exactly what a fresh
    manifest looks like. Requiring a human-written reason is what separates the two.

    Deliberately NOT satisfied by any non-empty string of whitespace, and deliberately naming
    both keys: `_armed` is the older spelling and several manifests still use it.
    """
    if m.get("data"):
        return
    note = m.get("_audited") or m.get("_armed")
    if note is None or not str(note).strip():
        out.append(("UNAUDITED", MANIFEST,
                    "an empty \"data\" list asserts nothing, and nothing here says that was a "
                    "finding rather than a default. Add \"_audited\" naming where this skill's "
                    "real output actually goes (usually its private companion repo) and how that "
                    "was verified."))


def check_no_undeclared_run_shapes(root, m, files, out):
    """The check that survives an EMPTY manifest -- see "WHY CHECK 4 EXISTS" at the top of this file.

    Checks 1..3 are declaration-driven, so a repo that declares nothing is checked for nothing. This
    one runs off the tracked file list instead: any file shaped like real-run output must be
    ACCOUNTED FOR by name in the manifest, under `data`/`data_sealed` (check 1 then reports it),
    `fixture` (check 2 then proves it is generator-reproducible), or `tool` (a per-path allowlist
    entry, which shows up in the diff and has to be argued for -- the .pii-allow pattern).

    Default-deny is the point. Nothing here needs the manifest to be right; it needs the repo to be
    empty of run output, which is a fact about the working tree that no note can talk its way out of.
    """
    declared = (m.get("data", []) + m.get("data_sealed", [])
                + m.get("fixture", []) + m.get("tool", []))
    for rel in sorted(files):
        if _covered(rel, declared) or SHAPE_EXEMPT.search(os.path.basename(rel)):
            continue
        for why, pat in RUN_SHAPES:
            if pat.search(rel):
                out.append(("RUN-SHAPE", rel, why))
                break


def shape_of(rel):
    """The shape `rel` wears, or None. `("(shape-exempt)", ...)` when a `.example`-style name."""
    if SHAPE_EXEMPT.search(os.path.basename(rel)):
        return "(shape-exempt: a published schema, not a record)"
    for why, pat in RUN_SHAPES:
        if pat.search(rel):
            return why
    return None


def explain(paths):
    """Print, per path, which run shape it wears. Exit 0 always: this is a diagnostic, not a gate.

    It exists because the shape list is the one part of this file that can be WRONG IN SILENCE.
    A pattern set that matches nothing is indistinguishable, from the outside, from a repo with
    nothing to match, and that is precisely how the previous list survived: it named the shapes of
    the fleet's OTHER skills and was never once held up against what this one writes. `--explain`
    is how you hold it up, against a real run's file names, before trusting a green report.
    """
    for p in paths:
        rel = p.replace("\\", "/").lstrip("./")
        why = shape_of(rel)
        print("%-4s %-60s %s" % ("HIT" if why else "----", p, why or "(no shape matches)"))
    return 0


def _resolve_companion(start):
    """Ask tools/datadir.py where this skill's private data lives. ONE resolver, here too.

    Not a second probe order. The whole point of datadir.py is that exactly one piece of code
    answers "where does real-run output go"; a checker that answered it independently could
    disagree with the writer it is auditing, and the disagreement would look like a clean report.
    """
    dd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datadir.py")
    if not os.path.isfile(dd_path):
        return None
    spec = importlib.util.spec_from_file_location("_data_boundary_datadir", dd_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        root = _repo_root(start)
    except GitError:
        root = start
    # DataDirInsideOwnRepo is deliberately NOT caught: a data dir pointing into the tool repo is a
    # defect, and a traceback naming it is a better outcome than "no companion resolved".
    p = mod.resolve_data_dir(os.path.basename(os.path.normpath(root)))
    return str(p) if p else None


def check_companion(companion, max_report):
    """Is the PRIVATE companion repo's real-run output actually under version control?

    THIS IS NOT THE SAME QUESTION AS THE ONE ABOVE, and conflating them has already caused one
    wrong verdict in this fleet. The rule is "DATA never in a PUBLIC repo", not "DATA never in
    git": a private companion repo is exactly where a person's real output legitimately lives, with
    a remote, a history and a backup. So this check does not object to run output being tracked
    there. It objects to run output being NEITHER tracked NOR ignored -- sitting loose in the
    working tree, in a limbo where it is not backed up by anything and where it buries `git status`
    under so much noise that a genuinely new file cannot be seen. Measured on the operator's
    companion 2026-08-27: 35 loose run trees, 1640 files, 1.5 GB, against 40 tracked files. The
    signal-to-noise ratio of `git status` there was 0.

    Deliberately opt-in (`--companion`) and deliberately NOT part of the public repo's CI: there is
    no companion on a CI runner, and a check that cannot run must say so rather than pass. Absent
    companion exits 3, which is neither clean nor a violation.
    """
    try:
        root = _repo_root(companion)
        status = _run(["git", "status", "--porcelain", "--untracked-files=all", "-z"], root)
        tracked_files = tracked(root)
    except GitError as e:
        print("data_boundary --companion: SCAN FAILED, NOTHING was examined.\n  %s" % e,
              file=sys.stderr)
        return 2

    loose = sorted(f[3:] for f in status.split("\0") if f.startswith("?? "))
    loose_shaped = [f for f in loose if shape_of(f) and not f.endswith("/")]
    # `git status -z` reports an untracked DIRECTORY as one entry ending in `/`; expand it so the
    # count is files, not directories. A directory reported as "1 item" is how 1640 files hide.
    for d in [f for f in loose if f.endswith("/")]:
        for r, _dirs, fs in os.walk(os.path.join(root, d)):
            for x in fs:
                rel = os.path.relpath(os.path.join(r, x), root).replace("\\", "/")
                if shape_of(rel):
                    loose_shaped.append(rel)
    tracked_shaped = [f for f in tracked_files if shape_of(f)]

    print("data_boundary --companion: %s" % root)
    print("  tracked files wearing a run shape:  %d   (correct: this is the private repo)"
          % len(tracked_shaped))
    print("  LOOSE files wearing a run shape:    %d   (neither tracked nor ignored)"
          % len(loose_shaped))
    if not loose_shaped:
        return 0

    shown = sorted(loose_shaped)[:max_report]
    for f in shown:
        print("    %s" % f, file=sys.stderr)
    withheld = len(loose_shaped) - len(shown)
    # The bound and what it dropped are both printed. A listing that silently stops at N teaches
    # the reader that N is the whole answer.
    print("    ... %d more not listed (--max-report %d)" % (withheld, max_report), file=sys.stderr)
    print("\n%d file(s) of real-run output are loose in %s: not tracked, and not ignored either.\n"
          "Decide, do not drift. Both dispositions are legitimate in a PRIVATE repo:\n"
          "  TRACK   if the files are curated output you would want to diff and restore.\n"
          "  IGNORE  if they are per-run scratch: raw third-party captures, fetch logs, stderr\n"
          "          dumps, one-off scripts. Check first that the curated record is already\n"
          "          tracked, then add the pattern to that repo's .gitignore.\n"
          "Leaving them loose is the one option that is not a decision."
          % (len(loose_shaped), root), file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description="Enforce the TOOL / FIXTURE / DATA boundary.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--explain", nargs="+", metavar="PATH",
                    help="diagnostic: print which run shape each PATH wears, then exit 0")
    ap.add_argument("--companion", action="store_true",
                    help="also audit the PRIVATE companion repo for run output that is neither "
                         "tracked nor ignored (exit 3 if no companion resolves: NOTHING checked)")
    ap.add_argument("--companion-dir", metavar="PATH",
                    help="the companion repo to audit, instead of resolving one")
    ap.add_argument("--max-report", type=int, default=20,
                    help="cap the --companion listing (the cap and the count withheld are printed)")
    a = ap.parse_args()

    if a.explain:
        return explain(a.explain)

    if a.companion or a.companion_dir:
        comp = a.companion_dir or _resolve_companion(os.path.abspath(a.repo))
        if comp is None:
            print("data_boundary --companion: no private companion repo resolved, so NOTHING was\n"
                  "  examined. This is not a clean bill of health. Point $%s_CONFIG at it, or\n"
                  "  pass --companion-dir."
                  % os.path.basename(os.path.abspath(a.repo)).upper().replace("-", "_"),
                  file=sys.stderr)
            return 3
        return check_companion(comp, a.max_report)

    try:
        root = _repo_root(os.path.abspath(a.repo))
        files = tracked(root)
    except GitError as e:
        # Exit 2, distinct from 0 (clean) and 1 (violations), mirroring pii_guard. "clean" and
        # "never ran" must not be the same output on the primary control.
        print("data_boundary: SCAN FAILED, git could not be used, so NOTHING was examined.\n  %s"
              % e, file=sys.stderr)
        return 2

    if not files:
        # AN EMPTY FILE LIST IS NOT A CLEAN REPO (fixed 2026-08-28, negative control:
        # tools/test_data_boundary.py::test_zero_tracked_files_is_not_a_clean_bill_of_health).
        #
        # `git ls-files` can exit 0 and hand back nothing -- a fresh work tree, an index that git
        # rebuilt as empty, a `--repo` pointed one directory off. Every per-file check below then
        # iterates zero times and the summary line printed "clean (... 0 tracked files scanned
        # ...)" with rc=0. The count inside a success message was the ONLY thing separating that
        # from a real pass, and this file's own `_run` docstring is a paragraph about how that
        # exact shape was the defect on the primary control. Same verdict as an unusable git,
        # because it is the same fact: nothing was examined.
        print("data_boundary: 0 tracked files in %s, so NOTHING was examined. This is not a clean\n"
              "  bill of health -- the scan had no input. Check that --repo names the work tree\n"
              "  you meant and that the index is populated (`git ls-files | head`)." % root,
              file=sys.stderr)
        return 2

    m = load_manifest(root)
    manifest_absent = m is None
    if manifest_absent:
        # A MISSING MANIFEST USED TO DISARM THE WHOLE GATE (fixed 2026-08-28, negative controls:
        # test_no_manifest_does_not_launder_a_tracked_run_artifact and
        # test_no_manifest_is_reported_as_not_armed_not_as_clean).
        #
        # The old body returned 0 right here, so the one-line route past the primary control was
        # `rm .dataclass.json`: the repo could then track an entire real archive and both hooks
        # would report success. That the fail-open was KNOWN is written into CI --
        # .github/workflows/pii-guard.yml carries a `test -f .dataclass.json` step whose error text
        # says data_boundary "would exit 0 on every run and leave the primary control inert". A
        # workaround in one caller is not a property of the gate, and the hooks never had it.
        #
        # Check 4 is manifest-INDEPENDENT by construction: it runs off the tracked file list and
        # asks whether anything WEARS the shape of run output. So it still runs, against an empty
        # manifest, and a run artifact is still caught. Only the declaration-driven checks (1, 2,
        # 3, 5) are genuinely unanswerable without a manifest, and that is reported as NOT ARMED
        # with its own exit code rather than as a pass.
        m = {}
    out = []
    check_data_not_tracked(root, m, files, out)
    check_data_has_schema(root, m, out)
    check_fixtures_are_generated(root, m, out)
    check_no_undeclared_run_shapes(root, m, files, out)
    if not manifest_absent:
        # Check 5 asks whether an EMPTY data list was a finding. With no manifest at all there is
        # no list to have been a finding, and reporting UNAUDITED against a file that does not
        # exist would tell the reader to edit a note when what is missing is the whole manifest.
        # The absence is reported below instead, in the exit code, which no reader can skim past.
        check_empty_data_is_audited(root, m, out)

    if not out and manifest_absent:
        print("data_boundary: NOT ARMED. There is no %s in %s, so checks 1, 2, 3 and 5 asserted\n"
              "  NOTHING about this repo. Check 4 ran (it needs no manifest) and found no tracked\n"
              "  file wearing the shape of real-run output, across %d tracked files -- that is the\n"
              "  only statement this run is entitled to make.\n"
              "  Declare the repo's classes in %s to arm the rest."
              % (MANIFEST, root, len(files), MANIFEST), file=sys.stderr)
        return 3

    if not out:
        # The shaped count is printed even when it is zero, and especially when it is zero. A
        # report that says only "clean, N files" cannot distinguish a repo with nothing to find
        # from a shape list that matches nothing -- which is exactly the state this check shipped
        # in until 2026-08-27, when its patterns turned out to match 0 of the 116 files a real run
        # of this skill writes. A reader who sees "0 wear a run shape" against a skill that
        # produces hundreds of files a day now has something to be suspicious of.
        shaped = sorted(f for f in files if shape_of(f))
        print("data_boundary: clean (%d DATA + %d sealed paths absent, %d FIXTUREs "
              "generator-reproducible, %d tracked files scanned, %d of them wear a run shape "
              "and all %d are declared)"
              % (len(m.get("data", [])), len(m.get("data_sealed", [])),
                 len(m.get("fixture", [])), len(files), len(shaped), len(shaped)))
        return 0

    print("data_boundary: %d violation(s) -- this repo is not an uninitialized tool\n" % len(out),
          file=sys.stderr)
    for kind, path, why in out:
        print("  %-16s %-52s %s" % (kind, path, why), file=sys.stderr)
    print("\nA public skill repo ships the TOOL and a SYNTHETIC fixture set. Everything a real run\n"
          "produced -- telemetry, real goldens, calibration, verdicts, config -- belongs in the\n"
          "private companion repo, and the loader resolves it from there.\n"
          "\nRUN-SHAPE means: this tracked file looks like output, and no class claims it. Move it out\n"
          "(the usual answer), or -- if it really is hand-written TOOL material that happens to wear\n"
          "the shape -- add the exact path to \"tool\" in %s with a reason." % MANIFEST,
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
