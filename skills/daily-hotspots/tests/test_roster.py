"""roster.py — schema validation, account-pull planner, mutation (deterministic, stdlib only).

Pins the §5.1 schema, the §6 pull planner (enabled tier-1 only, honoring topic_filter), and the
§8 yield-engine mutations (reversible auto-prune, human-gated propose-add). No network, no live
MCP — the seed fixture and small inline rosters are the whole world."""
import copy
import json
from pathlib import Path

import pytest

import roster as R
from lib import load_config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "roster.sample.json"


def _sample() -> dict:
    return R.normalize_roster(json.loads(SAMPLE.read_text(encoding="utf-8")))


def _entry(handle="alice", track="ai-agents", tier=1, enabled=True, **extra) -> dict:
    e = {"handle": handle, "track": track, "tier": tier, "enabled": enabled,
         "added_at": "2026-07-13T00:00:00Z", "provenance": "seed"}
    e.update(extra)
    return e


# =================================================================== the seed fixture is valid
def test_sample_fixture_exists_and_validates():
    roster = _sample()
    ok, errs = R.validate_roster(roster)
    assert ok, f"seed roster must validate clean, got: {errs}"
    # Appendix A: 15 verified-live starter handles (realGeorgeHotz is FLAGGED, not seeded).
    assert len(R.entries_of(roster)) == 15


def test_sample_entries_are_all_well_shaped():
    for e in R.entries_of(_sample()):
        assert R.validate_entry(e) == []
        assert e["tier"] in R.VALID_TIERS
        assert e["provenance"] in R.VALID_PROVENANCE
        assert e["enabled"] is True
        assert R._HANDLE_RE.match(R.normalize_handle(e["handle"]))


def test_sample_has_levelsio_topic_filter():
    e = R.find_entry(_sample(), "levelsio")
    assert e is not None
    assert e["topic_filter"] == "(AI OR coding OR startup OR ship)"


def test_sample_uses_marclou_not_marc_louvion():
    roster = _sample()
    assert R.find_entry(roster, "marclou") is not None
    assert R.find_entry(roster, "marc_louvion") is None  # 404 per Appendix A


# =================================================================== planner selection
def test_plan_pulls_selects_all_enabled_tier1_from_seed():
    roster = _sample()
    plan = R.plan_pulls(roster, load_config())
    assert len(plan) == 15                                  # every seed entry is enabled tier-1
    handles = [t["handle"] for t in plan]
    assert handles == [R.normalize_handle(e["handle"]) for e in R.entries_of(roster)]  # order kept
    for task in plan:
        assert set(task) == {"handle", "track", "tier", "topic_filter",
                             "min_faves", "include_replies"}
        assert task["tier"] == 1
        assert task["include_replies"] is False             # §6: includeReplies=false


def test_plan_pulls_carries_topic_filter_and_none_default():
    plan = {t["handle"]: t for t in R.plan_pulls(_sample(), load_config())}
    assert plan["levelsio"]["topic_filter"] == "(AI OR coding OR startup OR ship)"
    assert plan["karpathy"]["topic_filter"] is None          # no filter -> explicit None


def test_plan_pulls_excludes_disabled():
    roster = {"entries": [
        _entry("alice", enabled=True),
        _entry("bob", enabled=False),                        # pruned / disabled -> not pulled
        _entry("carol", enabled=True),
    ]}
    handles = [t["handle"] for t in R.plan_pulls(roster, load_config())]
    assert handles == ["alice", "carol"]


def test_plan_pulls_tier_filtering():
    roster = {"entries": [
        _entry("t1a", tier=1),
        _entry("t2a", tier=2),
        _entry("t1b", tier=1),
    ]}
    assert [t["handle"] for t in R.plan_pulls(roster, load_config(), tier=1)] == ["t1a", "t1b"]
    assert [t["handle"] for t in R.plan_pulls(roster, load_config(), tier=2)] == ["t2a"]


def test_select_handles_is_pure_and_order_stable():
    roster = _sample()
    a = R.select_handles(roster)
    b = R.select_handles(roster)
    assert [e["handle"] for e in a] == [e["handle"] for e in b]
    # selecting must not mutate the roster
    assert R.entries_of(roster) == R.entries_of(_sample())


def test_plan_pulls_min_faves_from_config():
    roster = {"entries": [_entry("alice")]}
    default_plan = R.plan_pulls(roster, {})
    assert default_plan[0]["min_faves"] == R.DEFAULT_MIN_FAVES_ROSTERED
    cfg = {"sources": {"twitterapi": {"min_faves_rostered": 25}}}
    assert R.plan_pulls(roster, cfg)[0]["min_faves"] == 25


def test_plan_pulls_normalizes_at_prefixed_handle():
    roster = {"entries": [_entry("@alice")]}
    assert R.plan_pulls(roster, load_config())[0]["handle"] == "alice"


# =================================================================== schema validation
def test_missing_required_key_is_error():
    e = _entry()
    del e["provenance"]
    errs = R.validate_entry(e)
    assert any("provenance" in x for x in errs)


@pytest.mark.parametrize("tier", [0, 3, "1", 1.0, True])
def test_bad_tier_rejected(tier):
    errs = R.validate_entry(_entry(tier=tier))
    assert any("tier" in x for x in errs), f"tier={tier!r} should be rejected"


def test_good_tiers_accepted():
    assert R.validate_entry(_entry(tier=1)) == []
    assert R.validate_entry(_entry(tier=2)) == []


@pytest.mark.parametrize("prov", ["Seed", "auto", "human", "", None])
def test_bad_provenance_rejected(prov):
    assert any("provenance" in x for x in R.validate_entry(_entry(provenance=prov)))


@pytest.mark.parametrize("enabled", [1, 0, "true", None])
def test_non_bool_enabled_rejected(enabled):
    assert any("enabled" in x for x in R.validate_entry(_entry(enabled=enabled)))


@pytest.mark.parametrize("handle", ["@alice", "a", "Dr_Jim_Fan"])
def test_valid_handles_accepted(handle):
    assert R.validate_entry(_entry(handle=handle)) == []


@pytest.mark.parametrize("handle", ["has space", "toolonghandle_exceeds15", "bad-dash",
                                    "emoji😀", "", "with.dot"])
def test_invalid_handles_rejected(handle):
    assert any("handle" in x for x in R.validate_entry(_entry(handle=handle)))


def test_unparseable_added_at_rejected():
    assert any("added_at" in x for x in R.validate_entry(_entry(added_at="not-a-date")))


def test_empty_topic_filter_rejected_but_absent_ok():
    assert any("topic_filter" in x for x in R.validate_entry(_entry(topic_filter="  ")))
    assert R.validate_entry(_entry(topic_filter="(AI OR ship)")) == []
    assert R.validate_entry(_entry()) == []                  # absent optional is fine


def test_duplicate_handle_is_roster_level_error():
    roster = {"entries": [_entry("dupe"), _entry("Dupe")]}   # case-insensitive collision
    ok, errs = R.validate_roster(roster)
    assert not ok
    assert any("duplicate" in x.lower() for x in errs)


def test_bare_list_roster_accepted():
    roster = [_entry("alice"), _entry("bob")]
    ok, errs = R.validate_roster(roster)
    assert ok, errs
    assert [t["handle"] for t in R.plan_pulls(roster, load_config())] == ["alice", "bob"]


def test_non_container_roster_is_error():
    ok, errs = R.validate_roster("nope")
    assert not ok and errs


def test_bad_schema_version_rejected():
    ok, errs = R.validate_roster({"schema_version": "1", "entries": []})
    assert not ok
    assert any("schema_version" in x for x in errs)


# =================================================================== mutation (yield engine)
def test_set_enabled_is_reversible_prune_not_delete():
    roster = _sample()
    n_before = len(R.entries_of(roster))
    e = R.set_enabled(roster, "balajis", False)             # AUTO-PRUNE
    assert e is not None and e["enabled"] is False
    assert len(R.entries_of(roster)) == n_before            # NEVER a delete
    assert R.find_entry(roster, "balajis")["enabled"] is False
    # a pruned handle drops out of the plan...
    assert "balajis" not in [t["handle"] for t in R.plan_pulls(roster, load_config())]
    # ...and can be un-pruned (reversible)
    R.set_enabled(roster, "balajis", True)
    assert R.find_entry(roster, "balajis")["enabled"] is True


def test_set_enabled_missing_handle_is_noop():
    roster = _sample()
    assert R.set_enabled(roster, "nobody_here", False) is None


def test_find_entry_case_insensitive():
    roster = _sample()
    assert R.find_entry(roster, "KARPATHY") is R.find_entry(roster, "karpathy")


def test_upsert_adds_new_approved_handle():
    roster = {"entries": [_entry("alice")]}
    e = R.new_entry("newbie", track="dev-tools", provenance="approved",
                    notes="promoted from review queue")
    R.upsert_entry(roster, e)
    stored = R.find_entry(roster, "newbie")
    assert stored is not None and stored["provenance"] == "approved"
    assert len(R.entries_of(roster)) == 2
    assert R.validate_roster(roster)[0]


def test_upsert_updates_existing_in_place():
    roster = {"entries": [_entry("alice", track="ai-agents"), _entry("bob")]}
    updated = R.new_entry("alice", track="dev-tools", topic_filter="(AI OR ship)",
                          provenance="approved")
    R.upsert_entry(roster, updated)
    assert len(R.entries_of(roster)) == 2                    # no new row
    a = R.find_entry(roster, "alice")
    assert a["track"] == "dev-tools" and a["topic_filter"] == "(AI OR ship)"
    assert R.entries_of(roster)[0]["handle"] == "alice"      # position preserved


def test_upsert_rejects_invalid_entry():
    roster = {"entries": []}
    with pytest.raises(ValueError):
        R.upsert_entry(roster, {"handle": "x", "tier": 9})   # bad tier + missing keys


def test_new_entry_fills_added_at_from_clock_seam():
    e = R.new_entry("alice", track="ai-agents")              # conftest freezes the clock
    assert e["added_at"] == "2026-06-25T12:00:00Z"
    assert R.validate_entry(e) == []


# =================================================================== I/O at the edges
def test_load_roster_missing_file_returns_empty(tmp_path):
    roster = R.load_roster(path=str(tmp_path / "does-not-exist.json"))
    assert roster == {"schema_version": R.ROSTER_SCHEMA_VERSION, "entries": []}


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "roster.json"
    original = _sample()
    R.save_roster(original, path=str(p))
    reloaded = R.load_roster(path=str(p))
    assert R.entries_of(reloaded) == R.entries_of(original)
    assert R.validate_roster(reloaded)[0]


def test_save_refuses_invalid_roster(tmp_path):
    p = tmp_path / "roster.json"
    bad = {"entries": [_entry("dupe"), _entry("dupe")]}      # duplicate handle
    with pytest.raises(ValueError):
        R.save_roster(bad, path=str(p))
    assert not p.exists()                                    # nothing written on refusal


def test_save_load_does_not_mutate_real_config(tmp_path):
    # guardrail: writing only ever touches the explicit path we pass, never the live companion
    before = copy.deepcopy(_sample())
    R.save_roster(before, path=str(tmp_path / "r.json"))
    assert before == _sample()
