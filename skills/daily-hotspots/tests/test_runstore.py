#!/usr/bin/env python3
"""Scratch stays outside every repo, and only the allow-listed slice is ever kept.

The bug being pinned: 1.5 GB of raw captures accumulated inside the private companion repo across 32
run trees, untracked and unignored, because nothing ever said where scratch should go and the
agent's working directory happened to be a checkout. Two properties have to hold forever, and both
are the kind that decay silently, so both get a test that goes red rather than a comment:

  * scratch is REFUSED when it would land inside a git worktree, including via the override env var
  * promotion copies a NAMED, SIZE-CAPPED slice and reports everything it skipped

The second matters as much as the first. A retention policy that quietly drops a file is
indistinguishable from data loss, and one that quietly accepts a new 400 MB filename is how the
archive grows back.
"""
from __future__ import annotations

import json

import pytest

import runstore as RS


def _mkrun(tmp_path, name="daily-2026-08-27", **files):
    d = tmp_path / "scratch" / name
    d.mkdir(parents=True)
    for fn, content in files.items():
        (d / fn).write_text(content, encoding="utf-8")
    return d


def _archive(tmp_path):
    a = tmp_path / "companion" / "archive"
    a.mkdir(parents=True)
    return a


# --------------------------------------------------------------------------- scratch location
def test_scratch_is_refused_inside_a_git_worktree(tmp_path, monkeypatch):
    """THE regression that this module exists to prevent. An operator pointing the override at a
    checkout must be refused, not silently obeyed."""
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("DAILY_HOTSPOTS_RUN_ROOT", str(repo / "runs"))
    with pytest.raises(RS.RunStoreError) as e:
        RS.run_dir("daily-2026-08-27")
    assert "git worktree" in str(e.value)
    assert not (repo / "runs").exists(), "it refused but created the directory anyway"


def test_scratch_is_refused_when_nested_deep_inside_a_worktree(tmp_path, monkeypatch):
    """The check walks ancestors, so burying scratch a few levels down must not evade it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("DAILY_HOTSPOTS_RUN_ROOT", str(repo / "a" / "b" / "c" / "runs"))
    with pytest.raises(RS.RunStoreError):
        RS.run_dir("daily-2026-08-27")


def test_scratch_outside_a_worktree_is_created(tmp_path, monkeypatch):
    """Over-rejection control: a resolver that refuses everything is not a resolver."""
    monkeypatch.setenv("DAILY_HOTSPOTS_RUN_ROOT", str(tmp_path / "cache" / "runs"))
    d = RS.run_dir("daily-2026-08-27")
    assert d.is_dir()
    assert d.name == "daily-2026-08-27"


def test_run_id_must_be_a_safe_directory_name(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_RUN_ROOT", str(tmp_path / "cache" / "runs"))
    for bad in ("", "..", "../escape", "a/b", "x" * 200):
        with pytest.raises(RS.RunStoreError):
            RS.run_dir(bad)


def test_scratch_root_never_falls_back_to_a_bare_home(tmp_path, monkeypatch):
    """A bare $HOME is how the previous generation of this bug scattered real data, so the cache
    fallback must be a nested cache path, never the home directory itself."""
    for v in ("DAILY_HOTSPOTS_RUN_ROOT", "TMPDIR", "TEMP", "TMP", "LOCALAPPDATA", "XDG_CACHE_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(RS.Path, "home", staticmethod(lambda: tmp_path / "home"))
    root = RS._scratch_root()
    assert root != (tmp_path / "home")
    assert RS.SKILL in root.parts


# --------------------------------------------------------------------------- promotion
def test_promote_keeps_the_slice_and_leaves_the_bulk_behind(tmp_path):
    src = _mkrun(tmp_path,
                 **{"candidates.json": '[{"title":"x"}]',
                    "result.json": '{"run_id":"daily-2026-08-27"}',
                    "roster_raw_responses.json": "x" * 5000,
                    "sources.json": "y" * 5000,
                    "fetch_reddit.py": "print(1)",
                    "reddit_log.txt": "log"})
    rep = RS.promote(src, _archive(tmp_path), "daily-2026-08-27")
    kept = {p["name"] for p in rep["promoted"]}
    assert kept == {"candidates.json", "result.json"}
    dest = tmp_path / "companion" / "archive" / "runs" / "2026-08-27"
    assert sorted(p.name for p in dest.iterdir()) == ["candidates.json", "result.json"]
    for gone in ("roster_raw_responses.json", "sources.json", "fetch_reddit.py", "reddit_log.txt"):
        assert not (dest / gone).exists(), f"{gone} was promoted; the allow list is not holding"


def test_promote_reports_an_absent_file_instead_of_passing_silently(tmp_path):
    src = _mkrun(tmp_path, **{"candidates.json": "[]"})
    rep = RS.promote(src, _archive(tmp_path), "daily-2026-08-27")
    reasons = {s["name"]: s["reason"] for s in rep["skipped"]}
    assert reasons.get("result.json") == "absent", "a missing keep-file left no trace in the report"


def test_promote_refuses_an_oversized_file_and_says_so(tmp_path):
    """The cap is what stops the archive growing back into a raw dump. It must report, not drop."""
    big = "x" * (RS.KEEP["result.json"][1] + 10)
    src = _mkrun(tmp_path, **{"candidates.json": "[]", "result.json": big})
    rep = RS.promote(src, _archive(tmp_path), "daily-2026-08-27")
    over = [s for s in rep["skipped"] if s["reason"] == "over_cap"]
    assert len(over) == 1 and over[0]["name"] == "result.json"
    assert not (tmp_path / "companion" / "archive" / "runs" / "2026-08-27" / "result.json").exists()


def test_promote_accepts_the_run_report_under_its_older_names(tmp_path):
    """The run's own report has shipped under several names; the first present wins and lands under
    the canonical one, so the archive has a single shape to read."""
    src = _mkrun(tmp_path, **{"candidates.json": "[]", "dry.json": '{"candidates":12}'})
    rep = RS.promote(src, _archive(tmp_path), "daily-2026-08-27")
    got = {p["name"]: p["from"] for p in rep["promoted"]}
    assert got.get("result.json") == "dry.json"
    dest = tmp_path / "companion" / "archive" / "runs" / "2026-08-27" / "result.json"
    assert json.loads(dest.read_text(encoding="utf-8"))["candidates"] == 12


def test_promote_hard_fails_when_the_scratch_is_missing(tmp_path):
    """Write path: a missing source is a refusal, never an empty success."""
    with pytest.raises(RS.RunStoreError):
        RS.promote(tmp_path / "nope", _archive(tmp_path), "daily-2026-08-27")


def test_promote_needs_a_dated_run_id(tmp_path):
    src = _mkrun(tmp_path, name="whatever", **{"candidates.json": "[]"})
    with pytest.raises(RS.RunStoreError):
        RS.promote(src, _archive(tmp_path), "whatever")


def test_promote_dry_run_writes_nothing(tmp_path):
    src = _mkrun(tmp_path, **{"candidates.json": "[]"})
    rep = RS.promote(src, _archive(tmp_path), "daily-2026-08-27", dry_run=True)
    assert rep["promoted"]
    assert not (tmp_path / "companion" / "archive" / "runs").exists()


# --------------------------------------------------------------------------- retention
def test_prune_removes_only_what_is_past_the_window(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    root.mkdir()
    for name in ("daily-2020-01-01", "daily-2020-01-02"):
        (root / name).mkdir()
    fresh = "daily-" + RS.datetime.now(RS.timezone.utc).date().isoformat()
    (root / fresh).mkdir()
    rep = RS.prune(root, retention_days=14)
    assert set(rep["removed"]) == {"daily-2020-01-01", "daily-2020-01-02"}
    assert rep["kept"] == [fresh]
    assert (root / fresh).is_dir()


def test_prune_never_touches_a_directory_without_a_date(tmp_path):
    """A misconfigured root must not turn retention into a recursive delete of unrelated things."""
    root = tmp_path / "runs"
    (root / "important-not-a-run").mkdir(parents=True)
    rep = RS.prune(root, retention_days=0)
    assert rep["removed"] == []
    assert [s["name"] for s in rep["skipped"]] == ["important-not-a-run"]
    assert (root / "important-not-a-run").is_dir()


def test_prune_refuses_to_run_inside_a_worktree(tmp_path):
    root = tmp_path / "repo" / "runs"
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    root.mkdir(parents=True)
    (root / "daily-2020-01-01").mkdir()
    with pytest.raises(RS.RunStoreError):
        RS.prune(root, retention_days=0)
    assert (root / "daily-2020-01-01").is_dir(), "it raised but deleted anyway"


def test_prune_on_a_missing_root_is_a_clean_no_op_that_says_it_did_not_exist(tmp_path):
    """Reader-ish path: nothing to prune is fine, but 'nothing there' and 'nothing old' must not
    look the same to whoever reads the report."""
    rep = RS.prune(tmp_path / "nope", retention_days=0)
    assert rep["existed"] is False
    assert rep["removed"] == []


def test_two_runs_on_one_date_do_not_collide(tmp_path):
    """Found during the real migration: `daily-2026-08-01` and `daily-2026-08-01-rerun-1214` both
    resolved to one date directory and the second silently overwrote the first. Two runs that
    happened are two runs to keep."""
    a = _mkrun(tmp_path, name="run-a", **{"candidates.json": '[{"t":"scheduled"}]'})
    b = _mkrun(tmp_path, name="run-b", **{"candidates.json": '[{"t":"rerun"}]'})
    arch = _archive(tmp_path)
    RS.promote(a, arch, "daily-2026-08-01")
    RS.promote(b, arch, "daily-2026-08-01-rerun-1214")
    runs = sorted(p.name for p in (arch / "runs").iterdir())
    assert runs == ["2026-08-01", "2026-08-01-rerun-1214"]
    assert '"scheduled"' in (arch / "runs" / "2026-08-01" / "candidates.json").read_text(encoding="utf-8")
    assert '"rerun"' in (arch / "runs" / "2026-08-01-rerun-1214" / "candidates.json").read_text(encoding="utf-8")


def test_promote_refuses_to_overwrite_a_kept_file_with_different_bytes(tmp_path):
    arch = _archive(tmp_path)
    RS.promote(_mkrun(tmp_path, name="one", **{"candidates.json": '["original"]'}),
               arch, "daily-2026-08-01")
    rep = RS.promote(_mkrun(tmp_path, name="two", **{"candidates.json": '["replacement"]'}),
                     arch, "daily-2026-08-01")
    assert [s["reason"] for s in rep["skipped"] if s["name"] == "candidates.json"] == ["would_clobber"]
    kept = (arch / "runs" / "2026-08-01" / "candidates.json").read_text(encoding="utf-8")
    assert kept == '["original"]', "the archived record was replaced"


def test_promote_is_idempotent_for_identical_bytes(tmp_path):
    """Over-rejection control: re-running a migration must not start reporting false clobbers."""
    arch = _archive(tmp_path)
    src = _mkrun(tmp_path, **{"candidates.json": '["same"]'})
    RS.promote(src, arch, "daily-2026-08-27")
    rep = RS.promote(src, arch, "daily-2026-08-27")
    assert [p["name"] for p in rep["promoted"]] == ["candidates.json"]
    assert not [s for s in rep["skipped"] if s["reason"] == "would_clobber"]


def test_scratch_prefers_temp_because_the_agent_sandbox_allows_only_workdir_and_temp(monkeypatch, tmp_path):
    """Not a preference, a constraint. The collector may run under a write sandbox scoped to the
    working directory plus temp, and the working directory has to stay the companion repo so the
    archive is writable at all. Temp is therefore the only permitted home that is not a repo. If
    this ordering regresses to a cache dir, the sandboxed leg silently cannot write its scratch."""
    monkeypatch.delenv("DAILY_HOTSPOTS_RUN_ROOT", raising=False)
    monkeypatch.setenv("TEMP", str(tmp_path / "t"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = RS._scratch_root()
    assert str(root).startswith(str(tmp_path / "t")), f"scratch root ignored temp: {root}"
