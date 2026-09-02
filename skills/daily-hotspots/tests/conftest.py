import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Freeze the clock for every test so freshness/age/timestamps are deterministic.
os.environ.setdefault("DAILY_HOTSPOTS_NOW", "2026-06-25T12:00:00Z")


# --------------------------------------------------------------------------- staged archive fixtures
# `guards/tools/datadir.py` refuses ANY archive dir that resolves inside this repo, at the reader seam as
# well as the writer seam, because a caller-supplied `--archive-dir` is how a writer gets its
# destination too. Committed archive fixtures are synthetic and legitimately live in the repo, but a
# test may not hand their in-repo path to the resolver: that is the exact shape the guard exists to
# refuse, and loosening the guard so the tests can pass would delete the control.
#
# So tests stage a COPY outside the worktree and point the resolver at that. The guard stays strict,
# the fixture stays committed and synthetic, and nothing writes into the repo.
import atexit
import shutil
import tempfile

_STAGED: dict[str, Path] = {}


def staged_fixture_archive(name: str) -> Path:
    """Copy tests/fixtures/<name> to a temp dir OUTSIDE this repo and return the copy.

    Hard-fails when the source fixture is missing: a staging helper that silently handed back an
    empty directory would turn "the fixture is gone" into "the engine found no history", which is a
    passing test for the wrong reason.
    """
    if name in _STAGED:
        return _STAGED[name]
    src = Path(__file__).resolve().parent / "fixtures" / name
    if not src.is_dir():
        raise FileNotFoundError(
            "fixture archive %s does not exist; regenerate it with tools/make_fixtures.py" % src)
    dest_root = Path(tempfile.mkdtemp(prefix="dh-fixture-%s-" % name))
    dest = dest_root / name
    shutil.copytree(src, dest)
    atexit.register(shutil.rmtree, str(dest_root), True)
    _STAGED[name] = dest
    return dest


# --------------------------------------------------------------------------- hermetic companion dir
# THE SUITE MUST NOT DEPEND ON THE OPERATOR'S PRIVATE DATA REPO, and until 2026-08-29 it did.
#
# Removing archive.py's phantom `$HOME` fallback was correct: an uninitialized install must refuse to
# invent a home for real output. But 22 tests drive the pipeline without naming an archive dir, so
# they went from "quietly filing into a scattered home path" to raising. On the maintainer's machine
# they kept passing, because DAILY_HOTSPOTS_CONFIG was exported in the shell and the resolver
# happily answered with the operator's REAL companion repo. Green locally, red anywhere else, and
# green for a reason nobody would want: the tests could reach live data.
#
# It surfaced from an unexpected direction. The self-evolve harness profiles a target by running its
# suite in a clean worktree, got exit 1, and froze the target at its weakest signal tier. The suite
# was the thing that was wrong, not the harness. Reproduced directly: a detached worktree at HEAD
# with the variable unset fails 22 of 1080. tests.yml already asserts the variable is ABSENT on the
# runner and says the suite "must be proven" self-contained, so CI was red on this commit too.
#
# Fix: every test runs against a THROWAWAY companion dir. Hermetic, deterministic, and it makes
# touching the real archive impossible rather than merely discouraged. Tests that deliberately
# exercise the UNINITIALIZED state unset the variable themselves and are unaffected.
_HERMETIC_ROOT = Path(tempfile.mkdtemp(prefix="dh-hermetic-"))
atexit.register(shutil.rmtree, str(_HERMETIC_ROOT), True)
(_HERMETIC_ROOT / "archive").mkdir(parents=True, exist_ok=True)

# Set unconditionally, NOT setdefault: inheriting the operator's real path is the defect.
os.environ["DAILY_HOTSPOTS_CONFIG"] = str(_HERMETIC_ROOT)


def hermetic_companion() -> Path:
    """The throwaway companion dir this session runs against."""
    return _HERMETIC_ROOT
