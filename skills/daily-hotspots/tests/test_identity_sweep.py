"""identity_sweep.sweep parallelism, correct handle->data mapping + fail-loud propagation.

The sweep was parallelized (bounded ThreadPool over the paid twitterapi.io calls). The two
invariants that MUST survive parallelization:
  1. infos maps every enabled handle to its OWN fetched data (no cross-handle mixups despite
     out-of-order completion).
  2. fail-loud: a transient-exhausted fetch RAISES, and that must propagate out of sweep so the
     sweep aborts rather than silently marking a live account dead (the §9 guardrail's whole point).
No network: fetch_one is monkeypatched to a deterministic stub keyed by handle.
"""
import concurrent.futures

import pytest

import identity_sweep as S


def _roster(handles):
    # entries_of accepts a bare list of entry dicts (roster.py:83)
    return [{"handle": h, "enabled": True} for h in handles]


def test_parallel_sweep_maps_each_handle_to_its_own_data(monkeypatch):
    handles = [f"user{i}" for i in range(30)]

    def stub(handle, token, timeout, retries=3):
        # data whose statusesCount encodes the handle index -> lets us prove no cross-mixup
        idx = int(handle[4:])
        if idx % 5 == 0:
            return None  # a "gone" account (real signal)
        return {"userName": handle, "statusesCount": idx + 1}

    monkeypatch.setattr(S, "fetch_one", stub)
    infos = S.sweep(_roster(handles), token="t", delay=0.0, timeout=1.0, max_workers=8)

    assert set(infos) == set(handles), "every enabled handle must appear exactly once"
    for h in handles:
        idx = int(h[4:])
        if idx % 5 == 0:
            assert infos[h] is None, f"{h} should map to its own None (gone)"
        else:
            assert infos[h]["userName"] == h, f"{h} mapped to another handle's data (parallel mixup)"
            assert infos[h]["statusesCount"] == idx + 1


def test_serial_fallback_matches(monkeypatch):
    handles = ["a", "b", "c"]
    monkeypatch.setattr(S, "fetch_one",
                        lambda h, token, timeout, retries=3: {"userName": h, "statusesCount": 1})
    infos = S.sweep(_roster(handles), token="t", delay=0.0, timeout=1.0, max_workers=1)
    assert set(infos) == set(handles)
    assert all(infos[h]["userName"] == h for h in handles)


def test_fail_loud_propagates(monkeypatch):
    """A transient-exhausted fetch (RuntimeError) must abort the whole sweep, not be swallowed."""
    handles = [f"u{i}" for i in range(20)]

    def stub(handle, token, timeout, retries=3):
        if handle == "u7":
            raise RuntimeError("twitterapi.io failed for @u7 after 3 tries")
        return {"userName": handle, "statusesCount": 1}

    monkeypatch.setattr(S, "fetch_one", stub)
    with pytest.raises(RuntimeError, match="u7"):
        S.sweep(_roster(handles), token="t", delay=0.0, timeout=1.0, max_workers=8)
    # serial path must fail loud too
    with pytest.raises(RuntimeError, match="u7"):
        S.sweep(_roster(handles), token="t", delay=0.0, timeout=1.0, max_workers=1)
