#!/usr/bin/env python3
"""The suite must not reach the operator's real data, and must not need it to pass.

Until 2026-08-29 it did both. Removing archive.py's phantom $HOME fallback was correct, but 22 tests
drive the pipeline without naming an archive dir, so they went from quietly filing into a scattered
home path to raising. On the maintainer's machine they kept passing because DAILY_HOTSPOTS_CONFIG
was exported in the shell and the resolver answered with the REAL companion repo. Green locally, red
everywhere else, and green for the worst possible reason: the tests could reach live data.

It surfaced sideways. The self-evolve harness profiles a target by running its suite in a clean
worktree, got exit 1, and froze the target at its weakest signal tier, which read as "there is
nothing here worth measuring". The suite was what was wrong. Reproduced directly: a detached
worktree at that commit, variable unset, failed 22 of 1080.

conftest now points every test at a throwaway companion dir. This file guards that, because an
environment-dependent green is the least visible kind of broken and it has now happened once.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import hermetic_companion


def test_the_companion_dir_under_test_is_a_throwaway_not_the_operators():
    """The load-bearing assertion. If this ever points at a real companion repo again, every test
    that writes becomes a test that can touch live data."""
    cfg = os.environ.get("DAILY_HOTSPOTS_CONFIG")
    assert cfg, "DAILY_HOTSPOTS_CONFIG is unset during tests; the pipeline tests will raise"
    here = Path(cfg).resolve()
    assert here == hermetic_companion().resolve(), \
        f"tests are running against {here}, not the throwaway dir; a real archive is reachable"


def test_the_companion_dir_is_outside_every_checkout():
    """Real run output must never resolve inside a repo, and neither may the test double, or the
    data-boundary guard would be exercised against a path it is supposed to refuse."""
    here = hermetic_companion().resolve()
    for node in (here, *here.parents):
        assert not (node / ".git").exists(), f"the test companion dir sits inside a worktree: {here}"


def test_the_companion_dir_is_empty_of_real_history():
    """A throwaway that happened to contain a real ledger would let a test pass on live evidence."""
    arch = hermetic_companion() / "archive"
    ledger = arch / "opportunities.jsonl"
    if ledger.exists():
        assert ledger.stat().st_size == 0, "the test archive already holds opportunity records"


def test_the_operators_real_archive_is_not_reachable_through_the_resolver():
    """End to end: ask the writer seam where output goes and confirm it is the throwaway."""
    import archive as A
    resolved = Path(A.resolve_archive_dir(None)).resolve()
    assert hermetic_companion().resolve() in (resolved, *resolved.parents), \
        f"the archive writer resolves to {resolved}, outside the throwaway dir"


def test_the_frozen_clock_is_in_effect():
    """The other environment dependency in this suite. A drifting clock makes freshness scores and
    therefore card ordering non-reproducible, which is a slow-moving version of the same bug."""
    assert os.environ.get("DAILY_HOTSPOTS_NOW"), "the test clock is not frozen"


@pytest.mark.parametrize("var", ["DAILY_HOTSPOTS_RUN_ROOT", "DAILY_HOTSPOTS_RUN_DIR"])
def test_run_scratch_env_does_not_leak_into_the_suite(var):
    """Scratch resolution is env driven, so an exported value from a real run would make the tests
    read or write a live scratch tree. If one is set it must at least not be inside a checkout."""
    val = os.environ.get(var)
    if not val:
        return
    p = Path(os.path.expanduser(val)).resolve()
    for node in (p, *p.parents):
        assert not (node / ".git").exists(), f"{var} points inside a worktree: {p}"
