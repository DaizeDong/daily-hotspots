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
