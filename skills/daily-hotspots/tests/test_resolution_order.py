#!/usr/bin/env python3
"""Two places where a SECOND decision used to silently overrule the first.

Both defects have the same shape: a value is resolved correctly, and then a later step quietly
replaces or re-derives it, so the answer a reader would predict from the documentation is not the
answer the code produces. Neither failed loudly. That is what makes them worth pinning.

  A. sourcehealth.specs_from_config implements the resolution order lib.py documents: a per-source
     `health` block IS the spec, wholesale. default_specs then merged that result UNDER the built-in
     DEFAULT_SPECS with `setdefault`, so for any name that also exists in DEFAULT_SPECS, which is
     every shipped source, the operator's explicit override was thrown away without a word. The
     probe kept reporting on the built-in control while the config said otherwise.

  B. runstore._datadir resolved `tools/datadir.py`, the ONE module allowed to say where real output
     goes, through the TOP LEVEL import namespace (`sys.path.insert` plus a bare `import datadir`),
     while archive.py and roster.py load the same file under a private module name and memoize it.
     A top-level name is shadowable by anything else in the process that is called `datadir`, and an
     unmemoized resolver re-runs module init whenever that shared namespace is cleared. The three
     copies stay separate on purpose (roster.py explains why: each writer proves its own
     destination); it is the MECHANISM that had to agree.

The over-rejection controls carry equal weight here. "Config always wins" would pass A's first test
and be a different bug, so a row WITHOUT a health block must still resolve to the built-in entry,
and a DISABLED source must still be probed.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import archive as AR
import runstore as RS
import sourcehealth as SH


# ===========================================================================
# A. an explicit `health` block is the spec, wholesale, all the way through default_specs
# ===========================================================================

def _specs_by_name(specs):
    return {s["name"]: s for s in specs}


def test_an_explicit_health_block_survives_default_specs(monkeypatch):
    """THE DEFECT. `v2ex` ships in DEFAULT_SPECS, so `setdefault` dropped the operator's override.

    Driven through default_specs(), the real entry point the CLI and run.py call, not through
    specs_from_config, which was already correct on its own."""
    cfg = {"sources": {"v2ex": {"enabled": True, "health": {"kind": "web_search"}}}}
    monkeypatch.setattr(SH, "load_config", lambda: cfg)

    specs, note = SH.default_specs()
    v2ex = _specs_by_name(specs)["v2ex"]

    assert v2ex["kind"] == "web_search", (
        "the config declared a health block and the built-in spec overruled it: %r" % v2ex)
    assert SH.control_for(v2ex)["id"] == SH.CONTROLS["web_search"]["id"]
    assert "config sources merged" in note


def test_a_row_without_a_health_block_still_resolves_to_the_builtin_spec(monkeypatch):
    """OVER-REJECTION CONTROL. Only a DECLARED override may win.

    A plain enabled row carries no probe information at all, so it must come back as the built-in
    entry, control query and all. "The config always wins" would satisfy the test above and quietly
    strip every shipped control down to nothing."""
    cfg = {"sources": {"v2ex": {"enabled": True}}}
    monkeypatch.setattr(SH, "load_config", lambda: cfg)

    v2ex = _specs_by_name(SH.default_specs()[0])["v2ex"]
    builtin = _specs_by_name(SH.DEFAULT_SPECS)["v2ex"]

    assert v2ex == builtin
    assert SH.control_for(v2ex)["id"] == "json_root_list:v2ex"
    assert "v2ex.com" in SH.control_for(v2ex)["query"]["url"]


def test_a_disabled_source_is_still_probed_by_default_specs(monkeypatch):
    """EXISTING BEHAVIOR, kept green. brightdata is disabled for COLLECTION and the reason it is
    disabled is exactly the thing the health probe watches, so it must still carry its built-in
    spec after the merge."""
    import lib
    cfg = lib.DEFAULT_CONFIG
    assert (cfg["sources"]["brightdata"] or {}).get("enabled") is False
    monkeypatch.setattr(SH, "load_config", lambda: cfg)

    specs = SH.default_specs()[0]
    bd = _specs_by_name(specs)["brightdata"]

    assert "brightdata" not in {s["name"] for s in SH.specs_from_config(cfg)}
    assert bd == _specs_by_name(SH.DEFAULT_SPECS)["brightdata"]
    assert SH.control_for(bd)["id"] == SH.CONTROLS["web_search"]["id"]


def test_every_enabled_default_config_row_survives_the_merge(monkeypatch):
    """The merge may not LOSE a source either. Overriding by name is only safe while every enabled
    row still appears exactly once in the result."""
    import lib
    monkeypatch.setattr(SH, "load_config", lambda: lib.DEFAULT_CONFIG)
    specs = SH.default_specs()[0]
    names = [s["name"] for s in specs]

    assert len(names) == len(set(names)), "the merge emitted a duplicate spec: %s" % names
    assert {s["name"] for s in SH.specs_from_config(lib.DEFAULT_CONFIG)} <= set(names)
    assert {s["name"] for s in SH.DEFAULT_SPECS} <= set(names)


# ===========================================================================
# B. runstore resolves tools/datadir.py the way the other two writers do
# ===========================================================================

@pytest.fixture
def _cold_resolvers(monkeypatch):
    """Clear runstore's memo and any top-level `datadir` name, and put both back afterwards.

    Without this the tests below could pass on a memo some earlier test filled, which is a green
    that measured nothing."""
    monkeypatch.setattr(RS, "_datadir_mod", None, raising=False)
    stale = sys.modules.pop("datadir", None)
    try:
        yield
    finally:
        if stale is not None:
            sys.modules["datadir"] = stale
        else:
            sys.modules.pop("datadir", None)


def test_runstore_and_archive_resolve_the_same_datadir_file(_cold_resolvers):
    """Three copies of the walk, one destination. Separate copies are deliberate (roster.py: each
    writer proves its own destination); loading DIFFERENT files would not be."""
    mine = RS._datadir()
    theirs = AR._datadir()

    assert Path(mine.__file__).resolve() == Path(theirs.__file__).resolve()
    assert callable(mine.assert_outside_own_repo)


def test_runstore_resolver_is_memoized_and_does_not_re_run_module_init(_cold_resolvers):
    """Calling it twice must hand back the SAME module object, and must keep doing so when the
    shared top-level namespace is cleared underneath it. A bare `import datadir` leans on
    sys.modules for its memo, so anything that clears that entry silently re-executes module init
    and hands the next caller a different object with different identity for its exception types."""
    first = RS._datadir()
    first._probe_stamp = object()

    sys.modules.pop("datadir", None)   # not this module's memo to lean on
    second = RS._datadir()

    assert second is first, "the resolver re-executed tools/datadir.py instead of memoizing it"
    assert getattr(second, "_probe_stamp", None) is first._probe_stamp
    del first._probe_stamp


def test_runstore_resolver_ignores_a_shadowing_top_level_datadir(tmp_path, monkeypatch,
                                                                 _cold_resolvers):
    """A top-level module name is a shared namespace anything in the process can claim. The module
    that decides where REAL data goes must not be resolvable by name collision: it is loaded from
    the path this repo vendored it at, under a private name, or not at all."""
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "datadir.py").write_text(
        "IS_DECOY = True\n"
        "def resolve_data_dir(skill):\n"
        "    raise AssertionError('the decoy resolver was consulted')\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(decoy_dir))
    decoy = importlib.import_module("datadir")
    assert getattr(decoy, "IS_DECOY", False) is True

    mod = RS._datadir()

    assert getattr(mod, "IS_DECOY", False) is False, (
        "runstore resolved tools/datadir.py through the shadowable top-level import namespace")
    assert callable(mod.assert_outside_own_repo)
    assert Path(mod.__file__).resolve() == Path(AR._datadir().__file__).resolve()
    assert sys.modules.get("datadir") is decoy, (
        "the resolver rebound the top-level `datadir` name, which is the namespace it must not use")
