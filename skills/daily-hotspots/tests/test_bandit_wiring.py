"""R6 follow-through: the Thompson bandit must actually be WIRED INTO run.py (batch-6 deferred the
orchestration seam). HEAD ships bandit.py as pure functions but run.py scores with the STATIC track
weight and never closes the reward loop. These pin the wiring capability:

  * scoring uses an explore-adjusted (bandit) track weight, bounded, deterministic, opt-in;
  * the reward loop closes, each run emits the next per-track arm learned from realized outcomes;
  * default (no arms) is byte-identical to today; the bandit can never break score bounds.

Capability assertions (not a specific multiplier table). These began under xfail(strict=False);
the wiring landed, they went XPASS, and the markers were removed, so every case below is a hard
regression guard now. The R6 REACHABILITY block further down pins the CLI seam that made the
loop reachable at all.
"""
import run as runner
from lib import load_config

CFG = load_config()
# Headroom landed (self-evolve gate ACCEPT e=129.27, +12, 0 regressed): these are now permanent
# regression guards on the bandit->run.py wiring.


def _cand(title="MCP agent framework launch", track="ai-agents", timing=95, sources=None):
    sources = sources or ["hackernews", "product-hunt"]
    return {
        "title": title, "summary": "open source llm agent tooling", "track": track,
        "entities": (title + " open source llm agent").lower().split(),
        "evidence": [{"source": s, "origin": s + ".com", "url": "http://" + s + "/x",
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in sources],
        "score_breakdown": {"track_fit": 85, "timing": timing, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


def _hi_arm():   # well-performing track: theta ~0.98 deterministically
    return {"ai-agents": {"alpha": 50.0, "beta": 1.0, "n": 51}}


def _lo_arm():   # cold / under-performing track: theta ~0.02
    return {"ai-agents": {"alpha": 1.0, "beta": 50.0, "n": 51}}


# 1, capability surface exists
def test_capability_surface_exists():
    assert hasattr(runner, "effective_track_weight")
    res = runner.process([_cand()], CFG, ledger=None, dry_run=True,
                         bandit_arms=_hi_arm(), bandit_seed=7)
    assert "bandit_arms_next" in res and isinstance(res["bandit_arms_next"], dict)


# 2, default (no arms) is byte-identical to the static-weight score
def test_no_arms_is_byte_identical_to_static():
    static = runner.build_card(_cand(), CFG, "r")
    none_arms = runner.build_card(_cand(), CFG, "r", arms=None, seed=0)
    assert none_arms["final_score"] == static["final_score"]
    assert none_arms["raw_score"] == static["raw_score"]


# 3, a well-performing arm lifts its track's score above the static baseline
def test_high_mean_arm_lifts_score():
    static = runner.build_card(_cand(), CFG, "r")["final_score"]
    hi = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=7)["final_score"]
    assert hi > static


# 4, an under-performing arm dampens its track's score below the static baseline
def test_low_mean_arm_dampens_score():
    static = runner.build_card(_cand(), CFG, "r")["final_score"]
    lo = runner.build_card(_cand(), CFG, "r", arms=_lo_arm(), seed=7)["final_score"]
    assert lo < static


# 5, the explore-adjusted weight is BOUNDED to [0.5*static, 1.5*static] for any arm/seed
def test_effective_weight_bounded():
    static = runner.effective_track_weight("ai-agents", CFG)
    for seed in range(20):
        for arms in (_hi_arm(), _lo_arm(), {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}):
            w = runner.effective_track_weight("ai-agents", CFG, arms, seed)
            assert 0.5 * static - 1e-6 <= w <= 1.5 * static + 1e-6


# 6, deterministic / replay-safe: same (arms, seed) => byte-identical score
def test_determinism_replay():
    a = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=13)["final_score"]
    b = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=13)["final_score"]
    assert a == b


# 7, reward loop closes: a PUSHED card's track arm gains evidence (alpha up, n+1)
def test_reward_loop_pushed_updates_arm():
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    res = runner.process([_cand(timing=98)], CFG, ledger=None, dry_run=True,
                         bandit_arms=arms, bandit_seed=1)
    assert res["pushed"], "fixture must push so there is a positive outcome to learn from"
    nxt = res["bandit_arms_next"]["ai-agents"]
    assert nxt["alpha"] > arms["ai-agents"]["alpha"]
    assert nxt["n"] == arms["ai-agents"]["n"] + 1


# 8, input arms are never mutated (PURE feedback)
def test_input_arms_not_mutated():
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    snapshot = dict(arms["ai-agents"])
    runner.process([_cand()], CFG, ledger=None, dry_run=True, bandit_arms=arms, bandit_seed=1)
    assert arms["ai-agents"] == snapshot


# 9, only real outcomes teach the bandit: a run with NO actionable card leaves arms unchanged
def test_no_actionable_no_learning():
    arms = {"ai-agents": {"alpha": 3.0, "beta": 2.0, "n": 5}}
    # single-source candidate is gated out at the red line => not actionable
    weak = _cand(sources=["hackernews"])
    res = runner.process([weak], CFG, ledger=None, dry_run=True, bandit_arms=arms, bandit_seed=1)
    assert res["below_sources"]
    assert res["bandit_arms_next"]["ai-agents"] == arms["ai-agents"]


# 10, exploration actually varies with the seed (not a constant greedy pick)
def test_exploration_varies_with_seed():
    cold = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    vals = {runner.effective_track_weight("ai-agents", CFG, cold, s) for s in range(30)}
    assert len(vals) > 1, "a cold (uniform) arm must explore a range, not return one constant"


# 11, boundedness end-to-end: even an extreme arm keeps final_score in [0,100]
def test_score_bounds_hold_under_extreme_arm():
    extreme = {"ai-agents": {"alpha": 1e6, "beta": 1.0, "n": 1}}
    fs = runner.build_card(_cand(), CFG, "r", arms=extreme, seed=3)["final_score"]
    assert 0.0 <= fs <= 100.0


# 12, flows through the real score_opportunity(track_weight=) seam => monotone in arm quality
def test_monotone_in_arm_quality():
    lo = runner.build_card(_cand(), CFG, "r", arms=_lo_arm(), seed=5)["final_score"]
    hi = runner.build_card(_cand(), CFG, "r", arms=_hi_arm(), seed=5)["final_score"]
    assert hi >= lo


# ===================================================================================================
# R6 REACHABILITY. Everything above pinned a bandit that no entry point could switch on: process()
# took persist_bandit, main() defined no flag for it, so the learning loop had never turned once in
# production. These pin the parts that make it usable: the CLI switch, the per-run seed, the
# decision report, and the promise that OFF is still byte-identical.
# ===================================================================================================
import json

import bandit as bdt


def _argv(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["run.py", *args])


def _capture_process(monkeypatch):
    seen = {}

    def fake(candidates, cfg=None, ledger=None, **kw):
        seen.update(kw)
        return {"errors": [], "digest_markdown": ""}

    monkeypatch.setattr(runner, "process", fake)
    return seen


# 13, THE flag exists and reaches process()
def test_bandit_flag_turns_the_loop_on(monkeypatch, capsys):
    seen = _capture_process(monkeypatch)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("[]"))
    _argv(monkeypatch, "--no-ledger", "--dry-run", "--bandit")
    assert runner.main() == 0
    capsys.readouterr()
    assert seen.get("persist_bandit") is True


# 14, and it is OFF unless asked (negative control for 13: without it the loop must stay dark)
def test_bandit_is_off_without_the_flag(monkeypatch, capsys):
    seen = _capture_process(monkeypatch)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("[]"))
    _argv(monkeypatch, "--no-ledger", "--dry-run")
    assert runner.main() == 0
    capsys.readouterr()
    assert seen.get("persist_bandit") is False


# 15, the config switch is the other way in (no flag needed)
def test_config_enabled_turns_it_on_without_the_flag(monkeypatch, capsys):
    import copy
    on = copy.deepcopy(CFG)
    on["scoring"]["bandit"]["enabled"] = True
    monkeypatch.setattr(runner, "load_config", lambda *a, **k: on)
    seen = _capture_process(monkeypatch)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("[]"))
    _argv(monkeypatch, "--no-ledger", "--dry-run")
    assert runner.main() == 0
    capsys.readouterr()
    assert seen.get("persist_bandit") is True


# 16, OFF is byte-identical: the static run result carries NO bandit block at all
def test_off_run_result_has_no_bandit_block():
    res = runner.process([_cand()], CFG, ledger=None, dry_run=True)
    assert "bandit" not in res
    assert res["bandit_arms_next"] is None
    assert sorted(res) == [
        "archived", "bandit_arms_next", "below_sources", "blocked", "built", "candidates",
        "community_pulse", "coverage", "digest_markdown", "digest_path", "empty_day", "errors",
        "excluded", "new", "pushed", "resurface", "run_id", "suppressed", "watermark_advanced"]


# 17, ON: the run REPORTS what the bandit did, per track, and the numbers reconcile
def test_bandit_block_reports_every_draw():
    res = runner.process([_cand()], CFG, ledger=None, dry_run=True,
                         bandit_arms=_hi_arm(), bandit_seed=7)
    rep = res["bandit"]
    assert rep["seed"] == 7
    row = next(r for r in rep["tracks"] if r["track"] == "ai-agents")
    assert row["scored"] is True
    assert row["static_weight"] == runner._track_weight("ai-agents", CFG)
    assert row["explore_multiplier"] == bdt.explore_weight(_hi_arm(), "ai-agents", 7, CFG)
    assert row["effective_weight"] == round(row["static_weight"] * row["explore_multiplier"], 6)
    # the posterior moved, and the report says by how much and off how many outcomes
    assert row["n_after"] == row["n_before"] + 1
    assert row["pulls_this_run"] == 1
    assert row["reward_this_run"] > 0
    json.dumps(rep)  # the report must survive the CLI's json.dumps


# 18, "saved" and "nothing was written" are different words, never both silent
def test_persist_state_names_why_nothing_was_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    dry = runner.process([_cand()], CFG, ledger=None, dry_run=True, bandit_arms=dict(arms))
    assert dry["bandit"]["persist_state"] == "not-requested"
    assert dry["bandit"]["persisted"] is False

    from test_bandit_persist import _FakeLedger
    ok = runner.process([_cand()], CFG, ledger=_FakeLedger(arms=dict(arms)), dry_run=False,
                        archive_dir=str(tmp_path), persist_bandit=True)
    assert ok["bandit"]["persist_state"] == "saved" and ok["bandit"]["persisted"] is True

    bad = runner.process([_cand()], CFG, ledger=_FakeLedger(arms=dict(arms), fail_upsert=True),
                         dry_run=False, archive_dir=str(tmp_path), persist_bandit=True)
    assert bad["bandit"]["persist_state"] == "held-errors"
    assert bad["bandit"]["persisted"] is False


# 19, the per-run seed: replayable for one run_id, different across days
def test_run_seed_is_replayable_and_moves_between_runs():
    assert bdt.run_seed("daily-2026-06-25") == bdt.run_seed("daily-2026-06-25")
    assert bdt.run_seed("daily-2026-06-25") != bdt.run_seed("daily-2026-06-26")


# 20, persist mode derives that seed from run_id; an explicit seed still wins
def test_persist_mode_derives_the_seed_from_the_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    from test_bandit_persist import _FakeLedger
    arms = {"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    a = runner.process([_cand()], CFG, ledger=_FakeLedger(arms=dict(arms)), dry_run=False,
                       run_id="daily-2026-06-25", archive_dir=str(tmp_path), persist_bandit=True)
    assert a["bandit"]["seed"] == bdt.run_seed("daily-2026-06-25")
    b = runner.process([_cand()], CFG, ledger=_FakeLedger(arms=dict(arms)), dry_run=False,
                       run_id="daily-2026-06-26", archive_dir=str(tmp_path), persist_bandit=True)
    assert b["bandit"]["seed"] != a["bandit"]["seed"], "a fixed seed would freeze exploration"
    c = runner.process([_cand()], CFG, ledger=_FakeLedger(arms=dict(arms)), dry_run=False,
                       run_id="daily-2026-06-25", archive_dir=str(tmp_path), persist_bandit=True,
                       bandit_seed=4242)
    assert c["bandit"]["seed"] == 4242


# 21, the track weight is drawn ONCE per track, not once per candidate
def test_track_weight_is_computed_once_per_track(monkeypatch):
    calls = []
    real = bdt.explore_weight
    monkeypatch.setattr(bdt, "explore_weight",
                        lambda arms, track, seed=0, cfg=None: (calls.append(track),
                                                               real(arms, track, seed, cfg))[1])
    cands = [_cand(title=f"MCP agent framework launch {i}") for i in range(5)]
    res = runner.process(cands, CFG, ledger=None, dry_run=True, bandit_arms=_hi_arm(), bandit_seed=7)
    assert res["built"] == 5, "the fixture must really produce 5 scored cards"
    assert calls == ["ai-agents"], f"one draw per track, got {calls}"


# ===================================================================================================
# run.py process() loop structure. Not a bandit assertion, but it guards the same function the
# bandit wiring above lives in: the ledger match used to be recomputed from scratch in the upsert
# loop, a full simhash/Jaccard scan of every ledger row per card, for a value the dedup loop had
# already produced from inputs that cannot change in between.
# ===================================================================================================
import dedup as dd


class _RecordingLedger:
    """Enough LedgerClient surface for process(), and it remembers what it was asked to write."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.upserts = []

    def list_active(self, limit=500):
        return list(self.rows)

    def upsert(self, candidate, ext, title=None, state="pending"):
        self.upserts.append((candidate.get("canonical_key"), ext))
        return {"item": {"id": "x"}}

    def add_watermark(self, last_run_at):
        pass

    def _run(self, verb, args):
        return {}


# 22, the expensive ledger match runs once per card, not once per card per loop
def test_ledger_match_is_computed_once_per_card(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    calls = []
    real = dd.match_existing
    monkeypatch.setattr(dd, "match_existing",
                        lambda c, rows, cfg=None: (calls.append(c["canonical_key"]),
                                                   real(c, rows, cfg))[1])
    led = _RecordingLedger()
    cands = [_cand(title="MCP agent framework launch"),
             _cand(title="LLM eval harness launch")]
    res = runner.process(cands, CFG, ledger=led, dry_run=False, archive_dir=str(tmp_path))
    # positive controls: the run really built both cards AND really reached the upsert loop, which
    # is the site that used to re-scan. A run that quietly built nothing would pass a bare count.
    assert res["built"] == 2, "the fixture must really produce 2 scored cards"
    assert len(led.upserts) == 2, "the upsert loop must really have run"
    assert len(calls) == 2, f"one ledger match per card, got {len(calls)}: {calls}"


# 23, and the hoisted row is still USED: an existing row's history must reach the upsert ext
def test_hoisted_match_still_carries_the_prior_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    seed_card = runner.build_card(_cand(), CFG, "day0")
    prior_row = {"idempotency_key": seed_card["canonical_key"],
                 "ext": {dd.EXT_PREFIX + "canonical_key": seed_card["canonical_key"],
                         dd.EXT_PREFIX + "first_seen": "2026-06-01T00:00:00Z",
                         dd.EXT_PREFIX + "text": seed_card["title"] + " " + seed_card["summary"],
                         dd.EXT_PREFIX + "push_count": 3}}
    led = _RecordingLedger(rows=[prior_row])
    runner.process([_cand()], CFG, ledger=led, dry_run=False, archive_dir=str(tmp_path))
    assert led.upserts, "the upsert loop must really have run"
    _, ext = led.upserts[0]
    assert ext[dd.EXT_PREFIX + "first_seen"] == "2026-06-01T00:00:00Z", \
        "the matched row's history must survive the hoist, not restart at today"
