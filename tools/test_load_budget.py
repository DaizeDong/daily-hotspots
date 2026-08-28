#!/usr/bin/env python3
"""Regression suite for tools/load_budget.py, the PHILOSOPHY P7 gate.

WHY THIS FILE EXISTS
    load_budget.py was wired into CI (.github/workflows/load-budget.yml) with no tests at all, while
    the two guards it sits beside, dash_guard and pii_guard, each run their own suite as the FIRST
    step of their own workflow. That asymmetry is not cosmetic. Both of those suites exist because
    their guard once printed a clean result over a scan that never happened, and the only thing that
    catches that class of bug is a test that feeds the guard something bad and demands red.

    So the shape of this file is deliberate: every guarantee load_budget makes has a POSITIVE test
    (good input, exit 0) paired with a NEGATIVE CONTROL (bad input, nonzero exit, and the specific
    word the report must say). A test that cannot go red is not a test, and a suite of only-green
    tests over a gate is the same disease the gate was built to cure, one level up.

    Each negative control below is annotated with what breaks it, i.e. what edit to load_budget.py
    would make that specific test pass vacuously. If you change the tool and a NEG test still passes,
    check that it is still reaching the branch it names before believing it.

HOW IT RUNS THE TOOL
    Through subprocess, on a synthetic repo built in tmp_path, never by importing and calling main().
    The thing under test is the EXIT CODE as CI and the hooks observe it, and an in-process call
    cannot catch a wrapper that swallows a status. Fixtures are entirely synthetic prose; nothing in
    here is copied from any real SKILL.md.
"""

import os
import subprocess
import sys

import pytest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load_budget.py")

# 8-word shingles are what the duplication half counts, so fixture prose must be long enough to
# produce plenty of them and, across two different seeds, must share NO 8-word run.
#
# The first version of this generator was one fixed sentence with the line number interpolated into
# it. Every line of every fixture then shared the same eight words, because shingles() drops
# all-digit tokens before shingling, so the only thing distinguishing the lines was deleted before
# the comparison. Result: every "unrelated reference" fixture measured 100% duplication and five
# tests failed for reasons that had nothing to do with the tool. Word-level randomness from a seeded
# RNG over a synthetic vocabulary makes an 8-gram collision between two seeds vanishingly unlikely,
# while keeping the same seed byte-identical so the duplication fixtures stay exact.
_VOCAB = ["lorem%d" % i for i in range(400)]


def _prose(n_lines, seed=0):
    import random
    rng = random.Random(seed)
    return "\n".join(" ".join(rng.choice(_VOCAB) for _ in range(14)) for _ in range(n_lines))


def _mkrepo(root, skill_lines=40, refs=None, layout="skills", skill_text=None, plugin=True):
    """Build a synthetic skill repo.

    layout="skills" -> skills/<name>/SKILL.md   (multi-skill layout)
    layout="root"   -> SKILL.md at the repo root (single-skill layout)
    refs            -> {"filename.md": "body"} written beside SKILL.md
    """
    root = str(root)
    if plugin:
        os.makedirs(os.path.join(root, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as fh:
            fh.write('{"name": "fixture-skill"}\n')
    if layout == "root":
        base = root
    else:
        base = os.path.join(root, "skills", "fixture-skill")
    os.makedirs(base, exist_ok=True)
    body = skill_text if skill_text is not None else _prose(skill_lines)
    with open(os.path.join(base, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(body)
    for name, text in (refs or {}).items():
        p = os.path.join(base, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def _run(root, *args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, TOOL, str(root), *args],
                       capture_output=True, text=True, env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# --------------------------------------------------------------------------------------------
# 1. The ALWAYS-LOADED BUDGET half. This is the half that, until 2026-08-27, could not fail at all:
#    exceeding it printed a note and the row still said "ok". Both directions are pinned here.
# --------------------------------------------------------------------------------------------

def test_always_loaded_under_cap_passes(tmp_path):
    """POSITIVE. A small SKILL.md with a reference that shares nothing is a clean run, exit 0."""
    _mkrepo(tmp_path, skill_lines=40, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path)
    assert rc == 0, out
    assert "[   ok]" in out, out
    assert "BLOCK" not in out, out


def test_NEG_always_loaded_over_cap_blocks(tmp_path):
    """NEGATIVE CONTROL for the always-loaded budget.

    Feeds the gate a SKILL.md far over the hard cap and demands a nonzero exit. This is the exact
    input that used to exit 0 with a soothing note, so if this test ever goes green while asserting
    rc == 0, the half has been un-armed again.

    What would break this test: deleting `over_lines` from the `failed |=` expression in main(), or
    raising ALWAYS_LOADED_MAX above the fixture size. The assertions on the message body are here so
    that a cap raised silently still fails this test rather than passing it.
    """
    _mkrepo(tmp_path, skill_lines=900, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path)
    assert rc == 1, "over-budget SKILL.md must BLOCK (exit 1), got rc=%s\n%s" % (rc, out)
    assert "BLOCK" in out, out
    assert "always-loaded budget exceeded" in out, out
    assert "over by" in out, "the report must say the bound AND the overshoot, not just 'too big'\n" + out


def test_NEG_cap_is_a_real_bound_not_decoration(tmp_path):
    """NEGATIVE CONTROL that the cap is compared, not merely printed.

    A cap that is never read would let ANY --max-lines pass. Here one repo is run twice: once under a
    cap it clears and once under a cap it does not, with nothing else changed. The verdict has to
    flip. This catches the failure mode where a threshold is formatted into the output but never
    reaches an `if`.
    """
    _mkrepo(tmp_path, skill_lines=200, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc_pass, out_pass = _run(tmp_path, "--max-lines", "500")
    rc_fail, out_fail = _run(tmp_path, "--max-lines", "50")
    assert rc_pass == 0, out_pass
    assert rc_fail == 1, "a cap below the file size must block; the cap is not being read\n" + out_fail
    assert "always-loaded budget exceeded" in out_fail, out_fail


def test_warn_band_is_advisory_and_says_so(tmp_path):
    """The 450 warn rung must stay a note, not a block, and must name both rungs.

    Two thresholds that report identically is how an advisory gets read as a verdict (or a verdict as
    an advisory). This pins the ladder: over WARN, under MAX, exit 0, and the text names the block
    threshold so nobody has to guess how much room is left.
    """
    _mkrepo(tmp_path, skill_lines=500, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path, "--max-lines", "600")
    assert rc == 0, "the warn band must not block\n" + out
    assert "[   ok]" in out, out
    assert "block at 600" in out, "the advisory note must state the hard cap it is short of\n" + out


# --------------------------------------------------------------------------------------------
# 2. The DUPLICATION half. It could already fail; these keep it that way and pin the reporting.
# --------------------------------------------------------------------------------------------

def test_NEG_duplicated_prose_blocks(tmp_path):
    """NEGATIVE CONTROL for the duplication half: a paragraph living in SKILL.md and in a reference.

    What would break this test: raising DUP_PCT_MAX, or excluding the reference (see is_template, a
    reference named *template*/*fixture*/*_example* is skipped on purpose, so the fixture is named
    reference/rationale.md and must stay that way).
    """
    shared = _prose(30, seed=100)
    _mkrepo(tmp_path, skill_text=shared + "\n" + _prose(10, seed=500),
            refs={"reference/rationale.md": shared})
    rc, out = _run(tmp_path)
    assert rc == 1, "prose duplicated into a reference must BLOCK\n" + out
    assert "BLOCK" in out, out
    assert "shared with" in out, out
    assert "reference/rationale.md" in out.replace("\\", "/"), out


def test_NEG_dup_threshold_is_read(tmp_path):
    """NEGATIVE CONTROL that --max-dup is compared and not decoration. Same repo, flipped verdict."""
    shared = _prose(30, seed=100)
    _mkrepo(tmp_path, skill_text=shared + "\n" + _prose(10, seed=500),
            refs={"reference/rationale.md": shared})
    rc_fail, out_fail = _run(tmp_path, "--max-dup", "2.0")
    rc_pass, out_pass = _run(tmp_path, "--max-dup", "100.0")
    assert rc_fail == 1, out_fail
    assert rc_pass == 0, "a threshold above the measured value must pass; --max-dup is not read\n" + out_pass


def test_template_named_reference_is_excluded(tmp_path):
    """A template legitimately restates the rule it produces, so it is not duplication.

    Paired with the test above so the exclusion cannot quietly widen: identical content, the only
    difference is the filename.
    """
    shared = _prose(30, seed=100)
    _mkrepo(tmp_path, skill_text=shared + "\n" + _prose(10, seed=500),
            refs={"reference/report_template.md": shared})
    rc, out = _run(tmp_path)
    assert rc == 0, "a *template*.md reference must not count as duplicated prose\n" + out


# --------------------------------------------------------------------------------------------
# 3. "CLEAN" AND "CHECKED NOTHING" MUST BE DIFFERENT OUTPUTS.
#    dup_pct is a ratio and 0.00% has two causes: nothing matched, or nothing was compared.
# --------------------------------------------------------------------------------------------

def test_NEG_zero_references_reports_not_checked_not_clean(tmp_path):
    """NEGATIVE CONTROL for the empty-loop shape.

    A repo with no reference docs runs the comparison loop zero times. That is a legitimate repo
    shape (a single-file skill), so it is not a failure, but the report must not render it as a
    measured 0.00%. This asserts on the wording, which is the only observable difference, and it is
    the assertion that goes red the moment somebody "simplifies" the two cases back into one.
    """
    _mkrepo(tmp_path, skill_lines=40, refs=None)
    rc, out = _run(tmp_path)
    assert rc == 0, out
    assert "NOT CHECKED" in out, "0 refs must not be reported as a clean 0.00%\n" + out
    assert "0.00%" not in out, "a comparison that never ran must not print a ratio\n" + out
    assert "0 reference docs" in out, out


def test_dup_percent_is_printed_when_it_was_actually_measured(tmp_path):
    """Pairing for the test above: with a reference present, the ratio IS printed.

    Without this, the previous test could be satisfied by a tool that never prints a percentage at
    all, which would destroy the distinction from the other side.
    """
    _mkrepo(tmp_path, skill_lines=40, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path)
    assert rc == 0, out
    assert "0.00%" in out, out
    assert "NOT CHECKED" not in out, out


def test_NEG_skill_md_too_short_to_shingle_is_a_block(tmp_path):
    """NEGATIVE CONTROL: an empty or truncated SKILL.md measured NOTHING and must not read as ok.

    Fewer than SHINGLE_N words yields an empty shingle set. The old arithmetic divided by
    max(1, 0), producing a confident 0.00% over a file nothing had examined. Both halves are blind
    here, so the verdict is BLOCK.
    """
    _mkrepo(tmp_path, skill_text="tiny\n")
    rc, out = _run(tmp_path)
    assert rc == 1, "a SKILL.md with no measurable prose must BLOCK\n" + out
    assert "MEASURED NOTHING" in out, out
    assert "0.00%" not in out, "nothing was measured, so no ratio may be printed\n" + out


def test_NEG_completely_empty_skill_md_is_a_block(tmp_path):
    """Same guarantee at the degenerate end: a zero-byte SKILL.md."""
    _mkrepo(tmp_path, skill_text="")
    rc, out = _run(tmp_path)
    assert rc == 1, "a zero-byte SKILL.md must BLOCK\n" + out
    assert "MEASURED NOTHING" in out, out


# --------------------------------------------------------------------------------------------
# 4. NOTHING TO MEASURE AT ALL. Exit 3, the guarantee the docstring was rewritten for.
# --------------------------------------------------------------------------------------------

def test_NEG_skill_repo_with_no_skill_md_exits_3(tmp_path):
    """NEGATIVE CONTROL: a repo that declares a skill but ships no SKILL.md cleared nothing.

    This used to be exit 2 and blessed as "a state, not a failure". Exit 3 is asserted exactly, not
    merely "nonzero", because the retirement of 2 is the point: a caller that special-cased 2 as
    benign has to break loudly.
    """
    os.makedirs(os.path.join(str(tmp_path), ".claude-plugin"))
    with open(os.path.join(str(tmp_path), ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    rc, out = _run(tmp_path)
    assert rc == 3, "no SKILL.md in a skill repo must exit 3, got rc=%s\n%s" % (rc, out)
    assert "FAIL" in out and "measured NOTHING" in out, out
    assert "nothing is cleared" in out, out


def test_NEG_non_skill_directory_still_fails(tmp_path):
    """NEGATIVE CONTROL: a directory that is not a skill repo at all also exits 3.

    ships_a_skill() only changes the WORDING. If it ever starts converting the failure into a pass,
    this goes red. That conversion is the single most tempting "fix" for a noisy gate, so it gets its
    own test rather than sharing one with the case above.
    """
    (tmp_path / "README.md").write_text("not a skill repo\n", encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc == 3, "a non-skill directory measured nothing; that is still not a pass\n" + out
    assert "declares no skill" in out, out


def test_NEG_scan_all_over_an_empty_parent_exits_3(tmp_path):
    """NEGATIVE CONTROL for --scan-all: a parent directory holding no skill repos.

    The fleet-wide loop over an empty list of children is exactly the shape that prints green having
    checked nothing.
    """
    os.makedirs(os.path.join(str(tmp_path), "not-a-repo"))
    rc, out = _run(tmp_path, "--scan-all")
    assert rc == 3, "scan-all that found no repos must fail\n" + out
    assert "measured NOTHING" in out, out


def test_NEG_missing_root_directory_fails(tmp_path):
    """NEGATIVE CONTROL: pointed at a path that does not exist, the tool must not report success.

    A gate handed a bad path is the everyday form of "checked nothing": a renamed directory, a typo
    in a workflow, a checkout that did not happen.
    """
    rc, out = _run(os.path.join(str(tmp_path), "does-not-exist"), "--scan-all")
    assert rc == 3, "a nonexistent root must fail, not pass\n" + out


# --------------------------------------------------------------------------------------------
# 5. LAYOUT DISCOVERY. The original silence was a root-layout SKILL.md the tool could not see.
# --------------------------------------------------------------------------------------------

def test_root_layout_skill_md_is_discovered(tmp_path):
    """A single-skill repo keeps SKILL.md at the root. Not finding it is what started all this."""
    _mkrepo(tmp_path, skill_lines=40, layout="root",
            refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path)
    assert rc == 0, out
    assert "measured NOTHING" not in out, "the root-layout SKILL.md was not discovered\n" + out


def test_NEG_root_layout_is_measured_not_just_found(tmp_path):
    """NEGATIVE CONTROL: discovery without measurement would be the same silence in a new costume.

    An over-budget SKILL.md at the ROOT must block just as it does under skills/. Finding a file and
    then not applying the budget to it is indistinguishable from not finding it.
    """
    _mkrepo(tmp_path, skill_lines=900, layout="root",
            refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path)
    assert rc == 1, "an over-budget root-layout SKILL.md must BLOCK\n" + out
    assert "always-loaded budget exceeded" in out, out


# --------------------------------------------------------------------------------------------
# 6. THE JSON MODE, which other tooling may read. The honesty flags have to be in it too.
# --------------------------------------------------------------------------------------------

def test_json_mode_carries_the_measured_and_dup_checked_flags(tmp_path):
    """A machine reader must be able to tell "clean" from "not compared" without parsing prose."""
    import json
    _mkrepo(tmp_path, skill_lines=40, refs=None)
    rc, out = _run(tmp_path, "--json")
    payload = json.loads(out[out.index("["):out.rindex("]") + 1])
    assert rc == 0, out
    assert payload[0]["measured"] is True, payload
    assert payload[0]["dup_checked"] is False, "0 refs must be dup_checked=False, not a 0.00% pass\n%s" % payload


def test_NEG_json_mode_still_returns_the_failing_exit_code(tmp_path):
    """NEGATIVE CONTROL: --json must not become an escape hatch.

    A reporting flag that suppresses the verdict is how a gate gets neutralised without anyone
    editing the gate. Same over-budget repo as the block test, asserted through --json.
    """
    import json
    _mkrepo(tmp_path, skill_lines=900, refs={"reference/notes.md": _prose(30, seed=9000)})
    rc, out = _run(tmp_path, "--json")
    assert rc == 1, "--json must not turn a failure into a pass\n" + out
    payload = json.loads(out[out.index("["):out.rindex("]") + 1])
    assert payload[0]["always_loaded_lines"] > 600, payload


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
