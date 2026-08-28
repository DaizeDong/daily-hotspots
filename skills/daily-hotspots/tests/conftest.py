import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Freeze the clock for every test so freshness/age/timestamps are deterministic.
os.environ.setdefault("DAILY_HOTSPOTS_NOW", "2026-06-25T12:00:00Z")


# --------------------------------------------------------------------------- staged archive fixtures
# `tools/datadir.py` refuses ANY archive dir that resolves inside this repo, at the reader seam as
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
