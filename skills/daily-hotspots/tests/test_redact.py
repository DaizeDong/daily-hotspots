"""Egress PII scrub for the pushed daily-hotspots digest (backported from demand-mining/redact.py).

SYNTHETIC PII ONLY (fake emails/phones/tokens). Two things this suite pins down:
  * the vendored Tier1/Tier2 core (redact/has_pii/pseudonymize) still works, and
  * the daily-hotspots-specific egress policy scrubs ONLY the dangerous structured types in place
    while LEAVING evidence URLs (<...>) and @handles, which are legitimate headline content, alone.
"""
import redact
from redact import redact as core_redact, has_pii, pseudonymize, scrub_egress, redact_egress

import push_card as pc


# --------------------------------------------------------------------------- vendored core sanity
def test_core_redact_email_phone():
    r = core_redact("ping jane.doe@acme.io or +1 (555) 867-5309 please")
    assert "jane.doe@acme.io" not in r["redacted"]
    assert "8675309" not in r["redacted"].replace(" ", "")
    assert r["found"].get("EMAIL") == 1 and r["found"].get("PHONE") == 1


def test_core_pseudonym_stable_irreversible():
    a, b, c = pseudonymize("user-123"), pseudonymize("user-123"), pseudonymize("user-999")
    assert a == b and a != c
    assert a.startswith("u_") and "user-123" not in a


def test_core_has_pii_still_flags_url_and_handle():
    # the sibling gate is intentionally strict (URL/HANDLE == PII); that is WHY we cannot use it as
    # the daily-hotspots egress gate and need scrub_egress instead.
    assert has_pii("see https://example.com/a") is True
    assert has_pii("cc @cooluser") is True


# --------------------------------------------------------------------------- (a) dangerous PII scrubbed
def test_egress_scrubs_email_phone_secret():
    msg = ("**1.【AI】新工具** 联系作者 jane.doe@acme.io 或 +1 (555) 867-5309，"
           "key sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a")
    out = scrub_egress(msg)
    assert "jane.doe@acme.io" not in out
    assert "8675309" not in out.replace(" ", "")
    assert "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c" not in out
    assert "[EMAIL_1]" in out and "[PHONE_1]" in out and "[SECRET_1]" in out


def test_egress_scrubs_card_ip_discord_id():
    # 4111 1111 1111 1111 is Luhn-valid; a bare 18-digit snowflake is a discord id; 203.0.113.9 an ip
    msg = "card 4111 1111 1111 1111 host 203.0.113.9 user <@123456789012345678> and 987654321098765432"
    out = scrub_egress(msg)
    assert "4111" not in out and "203.0.113.9" not in out
    assert "123456789012345678" not in out and "987654321098765432" not in out
    assert "[CARD_1]" in out and "[IP_1]" in out and "[DISCORD_ID_1]" in out


# --------------------------------------------------------------------------- (b) content is preserved
def test_egress_leaves_evidence_url_and_handle():
    msg = "**1.【AI】某话题**\n某摘要\n🔗 <https://x.com/a/1>　·　A 90 · 3源  cc @founder"
    out = scrub_egress(msg)
    assert "<https://x.com/a/1>" in out          # evidence link untouched (angle wrapper intact)
    assert "@founder" in out                     # legitimate handle untouched
    assert redact_egress(msg)["changed"] is False


def test_egress_does_not_mangle_long_tweet_status_id_in_url():
    # a real tweet permalink embeds a 19-digit status id that the bare-snowflake DISCORD_ID rule
    # would otherwise eat, the url stash must protect it so the evidence link survives verbatim.
    url = "https://x.com/some_user/status/1234567890123456789"
    out = scrub_egress(f"看这条 <{url}> 很关键")
    assert url in out                            # digits inside the url are NOT redacted
    assert "[DISCORD_ID" not in out


def test_egress_leaves_bare_url_without_angle_brackets():
    msg = "ref https://news.ycombinator.com/item?id=999 详情"
    assert scrub_egress(msg) == msg              # clean http(s) link, nothing dangerous -> verbatim


# --------------------------------------------------------------------------- (c) clean passes unchanged
def test_egress_clean_message_unchanged_byte_for_byte():
    # full-width spaces (U+3000) in the real headline layout must NOT be NFKC-rewritten on a clean day
    msg = "📰 **前沿机会头条** · 2026-07-15\n合格 3 · 精选 3\n\n**1.【AI】x**　·　A 90 · 2源"
    assert scrub_egress(msg) == msg
    assert redact_egress(msg)["changed"] is False


def test_egress_leaves_year_range_and_year_list():
    # frontier headlines are full of year ranges / CAGR windows; the loose phone matcher would eat an
    # 8-digit 'YYYY-YYYY' or a run of years and print [PHONE_1]. The year-run guard must leave them.
    for msg in ("市场 2020-2026 复合增速 40%", "覆盖 2019 2020 2021 2022 四个年度", "窗口 2023-2030 展望"):
        assert scrub_egress(msg) == msg, msg
        assert redact_egress(msg)["changed"] is False, msg
    # a real phone in the same neighbourhood is STILL redacted (guard is year-only, not digit-count)
    hot = scrub_egress("增长 2020-2026，热线 +1 (555) 867-5309")
    assert "2020-2026" in hot and "8675309" not in hot.replace(" ", "") and "[PHONE_1]" in hot


def test_egress_report_flags_changed_and_types():
    r = redact_egress("mail me at bob@example.com")
    assert r["changed"] is True
    assert r["found"].get("EMAIL") == 1
    assert "bob@example.com" not in r["redacted"]


# --------------------------------------------------------------------------- deliver() wiring
def _patch_relay(monkeypatch):
    """Capture the exact text handed to the relay subprocess; never actually spawn one."""
    captured = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        captured["message"] = cmd[-1]
        return _Proc()

    monkeypatch.setattr(pc.subprocess, "run", fake_run)
    monkeypatch.delenv("DAILY_HOTSPOTS_DRYRUN", raising=False)
    return captured


def test_deliver_scrubs_email_before_handing_to_relay(monkeypatch, capsys):
    captured = _patch_relay(monkeypatch)
    ok, detail = pc.deliver("头条：作者邮箱 leak.me@evil.example.com 见正文")
    assert ok is True
    assert "leak.me@evil.example.com" not in captured["message"]   # relay never sees the address
    assert "[EMAIL_1]" in captured["message"]
    assert "egress scrub" in capsys.readouterr().err               # one-line note logged


def test_deliver_passes_clean_headline_unchanged(monkeypatch, capsys):
    captured = _patch_relay(monkeypatch)
    headline = "📰 头条\n**1.【AI】x**\n🔗 <https://x.com/a/1>　·　A 90 · 2源  cc @maker"
    ok, _ = pc.deliver(headline)
    assert ok is True
    assert captured["message"] == headline                          # byte-identical, url+handle kept
    assert "egress scrub" not in capsys.readouterr().err            # nothing scrubbed -> no note


def test_deliver_dry_run_still_scrubs_length():
    # dry-run returns a length report; the reported length must be of the SCRUBBED message, not the
    # raw one, assert the exact count so a regression that reports pre-scrub length is caught.
    raw = "邮箱 a@b.example.com 结束"
    scrubbed_len = len(scrub_egress(raw))
    ok, detail = pc.deliver(raw, dry_run=True)
    assert ok is True and "dry-run" in detail
    assert f"{scrubbed_len} chars" in detail and scrubbed_len != len(raw)
