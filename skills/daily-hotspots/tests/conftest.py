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
import subprocess
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
for _selector in ('DAILY_HOTSPOTS_DATA_DIR', 'DAILY_HOTSPOTS_CONFIG_DIR'):
    os.environ.pop(_selector, None)


import pytest


@pytest.fixture(autouse=True)
def synthetic_private_metadata(monkeypatch):
    """Synthetic business tests receive proof snapshots; native tests retain real guard policy."""
    from types import SimpleNamespace
    import private_storage
    temporary = Path(tempfile.gettempdir()).resolve()

    def repository(existing):
        if not existing.is_relative_to(temporary):
            raise AssertionError('test attempted non-synthetic repository discovery')
        return temporary

    def proof(selected):
        if not Path(selected).is_relative_to(temporary):
            raise AssertionError('test attempted non-synthetic repository metadata access')
        return SimpleNamespace(root=str(selected), repositories=('example/synthetic-hotspots-config',),
                               signature='synthetic-proof')

    monkeypatch.setattr(private_storage, '_repository_root', repository)
    monkeypatch.setattr(private_storage, '_prove_repository', proof)
    monkeypatch.setattr(private_storage, '_shared_boundary', lambda: SimpleNamespace(
        prove_private_companion=proof, GitError=RuntimeError,
        read_private_companion_git=lambda snapshot, *arguments: SimpleNamespace(
            returncode=1 if arguments[0] == 'check-ignore' else 0, stdout='synthetic-head')))


def hermetic_companion() -> Path:
    """The throwaway companion dir this session runs against."""
    return _HERMETIC_ROOT


def _fixture_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location('complete_test_fixtures', SCRIPTS.parents[2]/'tools/make_fixtures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_scenario():
    return _fixture_generator().candidate_contract_scenario()


def generated_candidate(**changes):
    """Explicit complete input for tests whose subject is downstream of schema validation."""
    scenario = _candidate_scenario()
    candidate = scenario['candidate']
    candidate['pain_evidence'] = scenario['pain_evidence']
    candidate.update(changes)
    return candidate


def generated_offtopic_summary():
    return _candidate_scenario()['offtopic_summary']



def synthetic_production_delivery(monkeypatch):
    """Enable production state transitions while replacing only the delivery effect."""
    import push_card
    monkeypatch.delenv('DAILY_HOTSPOTS_DRYRUN', raising=False)
    monkeypatch.setattr(push_card, 'deliver', lambda text, dry_run=False: (True, 'synthetic acknowledgement'))


def generated_singleton(field, payload):
    return _fixture_generator().source10_singleton(field, payload)


def generated_history_row(**changes):
    return _fixture_generator().source10_history_row(**changes)


def generated_pull(handle, stamp, kept=0):
    return _fixture_generator().source10_pull(handle, stamp, kept)


@pytest.fixture
def synthetic_cli_companion(tmp_path, monkeypatch):
    """Run original child CLIs with real local Git and generated visibility receipts."""
    import json
    sample = _fixture_generator().private_storage_path_scenario()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'remote', 'add', 'origin', sample['origin']], check=True)
    _fixture_generator().synthetic_repository_history(tmp_path)
    home = tmp_path / 'proof-home'
    (home / '.pii-guard').mkdir(parents=True)
    for key in list(os.environ):
        if key.upper().startswith('GIT_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('SYNTHETIC_CLI_SLUG', sample['slug'])
    monkeypatch.setenv('SYNTHETIC_CLI_IMPORTS', str(SCRIPTS))
    monkeypatch.setenv('SYNTHETIC_CLI_VISIBILITY', 'true')
    launch = r"""
import json, os, runpy, sys
from datetime import datetime, timezone
from pathlib import Path
script = sys.argv.pop(1)
sys.path.insert(0, os.environ['SYNTHETIC_CLI_IMPORTS'])
state = {'true': 'PRIVATE', 'false': 'PUBLIC'}.get(os.environ['SYNTHETIC_CLI_VISIBILITY'], 'UNKNOWN')
receipt = {'_refreshed': datetime.now(timezone.utc).isoformat(), os.environ['SYNTHETIC_CLI_SLUG']: state}
(Path.home() / '.pii-guard/visibility.json').write_text(json.dumps(receipt), encoding='utf-8')
if Path(script).name == 'reminder.py':
    # The installed ledger additionally verifies provider visibility live. Replace
    # only that network response; real Git discovery, policy and SQLite remain active.
    sys.path.insert(0, str(Path(script).parent))
    import private_data
    def synthetic_visibility(argv):
        expected = ['gh', 'repo', 'view', os.environ['SYNTHETIC_CLI_SLUG'], '--json', 'nameWithOwner,visibility']
        if (len(argv) != len(expected) or argv[:3] != expected[:3] or argv[4:] != expected[4:]
                or argv[3].casefold() != expected[3].casefold()):
            raise AssertionError('unexpected provider metadata request')
        return json.dumps({'nameWithOwner': os.environ['SYNTHETIC_CLI_SLUG'], 'visibility': state})
    private_data._query = synthetic_visibility
runpy.run_path(script, run_name='__main__')
"""
    return lambda script: [sys.executable, '-c', launch, str(script)]
