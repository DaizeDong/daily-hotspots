"""roster.py path resolution: ONE resolver, no invented home, writer hard-fails.

roster.py was the last writer in this repo still carrying the two defects archive.py had already
shed. It ran its own probe order (``lib.find_config_dir``) instead of the single guarded resolver
``tools/datadir.py``, and when that probe came back empty it answered
``Path.home() / ".daily-hotspots-config" / "roster.json"``, inventing a home for real run output at
write time. ``save_roster`` then called ``mkdir(parents=True)`` on that parent, so an uninitialized
machine did not fail loudly: it conjured a companion config into $HOME and started a real KOL
roster inside it, with no remote, no history and no backup, and "uninitialized" became
indistinguishable from "initialized at the default path".

These tests pin the repaired contract, and the negative controls are the regression guard: they go
RED if anything reintroduces a $HOME fallback, a second probe order, or a write that proceeds when
the destination is unknown.

  WRITE (resolve_roster_path / save_roster)  hard-fails, and creates NOTHING while failing
  READ  (find_roster_path / load_roster)     degrades to an empty roster, and says it is degrading
  EITHER, given an explicit path inside the tool repo, refuses via datadir.assert_outside_own_repo
"""
import ast
import inspect
import json
from pathlib import Path

import pytest

import roster as R

IN_REPO_PATH = Path(__file__).resolve().parent / "fixtures" / "roster-must-never-be-written.json"


def _sample() -> dict:
    return {"schema_version": 1, "entries": [{
        "handle": "example_founder", "track": "ai-infra", "tier": 1, "enabled": True,
        "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}]}


@pytest.fixture
def uninitialized(monkeypatch, tmp_path):
    """A machine with NO companion config anywhere: fresh clone, nothing exported, empty $HOME.

    Every discovery channel datadir has is closed honestly rather than by stubbing the resolver:
    the three env vars are unset, $HOME points at an empty temp dir (so the dotfile and XDG
    candidates resolve to real paths that simply do not exist), and the sibling-repo convention is
    switched off because this checkout DOES have a companion repo beside it on this machine.
    Returns the temp home so a test can assert nothing was created inside it.
    """
    dd = R._datadir()
    home = tmp_path / "home"
    home.mkdir()
    for var in ("DAILY_HOTSPOTS_DATA_DIR", "DAILY_HOTSPOTS_CONFIG", "DAILY_HOTSPOTS_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var in ("HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dd, "_convention_roots", lambda skill: [])
    # The control must actually control: if $HOME did not move, "nothing was created under home"
    # would be asserting about the wrong directory and would pass for the wrong reason.
    assert Path.home() == home, "test harness failed to redirect $HOME; the negative control is inert"
    assert dd.resolve_data_dir("daily-hotspots") is None, "something is still configured"
    return home


# ==================================================== negative control: WRITE with nothing configured

def test_write_seam_raises_when_nothing_is_configured(uninitialized):
    with pytest.raises(R.RosterPathNotInitialized) as e:
        R.resolve_roster_path()
    msg = str(e.value)
    assert "DAILY_HOTSPOTS_CONFIG" in msg, "the refusal must say how to initialize, not just refuse"
    assert list(uninitialized.rglob("*")) == [], "the writer invented a home under $HOME"


def test_save_roster_raises_and_writes_nothing_when_nothing_is_configured(uninitialized):
    with pytest.raises(R.RosterPathNotInitialized):
        R.save_roster(_sample())
    # The old code reached mkdir(parents=True) here and a roster.json landed at a scattered $HOME
    # path. Nothing at all may appear: not the dotfile directory, not the file.
    assert list(uninitialized.rglob("*")) == [], "save_roster created something under $HOME"
    assert not (uninitialized / ".daily-hotspots-config").exists()


# ==================================================== negative control: READ degrades cleanly

def test_read_seam_degrades_to_empty_roster_when_nothing_is_configured(uninitialized, capsys):
    roster = R.load_roster()               # must NOT raise: a fresh clone still runs keyword-only
    assert R.entries_of(roster) == []
    assert roster["schema_version"] == R.ROSTER_SCHEMA_VERSION
    # "empty" and "never looked at anything" are different facts, so they are different outputs.
    err = capsys.readouterr().err
    assert "UNINITIALIZED" in err
    assert "DAILY_HOTSPOTS_CONFIG" in err
    assert list(uninitialized.rglob("*")) == [], "the reader created something under $HOME"


def test_read_seam_uninitialized_notice_can_be_silenced(uninitialized, capsys):
    R.load_roster(warn=False)
    assert capsys.readouterr().err == ""


def test_find_roster_path_reports_uninitialized_as_data(uninitialized):
    # The state is available as a value, not only as a log line, so a caller can branch on it.
    assert R.find_roster_path() is None


# ==================================================== negative control: explicit path inside the tool repo

def test_explicit_in_repo_path_is_refused_on_the_write_seam():
    dd = R._datadir()
    assert dd._own_repo_root() is not None, "not running from a worktree; the guard would be inert"
    with pytest.raises(dd.DataDirInsideOwnRepo):
        R.resolve_roster_path(str(IN_REPO_PATH))


def test_explicit_in_repo_path_is_refused_on_the_read_seam():
    dd = R._datadir()
    with pytest.raises(dd.DataDirInsideOwnRepo):
        R.load_roster(path=str(IN_REPO_PATH))


def test_save_roster_to_an_in_repo_path_refuses_and_writes_nothing():
    dd = R._datadir()
    try:
        with pytest.raises(dd.DataDirInsideOwnRepo):
            R.save_roster(_sample(), path=str(IN_REPO_PATH))
        assert not IN_REPO_PATH.exists(), "a refused write still landed inside the tool repo"
    finally:
        # When this test FAILS it is because the write went through, and the evidence is a real
        # file inside the tool repo. Sweep it up here: a red test may not leave the repo dirty.
        IN_REPO_PATH.unlink(missing_ok=True)


# ==================================================== the $HOME fallback is really gone

def test_no_home_anchored_fallback_survives_in_the_source():
    """Structural, not textual: the docstrings still DESCRIBE the old fallback on purpose.

    Asserted over the AST so the prose explaining why the fallback was removed cannot satisfy the
    check, and so a reintroduced ``Path.home() / ...`` goes red even when spelled through a
    different local name.
    """
    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    home_calls = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("home", "expandvars")]
    assert home_calls == [], "roster.py resolves a $HOME-anchored path again"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "find_config_dir" not in names, \
        "roster.py has a second probe order again; datadir.py is the only resolver allowed to decide"


# ==================================================== positive controls, so the refusals are not always-on

def test_write_seam_resolves_through_datadir_not_a_private_probe(monkeypatch, tmp_path):
    """DAILY_HOTSPOTS_DATA_DIR is a datadir-only channel; lib.find_config_dir never knew it.

    So this pins WHICH resolver is in play, not merely that some path comes back: the old
    find_config_dir probe ignores this variable and answers with the companion repo instead.
    """
    private = tmp_path / "private-companion"
    private.mkdir()
    monkeypatch.setenv("DAILY_HOTSPOTS_DATA_DIR", str(private))
    assert R.resolve_roster_path() == private / "roster.json"
    assert R.find_roster_path() == private / "roster.json"


def test_configured_companion_round_trips(monkeypatch, tmp_path):
    companion = tmp_path / "daily-hotspots-config"
    companion.mkdir()
    monkeypatch.delenv("DAILY_HOTSPOTS_DATA_DIR", raising=False)
    monkeypatch.setenv("DAILY_HOTSPOTS_CONFIG", str(companion))
    written = R.save_roster(_sample())
    assert written == companion / "roster.json"
    assert json.loads(written.read_text(encoding="utf-8"))["entries"][0]["handle"] == "example_founder"
    assert R.entries_of(R.load_roster()) == R.entries_of(_sample())


# ==================================================== the write path cannot be told to skip validation

def test_save_roster_validation_cannot_be_bypassed(tmp_path):
    """``validate=False`` was deleted with the behavior; no caller in either repo ever passed it.

    Pinned as a signature fact plus a behavioral one, so re-adding the escape hatch goes red rather
    than quietly restoring a way to persist a roster that load_roster would then call corrupt.
    """
    assert "validate" not in inspect.signature(R.save_roster).parameters
    ent = {"handle": "dupe", "track": "t", "tier": 1, "enabled": True,
           "added_at": "2026-06-01T00:00:00Z", "provenance": "seed"}
    p = tmp_path / "roster.json"
    with pytest.raises(ValueError):
        R.save_roster({"entries": [dict(ent), dict(ent)]}, path=str(p))
    assert not p.exists()
