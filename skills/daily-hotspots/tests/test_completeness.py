"""Per-date COMPLETENESS of the digest archive, plus the wrapper probe that has to agree with it.

WHY THIS FILE EXISTS
--------------------
Nothing in this repo ever asked "which DAYS are missing". Two separate mechanisms both look like
they answer that and neither does:

  * the external task-health monitor watches the newest-descendant mtime of the archive. That is
    LIVENESS. The moment one good day lands, every older hole stops being reported, forever. On
    2026-08-28 the live archive held 31 digests across a 45 day span and the monitor was green.
  * wrapper.ps1's own artifact probe asked whether archive/pulls-*.jsonl carried today's run_id.
    That ledger is written by `run.py --sources`, which short-circuits (`if a.sources: return
    _run_sources(a)`) BEFORE the candidate read and before process(). The digest is written inside
    process() by digest.write_digest_file, unconditionally, even on an empty day. So a present
    pulls line proves collection BEGAN and proves nothing about the artifact. Measured on
    2026-07-22: 142 pulls lines stamped daily-2026-07-22, no archive/digests/2026/2026-07-22.md,
    the log ends "run end rc=0", and the commit titled "data: daily archive 2026-07-22" carries
    exactly one file, the pulls ledger. The wrapper certified a lost day as legitimate.

So the two halves tested here are the same guarantee seen from two sides: a scanner that names the
holes after the fact, and a probe that refuses to call a day healthy without the artifact that day
was supposed to produce.

WHAT EVERY TEST BELOW IS SHAPED AGAINST
---------------------------------------
"clean" and "did not check anything" must be different outputs. An absent archive, an archive whose
digests directory is missing, and an archive with zero digests in it must NOT be able to print the
same verdict as a genuinely complete range. Each of those is asserted separately, because the
scanner's whole value is that it is the thing that speaks up when nobody else does.

These tests run on ubuntu in CI, where the skipped-test count is a hard failure, so every assertion
about a .ps1 file is TEXTUAL. Nothing here shells out to powershell.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]              # daily-hotspots/
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCANNER = SCRIPTS / "completeness.py"
WRAPPER = SCRIPTS / "wrapper.ps1"
REGISTER = SCRIPTS / "register-task.ps1"

# The three verdicts, and the fact that they are three. Kept here as literals rather than imported
# from the module under test: a test that reads its expectations out of the implementation cannot
# fail when the implementation collapses two of them into one.
RC_COMPLETE = 0
RC_HOLES = 3
RC_CANNOT_CHECK = 2


# --------------------------------------------------------------------------------- helpers
def _mkarchive(root: Path, dates) -> Path:
    """Build a synthetic archive dir holding a digest for each date. Returns the archive dir.

    Synthetic by construction: the body is a fixed marker string, never a copy of a real digest.
    """
    arch = root / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    for d in dates:
        p = arch / "digests" / d[:4]
        p.mkdir(parents=True, exist_ok=True)
        (p / (d + ".md")).write_text("# synthetic digest %s\n\nSYNTHETIC FIXTURE BODY\n" % d,
                                     encoding="utf-8")
    return arch


def _scan(*args):
    """Run the scanner as a real subprocess and return (rc, stdout, stderr).

    A subprocess, not an in-process call, because the EXIT CODE is half of what is being tested and
    an in-process helper would let a wrong code hide behind a right return value.
    """
    r = subprocess.run([sys.executable, str(SCANNER)] + [str(a) for a in args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or "", r.stderr or ""


# --------------------------------------------------------------------------------- the scanner
def test_scanner_exists_and_is_executable_by_this_interpreter():
    """Negative control for every other scanner test in this file.

    If completeness.py were absent, `_scan` would come back with a nonzero rc and an ImportError on
    stderr, and the "holes" and "cannot check" tests below would pass for entirely the wrong reason
    while the "complete" test failed with a confusing message. Assert the subject exists first.
    """
    assert SCANNER.is_file(), "%s does not exist; nothing below is testing anything" % SCANNER
    rc, out, err = _scan("--help")
    assert rc == 0, "completeness.py --help failed rc=%s\nstdout:\n%s\nstderr:\n%s" % (rc, out, err)


def test_hole_is_named_and_exit_is_nonzero(tmp_path):
    """The load-bearing case: a day with no digest is NAMED, and the process exits nonzero.

    Naming it is the point. A scanner that says "14 holes" and not WHICH ones cannot be acted on,
    and a monitor bound to it would report a number that nobody can turn into a backfill.
    """
    arch = _mkarchive(tmp_path, ["2026-07-14", "2026-07-15", "2026-07-17"])
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-17")
    combined = out + err
    assert rc == RC_HOLES, "expected rc=%s for a range with a hole, got %s\n%s" % (
        RC_HOLES, rc, combined)
    assert "2026-07-16" in combined, "the missing date was never named:\n%s" % combined
    for present in ("2026-07-14", "2026-07-15", "2026-07-17"):
        assert not re.search(r"MISSING\s+" + present, combined), \
            "%s has a digest but was reported missing:\n%s" % (present, combined)


def test_complete_archive_exits_zero_and_says_how_many_days_it_checked(tmp_path):
    """A genuinely complete range exits 0, and says how much it looked at.

    The count is asserted because "COMPLETE" over a zero day range is the silent-cap failure this
    whole file exists to make impossible: it reads identically to a real clean bill of health.
    """
    arch = _mkarchive(tmp_path, ["2026-07-14", "2026-07-15", "2026-07-16"])
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-16")
    combined = out + err
    assert rc == RC_COMPLETE, "expected rc=0 for a complete range, got %s\n%s" % (rc, combined)
    assert "3" in combined, "the scanner did not report how many days it checked:\n%s" % combined
    assert "MISSING" not in combined, "a complete range reported a missing date:\n%s" % combined


def test_absent_archive_is_the_third_code_and_is_never_reported_complete(tmp_path):
    """An archive that is not there must not print clean, and must not print "holes" either.

    Both wrong answers are actively harmful in different ways. "Complete" over a missing archive is
    the silence the task-health monitor already gives. "Holes" over a missing archive would name
    every date in the range as missing, which is technically true and operationally useless, and it
    would train the operator to ignore the alert.
    """
    missing = tmp_path / "there-is-no-archive-here"
    rc, out, err = _scan("--archive-dir", missing, "--start", "2026-07-14", "--end", "2026-07-16")
    combined = out + err
    assert rc == RC_CANNOT_CHECK, \
        "expected rc=%s for an absent archive, got %s\n%s" % (RC_CANNOT_CHECK, rc, combined)
    # Scoped to the VERDICT line, not to the substring: the tool's own name contains "complete",
    # so a naive substring check here would go red on correct output and green on nothing at all.
    assert "RESULT: COMPLETE" not in combined.upper(), \
        "an unreadable archive was reported as complete:\n%s" % combined
    assert "CANNOT CHECK" in combined.upper(), \
        "the refusal was not stated in words, only in an exit code:\n%s" % combined


def test_digests_directory_missing_is_cannot_check_not_complete(tmp_path):
    """The archive dir exists but has never held a digests/ tree.

    This is the shape a fresh or half-initialized companion repo has, and it is exactly the input
    that a naive `for f in glob(...)` scanner reports as clean: the glob matches nothing, the loop
    body never runs, and the function returns "no holes found".
    """
    arch = tmp_path / "archive"
    arch.mkdir(parents=True)
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-16")
    combined = out + err
    assert rc == RC_CANNOT_CHECK, \
        "a missing digests/ tree must be 'cannot check', got rc=%s\n%s" % (rc, combined)


def test_digests_path_that_is_a_file_is_cannot_check(tmp_path):
    """An unreadable digests tree (here: a plain file where the directory should be).

    Portable stand-in for a permission failure, which cannot be produced reliably on both Windows
    and the ubuntu CI runner. The requirement it is standing for is the general one: any OSError
    while enumerating the archive is 'could not check', never 'clean'.
    """
    arch = tmp_path / "archive"
    arch.mkdir(parents=True)
    (arch / "digests").write_text("not a directory", encoding="utf-8")
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-16")
    combined = out + err
    assert rc == RC_CANNOT_CHECK, \
        "an unreadable digests path must be 'cannot check', got rc=%s\n%s" % (rc, combined)


def test_empty_digests_tree_without_an_explicit_range_cannot_anchor(tmp_path):
    """Zero digests and no operator-supplied range: there is nothing to derive a range FROM.

    Reporting "complete" here would be the purest form of the defect. Reporting the holes would
    require inventing a start date. The honest answer is that the check did not happen.
    """
    arch = tmp_path / "archive"
    (arch / "digests").mkdir(parents=True)
    rc, out, err = _scan("--archive-dir", arch)
    combined = out + err
    assert rc == RC_CANNOT_CHECK, \
        "an empty archive with no explicit range must be 'cannot check', got rc=%s\n%s" % (
            rc, combined)


def test_zero_byte_digest_counts_as_a_hole(tmp_path):
    """A digest file that exists but holds nothing is not a day that was published.

    write_digest_file is atomic (temp file plus os.replace), so this should not happen through the
    supported path. It is asserted anyway because the probe and the scanner are both existence
    checks by nature, and an existence check is one truncation away from certifying an empty day.
    """
    arch = _mkarchive(tmp_path, ["2026-07-14", "2026-07-16"])
    hole = arch / "digests" / "2026" / "2026-07-15.md"
    hole.write_text("", encoding="utf-8")
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-16")
    combined = out + err
    assert rc == RC_HOLES, "a zero byte digest must count as a hole, got rc=%s\n%s" % (rc, combined)
    assert "2026-07-15" in combined, "the empty digest's date was not named:\n%s" % combined


def test_the_three_verdicts_are_three_distinct_exit_codes(tmp_path):
    """Negative control against the codes being collapsed.

    Every other test here pins one code against a literal. This one pins the RELATION: if a future
    edit makes 'cannot check' return the same number as 'holes' or as 'complete', the individual
    assertions above could all be rewritten to match and this one still could not.
    """
    complete = _mkarchive(tmp_path / "a", ["2026-07-14"])
    holed = _mkarchive(tmp_path / "b", ["2026-07-14"])
    absent = tmp_path / "c" / "nope"
    codes = {
        "complete": _scan("--archive-dir", complete,
                          "--start", "2026-07-14", "--end", "2026-07-14")[0],
        "holes": _scan("--archive-dir", holed,
                       "--start", "2026-07-14", "--end", "2026-07-15")[0],
        "cannot": _scan("--archive-dir", absent,
                        "--start", "2026-07-14", "--end", "2026-07-15")[0],
    }
    assert len(set(codes.values())) == 3, "the three verdicts are not distinguishable: %s" % codes
    assert codes["complete"] == 0, "'complete' must be 0 so a shell && reads it right: %s" % codes


def test_json_output_is_machine_readable_and_names_the_holes(tmp_path):
    """--json is what a monitor binds to, so it carries the same three facts as the text output."""
    arch = _mkarchive(tmp_path, ["2026-07-14", "2026-07-16"])
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-16",
                         "--json")
    assert rc == RC_HOLES, "got rc=%s\n%s\n%s" % (rc, out, err)
    doc = json.loads(out)
    assert doc["missing"] == ["2026-07-15"], doc
    assert doc["days_checked"] == 3, doc
    assert doc["status"] == "holes", doc


def test_report_file_is_written_and_an_unwritable_report_hard_fails(tmp_path):
    """The scanner is bound as its own task-health artifact, so --report is a WRITE path.

    Writers hard-fail. A --report that cannot be written must not let the process exit 0 having
    "checked fine", because the monitor watching that file would then see a stale artifact and a
    green exit code at the same time and believe the green.
    """
    arch = _mkarchive(tmp_path, ["2026-07-14", "2026-07-15"])
    rpt = tmp_path / "out" / "completeness.json"
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-15",
                         "--report", rpt)
    assert rc == RC_COMPLETE, "%s\n%s" % (out, err)
    assert rpt.is_file(), "the --report artifact was not written"
    doc = json.loads(rpt.read_text(encoding="utf-8"))
    assert doc["status"] == "complete" and doc["days_checked"] == 2, doc

    # Now make the report path unwritable by putting a FILE where its parent directory must go.
    bad_parent = tmp_path / "blocker"
    bad_parent.write_text("i am a file", encoding="utf-8")
    rc2, out2, err2 = _scan("--archive-dir", arch, "--start", "2026-07-14", "--end", "2026-07-15",
                            "--report", bad_parent / "completeness.json")
    assert rc2 != RC_COMPLETE, \
        "a report that could not be written still exited 'complete' rc=%s\n%s\n%s" % (
            rc2, out2, err2)


def test_a_bad_range_is_cannot_check(tmp_path):
    """--start after --end enumerates zero days. Zero days checked is not a clean bill of health."""
    arch = _mkarchive(tmp_path, ["2026-07-14"])
    rc, out, err = _scan("--archive-dir", arch, "--start", "2026-07-20", "--end", "2026-07-14")
    assert rc == RC_CANNOT_CHECK, \
        "an inverted range must not print clean, got rc=%s\n%s%s" % (rc, out, err)


# ------------------------------------------------------------------- the wrapper's liveness probe
def _probe_block(src: str) -> str:
    """Slice out the wrapper's artifact-probe function.

    Anchored on a `function ...Pipeline...` declaration and terminated at the next top level
    `function` or `try {`, so the assertions below are scoped to the probe itself and cannot be
    satisfied by the word "digests" appearing in a comment somewhere else in the file.
    """
    lines = src.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^function\s+\S*Pipeline\S*", line):
            start = i
            break
    if start is None:
        return ""
    out = []
    for line in lines[start + 1:]:
        if re.match(r"^(function\s|try\s*\{)", line):
            break
        out.append(line)
    return "\n".join(out)


def test_probe_block_slicer_finds_something():
    """Negative control for the slicer.

    If `_probe_block` returned "" because the function was renamed out of the pattern, every
    assertion below would pass or fail for reasons unrelated to the probe. An empty slice contains
    no forbidden thing either, which is the same 'checked nothing, printed green' shape the scanner
    tests guard against, reproduced inside a test helper.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    block = _probe_block(src)
    assert len(block) > 300, \
        "the probe function could not be located in wrapper.ps1; the slicer no longer matches it"


def test_liveness_probe_reads_the_digest_artifact_not_only_the_pulls_ledger():
    """THE regression guard for 2026-07-22.

    The old probe asked archive/pulls-*.jsonl whether collection had begun. run.py --sources writes
    that ledger and returns before process() ever runs, so it answers a question nobody asked. The
    artifact the day exists to produce is archive/digests/<yyyy>/<date>.md, written unconditionally
    inside process(), even on an empty day. A revert to the pulls-only probe turns this red.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    block = _probe_block(src)
    assert "digests" in block, \
        "the artifact probe does not look at archive/digests/; it is back to proving only that " \
        "collection began (the 2026-07-22 shape: 142 pulls lines, no digest, certified healthy)"
    assert ".md" in block, "the probe never names a digest file, so it cannot be checking for one"
    # the weaker second signal is deliberately KEPT, so three states stay distinguishable
    assert "pulls-" in block, \
        "the pulls ledger probe was removed; without it 'never collected' and 'collected then " \
        "died before producing' collapse into one indistinguishable failure"


def test_wrapper_gives_the_three_pipeline_states_three_distinct_exit_codes():
    """Three states, three codes. Two of them collapsing is the defect, so the RELATION is pinned.

    never collected / collected then died before producing / healthy are three different operator
    actions (the transport never delivered; the run was cut off mid-flight, look at the time limit;
    nothing to do). One shared exit code makes Task Scheduler's Last Run Result useless for telling
    them apart, which is the only place an unattended run reports at all.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    codes = dict(re.findall(r"\$script:(RC_[A-Z_]+)\s*=\s*(\d+)", src))
    for needed in ("RC_NEVER_COLLECTED", "RC_COLLECTED_NO_DIGEST", "RC_DIGEST_REFUSED"):
        assert needed in codes, \
            "wrapper.ps1 defines no $script:%s; found %s" % (needed, codes)
    vals = [codes[k] for k in ("RC_NEVER_COLLECTED", "RC_COLLECTED_NO_DIGEST", "RC_DIGEST_REFUSED")]
    assert len(set(vals)) == 3, "the three failure states share exit codes: %s" % codes
    assert "0" not in vals, "a failure state is using the success code: %s" % codes


def test_a_refused_digest_write_is_not_read_as_healthy():
    """DigestClobberError means the digest for today was NOT (re)written by this run.

    The file on disk is the earlier run's, so a pure existence probe reads healthy. The wrapper has
    to notice the raise and say so, or a same-day rerun that refused to clobber looks identical to
    a run that published.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    assert "DigestClobberError" in src, \
        "the wrapper never looks for DigestClobberError, so a refused digest write reads as healthy"


def test_scheduler_termination_leaves_a_durable_marker():
    """A run the scheduler kills at ExecutionTimeLimit never reaches try/catch/finally.

    TerminateProcess does not unwind PowerShell, so no exit-code line is written and Notify-Abort
    never fires: the run simply stops mid-log and the next morning looks like a quiet day. The only
    thing that survives is something written to disk BEFORE the kill, so an in-flight marker is
    dropped at start and cleared on every path that actually completes. A marker still present at
    the next start is proof the previous run was terminated.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    assert "inflight" in src.lower(), \
        "wrapper.ps1 drops no in-flight marker, so a scheduler-terminated run stays invisible"
    assert "terminated" in src.lower(), \
        "wrapper.ps1 never reports a terminated previous run"


def test_wrapper_detects_an_llmcall_chain_that_excludes_codex():
    """LLMCALL_CHAIN is cc,claude on this machine, which contradicts the wrapper's own comment.

    That comment records why the chain exists: on 2026-07-26 this task died rc=1 on all three
    retries against a claude weekly limit while codex, which carries its own quota pool, sat idle.
    A machine-level variable that drops codex reintroduces exactly that failure, and the wrapper
    cannot fix machine env from where it runs. So it must at least say so, loudly, every run.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    assert "LLMCALL_CHAIN" in src, "the wrapper never reads LLMCALL_CHAIN, so it cannot notice"
    assert re.search(r"LLMCALL_CHAIN[\s\S]{0,1500}?codex", src), \
        "the wrapper reads LLMCALL_CHAIN but never checks it for codex"


def test_llmcall_shim_is_isolated_from_stray_modules_in_temp():
    """The primary transport is loaded from a shim python file, and python puts that file's
    directory on sys.path[0]. Writing it straight into %TEMP% means any stray module sitting in
    %TEMP% shadows a real import, and the preflight (a separate `-c` invocation with a different
    sys.path[0]) cannot reproduce that failure.
    """
    src = WRAPPER.read_text(encoding="utf-8")
    assert "sys.path" in src, \
        "the shim does not touch sys.path, so a stray module next to it silently shadows the import"
    assert '"-c", "import llmcall"' not in src, \
        "the preflight still imports llmcall through a bare -c, which does not exercise the same " \
        "sys.path the real leg will have"
    assert "preflight" in src.lower(), "no preflight left"


# --------------------------------------------------------------------------------- register-task
def _ps_ints(src: str):
    return {k: int(v) for k, v in re.findall(r"^\s*\$(\w+)\s*=\s*(\d+)\s*$", src, re.M)}


def test_registered_time_limit_exceeds_the_wrapper_transport_budget():
    """Re-running register-task.ps1 as written used to CUT the live limit.

    Live task: ExecutionTimeLimit PT2H. The file said `New-TimeSpan -Hours 1`. The wrapper's own
    transport budget is DAILY_HOTSPOTS_AGENT_TIMEOUT (default 2400s) for the primary leg plus a
    fallback leg of its own, so a one hour limit guillotines a run mid-flight, and a killed run is
    the one shape that reports nothing at all.
    """
    src = REGISTER.read_text(encoding="utf-8")
    nums = _ps_ints(src)
    for needed in ("AgentTimeoutSec", "TransportBudgetSec", "ExecutionLimitSec"):
        assert needed in nums, \
            ("register-task.ps1 does not derive $%s; the limit is not tied to the budget and can "
             "drift below it again. found: %s" % (needed, nums))
    assert nums["TransportBudgetSec"] >= 2 * nums["AgentTimeoutSec"], \
        "the transport budget does not account for both legs: %s" % nums
    assert nums["ExecutionLimitSec"] > nums["TransportBudgetSec"], \
        "ExecutionTimeLimit does not exceed the transport budget: %s" % nums
    # and the relation is enforced at run time too, not only by this test
    assert "throw" in src and "ExecutionLimitSec" in src, \
        "register-task.ps1 does not hard-fail when the derived limit stops exceeding the budget"


def test_battery_conditions_are_disabled():
    """DisallowStartIfOnBatteries plus StopIfGoingOnBatteries are the default, and they dropped
    whole days with the machine up: no wrapper log, no skill-side alert, nothing to find.
    """
    src = REGISTER.read_text(encoding="utf-8")
    assert "-AllowStartIfOnBatteries" in src, "battery start condition is still blocking the task"
    assert "-DontStopIfGoingOnBatteries" in src, "the task is still killed when power changes"


def test_register_task_registers_the_completeness_scanner():
    """The scanner is only a mechanism once something runs it on a schedule."""
    src = REGISTER.read_text(encoding="utf-8")
    assert "Completeness" in src, "register-task.ps1 does not register the completeness scanner"
    assert "-CompletenessOnly" in src, \
        "the completeness task does not invoke the wrapper's completeness leg"


def test_wrapper_exposes_the_completeness_leg():
    src = WRAPPER.read_text(encoding="utf-8")
    assert "CompletenessOnly" in src, "wrapper.ps1 has no -CompletenessOnly switch to register"
    assert "completeness.py" in src, "wrapper.ps1 never invokes the scanner"
