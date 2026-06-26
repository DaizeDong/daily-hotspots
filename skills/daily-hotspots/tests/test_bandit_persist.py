"""R6 loop close: the bandit posterior must PERSIST across runs (HEAD emits bandit_arms_next but
never stores/reloads it, so every run cold-starts and the learning evaporates). Pins:

  * pure, deterministic, DEFENSIVE (de)serialization of arm state for storage;
  * LedgerClient.set/get_bandit_arms round-trip (real reminder.py base);
  * run.process(persist_bandit=True) hydrates arms from the ledger and saves the learned arms back,
    gated on a clean run (same atomicity as the watermark); default stays byte-identical.

Capability assertions, marked xfail(strict=False) so the green baseline stays green until landed.
"""
from pathlib import Path

import pytest

import bandit as bdt
import dedup as dd
import run as runner
from lib import load_config

CFG = load_config()
# Headroom landed (self-evolve gate ACCEPT e=40.36, +10, 0 regressed): permanent guards on the
# bandit posterior persistence loop.

REMINDER = Path.home() / ".claude/skills/schedule-reminder/scripts/reminder.py"
_has_base = REMINDER.is_file()


def _cand(title="MCP agent framework launch", timing=98):
    return {
        "title": title, "summary": "open source llm agent tooling", "track": "ai-agents",
        "entities": (title + " open source llm agent").lower().split(),
        "evidence": [{"source": s, "origin": s + ".com", "url": "http://" + s + "/x",
                      "signal": "sig", "ts": "2026-06-25T11:00:00Z"} for s in ("hackernews", "product-hunt")],
        "score_breakdown": {"track_fit": 85, "timing": timing, "feasibility": 75,
                            "competition": 65, "executability": 82},
        "age_hours": 4.0, "velocity": 0.2, "lifecycle_stage": "emerging",
        "why_now": "platform shift now", "contrarian_insight": "most think X, really Y",
        "action": "ship MVP this week",
    }


# 1 — capability surface exists
def test_capability_surface_exists():
    assert hasattr(bdt, "serialize_arms") and hasattr(bdt, "deserialize_arms")
    assert hasattr(dd.LedgerClient, "set_bandit_arms") and hasattr(dd.LedgerClient, "get_bandit_arms")


# 2 — round-trip identity for clean arms
def test_serialize_roundtrip_identity():
    arms = {"ai-agents": {"alpha": 3.0, "beta": 2.0, "n": 4},
            "dev-tools": {"alpha": 1.0, "beta": 1.0, "n": 0}}
    assert bdt.deserialize_arms(bdt.serialize_arms(arms)) == arms


# 3 — serialized form is JSON-safe
def test_serialized_is_json_safe():
    import json
    arms = {"ai-agents": {"alpha": 3.0, "beta": 2.0, "n": 4}}
    s = bdt.serialize_arms(arms)
    json.loads(json.dumps(s))  # must not raise


# 4 — defensive: negative/zero Beta params clamped to a valid arm (params > 0)
def test_deserialize_clamps_invalid_beta_params():
    bad = {"ai-agents": {"alpha": -5.0, "beta": 0.0, "n": 3}}
    arm = bdt.deserialize_arms(bad)["ai-agents"]
    assert arm["alpha"] > 0 and arm["beta"] > 0
    # a clamped arm must still produce a finite draw (no crash / NaN)
    w = bdt.explore_weight({"ai-agents": arm}, "ai-agents", seed=1)
    assert w == w  # not NaN


# 5 — defensive: non-numeric garbage falls back to the prior
def test_deserialize_garbage_falls_back():
    bad = {"ai-agents": {"alpha": "oops", "beta": None, "n": "x"}}
    arm = bdt.deserialize_arms(bad)["ai-agents"]
    assert arm["alpha"] > 0 and arm["beta"] > 0 and arm["n"] == 0


# 6 — defensive: non-dict input -> {}
def test_deserialize_non_dict():
    assert bdt.deserialize_arms(None) == {}
    assert bdt.deserialize_arms([1, 2, 3]) == {}


# 7 — deterministic: serialize is byte-identical across calls (sorted keys)
def test_serialize_deterministic():
    import json
    arms = {"z": {"alpha": 1.0, "beta": 1.0, "n": 0}, "a": {"alpha": 2.0, "beta": 1.0, "n": 1}}
    assert json.dumps(bdt.serialize_arms(arms)) == json.dumps(bdt.serialize_arms(arms))


# 8 — unknown extra fields are dropped on deserialize (only alpha/beta/n survive)
def test_deserialize_drops_extra_fields():
    arm = bdt.deserialize_arms({"ai-agents": {"alpha": 2.0, "beta": 3.0, "n": 1, "junk": 9}})["ai-agents"]
    assert set(arm.keys()) == {"alpha", "beta", "n"}


# ---------------- process integration with a deterministic fake ledger ----------------
class _FakeLedger:
    def __init__(self, arms=None, fail_upsert=False):
        self._arms = arms or {}
        self.fail_upsert = fail_upsert
        self.saved = None
        self.get_calls = 0
        self.set_calls = 0

    def list_active(self, limit=500):
        return []

    def upsert(self, candidate, ext, title=None, state="pending"):
        if self.fail_upsert:
            raise RuntimeError("simulated failure")
        return {"item": {"id": "x"}}

    def add_watermark(self, last_run_at):
        pass

    def _run(self, verb, args):
        return {}

    def get_bandit_arms(self):
        self.get_calls += 1
        return dict(self._arms)

    def set_bandit_arms(self, arms):
        self.set_calls += 1
        self.saved = arms


# 9 — persist mode hydrates from the ledger and saves the learned arms back
def test_process_persist_loads_and_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    led = _FakeLedger(arms={"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}})
    res = runner.process([_cand()], CFG, ledger=led, dry_run=False, archive_dir=str(tmp_path),
                         persist_bandit=True)
    assert led.get_calls == 1, "must hydrate the posterior from the ledger"
    assert led.set_calls == 1 and led.saved is not None, "must persist the learned posterior"
    assert led.saved["ai-agents"]["n"] == 1, "the pushed outcome must be recorded into the arm"


# 10 — persistence is gated on a clean run (a failed write must NOT bake in the posterior)
def test_process_persist_held_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    led = _FakeLedger(arms={"ai-agents": {"alpha": 1.0, "beta": 1.0, "n": 0}}, fail_upsert=True)
    res = runner.process([_cand()], CFG, ledger=led, dry_run=False, archive_dir=str(tmp_path),
                         persist_bandit=True)
    assert res["errors"], "the upsert failure must be surfaced"
    assert led.set_calls == 0, "a partial-failure run must NOT persist the bandit posterior"
    assert res["watermark_advanced"] is False


# 11 — backward compat: without persist_bandit, the ledger bandit API is never touched
def test_no_persist_never_touches_bandit_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_DRYRUN", "1")
    led = _FakeLedger(arms={"ai-agents": {"alpha": 5.0, "beta": 1.0, "n": 6}})
    runner.process([_cand()], CFG, ledger=led, dry_run=False, archive_dir=str(tmp_path))
    assert led.get_calls == 0 and led.set_calls == 0


# 12 — real base round-trip (runs when schedule-reminder is installed)
@pytest.mark.skipif(not _has_base, reason="schedule-reminder reminder.py not installed")
def test_real_ledger_bandit_roundtrip(tmp_path):
    db = str(tmp_path / "b.db")
    lc = dd.LedgerClient(db_path=db)
    lc.init()
    arms = {"ai-agents": {"alpha": 7.0, "beta": 3.0, "n": 9},
            "dev-tools": {"alpha": 2.0, "beta": 2.0, "n": 2}}
    lc.set_bandit_arms(arms)
    got = lc.get_bandit_arms()
    assert got == bdt.serialize_arms(arms)
    # singleton: a second save replaces, never duplicates
    lc.set_bandit_arms({"ai-agents": {"alpha": 8.0, "beta": 3.0, "n": 10}})
    got2 = lc.get_bandit_arms()
    assert got2["ai-agents"]["alpha"] == 8.0
