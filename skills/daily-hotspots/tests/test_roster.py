"""roster.py, schema validation, account-pull planner, mutation (deterministic, stdlib only).

Pins the §5.1 schema, the §6 pull planner (enabled tier-1 only, honoring topic_filter), and the
§8 yield-engine mutations (reversible auto-prune, human-gated propose-add). No network, no live
MCP, the seed fixture and small inline rosters are the whole world."""
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
    # Appendix A: 49 live-verified starter handles across ALL SIX tracks (twitterapi get_user_info
    # sweep 2026-07-13). realGeorgeHotz stays FLAGGED-not-seeded (purged); drifted/dead handles were
    # corrected (t3dotgg->theo, leeerob->leerob, aeyakovenko->rajgokal) or dropped (brianchesky stub).
    assert len(R.entries_of(roster)) == 49


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


def test_sample_covers_all_six_tracks():
    # the expansion's whole point: X voices on ALL SIX tracks, not just ai-agents (audit found 5/6
    # blind). saas-niche and consumer-social were EMPTY pre-expansion, assert they are now seeded.
    tracks = {e["track"] for e in R.entries_of(_sample())}
    assert tracks == {"ai-agents", "dev-tools", "saas-niche",
                      "fintech-crypto", "consumer-social", "hardware-iot"}


def test_sample_added_at_is_the_fixed_seed_date_not_wall_clock():
    # TEMPORAL VALIDATION (§9 anti-self-deception / installer determinism): every seed entry's
    # added_at must be the FIXED seed date, NOT now(). conftest freezes the clock to 2026-06-25T12:00Z
    # (see test_new_entry_fills_added_at_from_clock_seam); a now()-stamped seed would carry THAT value
    # and re-running the installer would drift the bytes. Pinning to a constant is what keeps the
    # installer byte-identical (E4) and the fixture a stable acceptance oracle.
    SEED_DATE = "2026-07-13T00:00:00Z"
    FROZEN_NOW = "2026-06-25T12:00:00Z"          # what now_utc() returns under the test clock seam
    assert SEED_DATE != FROZEN_NOW               # the two must be distinguishable for this test to bite
    for e in R.entries_of(_sample()):
        assert e["added_at"] == SEED_DATE, f"{e['handle']} not stamped with the fixed seed date"
        assert e["added_at"] != R.iso(R.now_utc())   # never the wall/frozen clock -> not now()-stamped
        R.parse_ts(e["added_at"])                # and still a well-formed, parseable timestamp


# =================================================================== planner selection
def test_plan_pulls_selects_all_enabled_tier1_from_seed():
    roster = _sample()
    plan = R.plan_pulls(roster, load_config())
    assert len(plan) == 49                                  # every seed entry is enabled tier-1
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


# =================================================================== HARDEN r3: min_faves_rostered cap (§6/§8/§9)
def test_min_faves_rostered_capped_at_keyword_floor():
    # An UNBOUNDED collection-side floor routes AROUND the §9 anti-mass-prune clamp: set it to 1e6 and
    # every rostered pull keeps 0 tweets (numerator 0) while run.py still appends a pulls-log line per
    # handle (denominator accrues), so after prune_after_weeks weeks decide_prune reads the WHOLE
    # roster as dead and --apply disables it. The floor is capped at the keyword-search faves floor
    # (500) it exists to undercut, so it can never blind collection wholesale.
    cfg = {"sources": {"twitterapi": {"min_faves_rostered": 1_000_000}}}
    assert R._min_faves_rostered(cfg) == R.KEYWORD_FAVES_FLOOR
    assert R.plan_pulls({"entries": [_entry("alice")]}, cfg)[0]["min_faves"] == R.KEYWORD_FAVES_FLOOR


def test_min_faves_rostered_negative_floored_to_zero():
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": -5}}}) == 0


def test_min_faves_rostered_in_range_is_honored():
    # a sane low floor is passed through unchanged, the knob still tunes normally UNDER the cap
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": 25}}}) == 25
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": 500}}}) == 500


def test_min_faves_rostered_cap_tracks_lower_pre_viral_threshold_only():
    # a user pre_viral_faves_threshold LOWER than the keyword floor tightens the cap...
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": 999}},
                                  "yield": {"pre_viral_faves_threshold": 200}}) == 200
    # ...but a HIGHER pre_viral threshold can NEVER lift the cap (no routing around via a 2nd knob)
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": 1_000_000}},
                                  "yield": {"pre_viral_faves_threshold": 1_000_000}}) \
        == R.KEYWORD_FAVES_FLOOR


@pytest.mark.parametrize("bad", [True, "oops", None, [1], {}])
def test_min_faves_rostered_garbled_falls_back_to_default(bad):
    assert R._min_faves_rostered({"sources": {"twitterapi": {"min_faves_rostered": bad}}}) \
        == R.DEFAULT_MIN_FAVES_ROSTERED


# =================================================================== HARDEN r3: corrupt != missing (§4)
def test_load_roster_corrupt_file_warns_loud_and_degrades_to_empty(tmp_path, capsys):
    # A PRESENT-but-CORRUPT roster.json must NOT be treated as merely-missing: it still degrades to an
    # empty roster (the keyword lane must keep working, a hard raise would take the whole run down)
    # but emits a LOUD stderr warning naming the corruption, so the run is never MUTE about a nullified
    # roster asset the daily cron's missing verify_config would otherwise never catch (§4).
    p = tmp_path / "roster.json"
    p.write_text("{ this is not valid json ", encoding="utf-8")
    roster = R.load_roster(path=str(p))
    assert roster == {"schema_version": R.ROSTER_SCHEMA_VERSION, "entries": []}
    err = capsys.readouterr().err
    assert "CORRUPT" in err and "roster.json" in err          # loud + names the asset


def test_load_roster_missing_file_is_silent(tmp_path, capsys):
    # the contrast: a MISSING file is a legitimate 'no roster yet' state -> empty roster, NO warning
    roster = R.load_roster(path=str(tmp_path / "nope.json"))
    assert roster == {"schema_version": R.ROSTER_SCHEMA_VERSION, "entries": []}
    assert capsys.readouterr().err == ""                       # silence distinguishes it from corrupt


def test_read_roster_file_distinguishes_corrupt_from_missing(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"entries": []}', encoding="utf-8")
    assert R._read_roster_file(good)[1] is None                # parsed clean -> no error
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert R._read_roster_file(bad)[1] is not None             # corrupt -> a non-None error string
    assert R._read_roster_file(tmp_path / "missing.json")[1] is None   # absent -> no error


def test_load_roster_corrupt_warning_can_be_silenced(tmp_path, capsys):
    # warn=False lets a caller that surfaces the state itself (verify_config schema-gates it) mute it
    p = tmp_path / "roster.json"
    p.write_text("{bad", encoding="utf-8")
    R.load_roster(path=str(p), warn=False)
    assert capsys.readouterr().err == ""
