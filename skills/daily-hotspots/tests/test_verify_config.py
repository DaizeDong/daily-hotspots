#!/usr/bin/env python3
"""verify_config doctor — roster.json schema validation + dependency-skill reachability (design
sec 4). Deterministic, stdlib only, no network (the MCP probe is exercised with an injected runner,
never a live `claude mcp list`).

verify_config.py lives at <repo>/scripts (not the skill scripts dir), so we add that to sys.path.
"""
import json
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))

import verify_config as vc  # noqa: E402

VALID_ROSTER = {
    "schema_version": 1,
    "entries": [
        {"handle": "karpathy", "track": "ai-agents", "tier": 1, "enabled": True,
         "added_at": "2026-07-13T00:00:00Z", "provenance": "seed"},
    ],
}
INVALID_ROSTER = {
    "schema_version": 1,
    "entries": [
        {"handle": "@bad handle!", "track": "", "tier": 5, "enabled": "yes",
         "added_at": "not-a-date", "provenance": "whatever"},
    ],
}


# --------------------------------------------------------------- dependency-skill reachability

def test_check_dependency_skills_all_present(tmp_path):
    for s in vc.DEPENDENCY_SKILLS:
        (tmp_path / s).mkdir()
    results = vc.check_dependency_skills(str(tmp_path))
    assert {name for name, _, _ in results} == set(vc.DEPENDENCY_SKILLS)
    assert all(ok for _, ok, _ in results)


def test_check_dependency_skills_missing_fails_loud(tmp_path):
    (tmp_path / "market-intel").mkdir()          # only one of the four present
    results = dict((name, ok) for name, ok, _ in vc.check_dependency_skills(str(tmp_path)))
    assert results["market-intel"] is True
    assert results["self-evolve"] is False       # a missing sibling is a FAIL, not a silent pass
    assert results["schedule-reminder"] is False
    assert results["small-cap-deepdive"] is False


def test_skills_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(vc.SKILLS_DIR_ENV, str(tmp_path))
    assert vc.skills_root() == str(tmp_path.resolve()) or vc.skills_root() == str(tmp_path)


# --------------------------------------------------------------- MCP reachability (injected runner)

def test_check_required_mcps_present():
    runner = lambda: "twitterapi: connected\nbrightdata: connected\n"
    assert all(ok for _, ok, _ in vc.check_required_mcps(runner=runner))


def test_check_required_mcps_missing():
    runner = lambda: "some-other-mcp: connected\n"
    got = dict((n, ok) for n, ok, _ in vc.check_required_mcps(runner=runner))
    assert got["twitterapi"] is False and got["brightdata"] is False


def test_check_required_mcps_cli_absent_is_soft_skip():
    def boom():
        raise FileNotFoundError("claude not on PATH")
    # a missing CLI must NOT be a false FAIL (absence of the tool != absence of the server)
    assert all(ok for _, ok, _ in vc.check_required_mcps(runner=boom))


# --------------------------------------------------------------- roster.json schema in the doctor

def _write_config(tmp_path, roster_obj):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "watchlist.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    (cfg / "roster.json").write_text(json.dumps(roster_obj), encoding="utf-8")
    return cfg


def _run_doctor(cfg_dir, skills_dir, monkeypatch, capsys):
    monkeypatch.setenv(vc.SKILLS_DIR_ENV, str(skills_dir))
    monkeypatch.setattr(sys, "argv", ["verify_config.py", "--config-dir", str(cfg_dir)])
    rc = vc.main()
    return rc, capsys.readouterr().out


def test_doctor_accepts_valid_roster(tmp_path, monkeypatch, capsys):
    cfg = _write_config(tmp_path, VALID_ROSTER)
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    _, out = _run_doctor(cfg, skills, monkeypatch, capsys)
    assert "[PASS] roster.json schema valid (spec 5.1)" in out
    assert "[PASS] dependency skill reachable: market-intel" in out


def test_doctor_flags_invalid_roster(tmp_path, monkeypatch, capsys):
    cfg = _write_config(tmp_path, INVALID_ROSTER)
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    rc, out = _run_doctor(cfg, skills, monkeypatch, capsys)
    assert "[FAIL] roster.json schema valid (spec 5.1)" in out
    assert rc == 1                               # a malformed roster makes the doctor NOT READY


def test_doctor_flags_missing_dependency_skill(tmp_path, monkeypatch, capsys):
    cfg = _write_config(tmp_path, VALID_ROSTER)
    skills = tmp_path / "skills"
    (skills / "market-intel").mkdir(parents=True)   # only one sibling present
    rc, out = _run_doctor(cfg, skills, monkeypatch, capsys)
    assert "[FAIL] dependency skill reachable: self-evolve" in out
    assert rc == 1


# --------------------------------------------------------------- HARDEN r2: MCP §4 never-silent-degrade

def test_check_required_mcps_present_detail_is_reachable():
    # a PRESENT mcp must carry an accurate detail ("reachable"), not the stale "not present" string —
    # so surfacing the detail on a PASS line (below) is informative, not misleading.
    runner = lambda: "twitterapi: connected\nbrightdata: connected\n"
    got = dict((n, d) for n, ok, d in vc.check_required_mcps(runner=runner))
    assert got["twitterapi"] == "reachable" and got["brightdata"] == "reachable"


def test_doctor_default_run_surfaces_mcp_advisory(tmp_path, monkeypatch, capsys):
    # §4 never-silently-degrade: even without --check-mcp the doctor must NOT be MUTE about the
    # source-wiring MCPs — it names them + how to verify, so a bare READY can't imply an MCP
    # reachability it never checked (the exact silent-degrade this design was written to prevent).
    cfg = _write_config(tmp_path, VALID_ROSTER)
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    _, out = _run_doctor(cfg, skills, monkeypatch, capsys)
    assert "MCP reachability NOT verified" in out
    for m in vc.REQUIRED_MCPS:
        assert m in out                                  # each source-wiring MCP is named
    assert "--check-mcp" in out                          # ...and how to actually verify it


# --------------------------------------------------------------- HARDEN r3: will-be-clamped surfacing (§6/§8/§9)

def test_validate_yield_block_flags_window_days_below_guard_floor():
    ok, errs = vc.validate_yield_block({"window_days": 7})
    assert not ok
    assert any("window_days" in e and "guard floor" in e for e in errs)


def test_validate_yield_block_accepts_window_days_at_or_above_floor():
    assert vc.validate_yield_block({"window_days": 30})[0]      # the shipped default
    assert vc.validate_yield_block({"window_days": 60})[0]      # a larger (safe-direction) window


def test_validate_yield_block_window_floor_follows_prune_after_weeks():
    # a larger prune span raises the floor -> window_days that cleared 30 can still be flagged
    ok, errs = vc.validate_yield_block({"window_days": 30, "prune_after_weeks": 6})   # span 42 > 30
    assert not ok and any("window_days" in e for e in errs)


def test_validate_sources_block_flags_unbounded_min_faves_rostered():
    ok, errs = vc.validate_sources_block({"twitterapi": {"min_faves_rostered": 1_000_000}})
    assert not ok
    assert any("min_faves_rostered" in e and "clamp" in e.lower() for e in errs)


def test_validate_sources_block_accepts_sane_or_absent_floor():
    assert vc.validate_sources_block({"twitterapi": {"min_faves_rostered": 25}})[0]
    assert vc.validate_sources_block({"twitterapi": {}})[0]     # absent knob -> nothing to flag
    assert vc.validate_sources_block(None)[0]                   # no sources block -> nothing to flag


def test_validate_sources_block_rejects_nonnumeric_floor():
    ok, errs = vc.validate_sources_block({"twitterapi": {"min_faves_rostered": "lots"}})
    assert not ok and any("number" in e for e in errs)


def test_doctor_flags_unbounded_min_faves_rostered(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "watchlist.json").write_text(json.dumps(
        {"schema_version": 1, "sources": {"twitterapi": {"min_faves_rostered": 1_000_000}}}),
        encoding="utf-8")
    (cfg / "roster.json").write_text(json.dumps(VALID_ROSTER), encoding="utf-8")
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    rc, out = _run_doctor(cfg, skills, monkeypatch, capsys)
    assert "[FAIL] min_faves_rostered within anti-mass-prune cap" in out
    assert rc == 1                                              # a routing-around knob makes it NOT READY


def test_doctor_check_mcp_soft_skip_is_visible_not_a_silent_pass(tmp_path, monkeypatch, capsys):
    # Under --check-mcp, a soft-SKIP (claude CLI absent -> ok=True; tool-absence != server-absence) is
    # still a PASS, but it must SURFACE its skip reason so it can never masquerade as a verified
    # reachable PASS (§4 no silent degrade).
    cfg = _write_config(tmp_path, VALID_ROSTER)
    skills = tmp_path / "skills"
    for s in vc.DEPENDENCY_SKILLS:
        (skills / s).mkdir(parents=True)
    monkeypatch.setattr(vc, "check_required_mcps", lambda *a, **k: [
        (n, True, "claude mcp list unavailable (FileNotFoundError) - skipped")
        for n in vc.REQUIRED_MCPS])
    monkeypatch.setenv(vc.SKILLS_DIR_ENV, str(skills))
    monkeypatch.setattr(sys, "argv",
                        ["verify_config.py", "--config-dir", str(cfg), "--check-mcp"])
    vc.main()
    out = capsys.readouterr().out
    assert "[PASS] MCP reachable: twitterapi" in out     # a soft-skip is a PASS (server may be up)...
    assert "skipped" in out                              # ...but VISIBLY marked skipped, not silent
    assert "MCP reachability NOT verified" not in out    # the default-run advisory is suppressed here
