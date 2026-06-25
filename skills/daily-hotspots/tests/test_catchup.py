"""R5 headroom: overslept-machine digest catch-up (at-least-once + dedupe).

The OS scheduler (Windows Task / cron) is NOT exactly-once: if the laptop is asleep over a
weekend the scheduled daily runs are simply skipped, and HEAD silently loses those days — there
is NO backfill. ARCHITECTURE §5.4 ("防双推/漏采") + §11 ("回写水位…全成功才推进") + ROADMAP R5
("机器睡过头补发 at-least-once+dedupe") require that, on wake-up, the system enumerates the
calendar dates missed since the last watermark and ensures each missed day's digest is emitted
exactly once — backfilling without ever double-sending an already-delivered day.

HEAD has neither a `missed_digest_dates` enumerator nor a `catch_up_digests` registrar; the
watermark is written but never *read to recover* missed slots. These tests assert the CAPABILITY
(bounded at-least-once backfill + idempotent dedupe via the per-date key), not any particular
date-arithmetic implementation.

Status: xfail headroom for self-evolve batch 4. The minimal additive fix in digest.py flips them
to XPASS; markers are then removed so they stand as permanent regression guards.
"""
import pytest

import digest as dg

_HAS_MISSED = hasattr(dg, "missed_digest_dates")
_HAS_CATCHUP = hasattr(dg, "catch_up_digests")
xfail = pytest.mark.xfail(reason="R5 catch-up not yet implemented", strict=False)

NOW = "2026-06-25T12:00:00Z"


def _missed(last_run, now=NOW, **kw):
    return dg.missed_digest_dates(last_run, now=now, **kw)


# --------------------------------------------------------------- presence
@xfail
def test_capability_present():
    assert _HAS_MISSED and _HAS_CATCHUP


# --------------------------------------------------------------- core backfill
@xfail
def test_overslept_three_days_backfills_each_missed_date():
    # last digest covered 2026-06-22; woke up 2026-06-25 -> 23,24,25 must be backfilled.
    got = _missed("2026-06-22T08:07:00Z")
    assert got == ["2026-06-23", "2026-06-24", "2026-06-25"]


@xfail
def test_normal_daily_run_yields_only_today():
    # ran yesterday, running today -> exactly one date (today), no backfill noise.
    assert _missed("2026-06-24T08:07:00Z") == ["2026-06-25"]


@xfail
def test_same_day_rerun_yields_nothing():
    # watermark already on today's date -> no missed slot (dedupe: never re-emit today).
    assert _missed("2026-06-25T03:00:00Z") == []


# --------------------------------------------------------------- first run / no watermark
@xfail
def test_first_run_no_watermark_only_today_no_storm():
    # cold start (None / empty) must NOT backfill the epoch — just today, bounded.
    assert _missed(None) == ["2026-06-25"]
    assert _missed("") == ["2026-06-25"]


# --------------------------------------------------------------- determinism
@xfail
def test_deterministic_byte_identical():
    a = _missed("2026-06-20T08:07:00Z")
    b = _missed("2026-06-20T08:07:00Z")
    assert a == b and a == sorted(a)  # pure + ascending


# --------------------------------------------------------------- boundedness (anti-flood)
@xfail
def test_long_outage_is_bounded_and_recent():
    # asleep ~400 days: must NOT flood; cap the backfill and keep the MOST RECENT dates incl today.
    got = _missed("2025-05-20T08:07:00Z", cap=30)
    assert len(got) <= 30
    assert got[-1] == "2026-06-25"          # today is always covered (at-least-once)
    assert got == sorted(got)               # ascending
    assert len(set(got)) == len(got)        # no duplicates


# --------------------------------------------------------------- clock skew robustness
@xfail
def test_future_watermark_clock_skew_no_garbage():
    # watermark ahead of now (skew) -> never negative/garbage; at most today, never empty crash.
    got = _missed("2026-07-01T00:00:00Z")
    assert isinstance(got, list)
    assert all(d <= "2026-06-25" for d in got)


# --------------------------------------------------------------- timezone-aware date boundary
@xfail
def test_tz_offset_uses_local_calendar_date():
    # 02:00Z with a -5h offset is still 2026-06-24 *locally* -> today_local = 2026-06-24.
    got = dg.missed_digest_dates("2026-06-23T00:00:00Z", now="2026-06-25T02:00:00Z",
                                 tz_offset_h=-5)
    assert got[-1] == "2026-06-24"


# --------------------------------------------------------------- idempotent catch-up registrar
class _FakeLedger:
    """Mimics the per-date idempotency_key UPSERT of the base: same key => no new row."""
    def __init__(self):
        self.keys = {}

    def _run(self, verb, args):
        key = args[args.index("--idempotency-key") + 1]
        first = key not in self.keys
        self.keys.setdefault(key, 0)
        self.keys[key] += 1
        return {"item": {"id": "id-" + key, "new": first}}


@xfail
def test_catch_up_registers_one_item_per_missed_date():
    lg = _FakeLedger()
    dates = dg.catch_up_digests(lg, "2026-06-22T08:07:00Z", now=NOW)
    assert dates == ["2026-06-23", "2026-06-24", "2026-06-25"]
    # exactly one distinct digest key per missed date
    digest_keys = [k for k in lg.keys if k.startswith("daily-hotspots:digest:")]
    assert len(digest_keys) == 3


@xfail
def test_catch_up_is_idempotent_dedupe():
    lg = _FakeLedger()
    dg.catch_up_digests(lg, "2026-06-23T08:07:00Z", now=NOW)   # -> 24, 25
    dg.catch_up_digests(lg, "2026-06-23T08:07:00Z", now=NOW)   # re-run: dedupe, no NEW keys
    digest_keys = [k for k in lg.keys if k.startswith("daily-hotspots:digest:")]
    assert len(digest_keys) == 2                                # still 2 distinct dates
    assert all(v == 2 for v in lg.keys.values())                # each seen twice = UPSERT, not new


@xfail
def test_catch_up_no_missed_registers_nothing():
    lg = _FakeLedger()
    dates = dg.catch_up_digests(lg, "2026-06-25T03:00:00Z", now=NOW)  # same-day
    assert dates == []
    assert lg.keys == {}                                        # no spurious registration
