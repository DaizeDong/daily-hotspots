#!/usr/bin/env python3
"""ONE url gate, and the STRONG one is the egress gate.

Until 2026-08-29 the pipeline carried two url checks and the weaker one guarded the push:

  * ``run.safe_url`` parses the authority and refuses ``userinfo@``, invisible characters and an
    unbounded length, but only the six demand parsers ever called it.
  * ``digest._clean_url`` was a shape check (starts with http(s)://, holds no space, tab, CR, LF,
    ``<`` or ``>``) and it was the ONLY url check the main pipeline's agent-collected evidence met
    on its way into the pushed Discord message.

So an evidence url the agent handed in could carry ``@evil.example`` (the request goes to
evil.example), a bidi override or zero-width character inside the host, or four thousand
characters, and get pushed. One level down the same hole: ``digest._inline`` collapses whitespace,
Python's \\s does NOT match the Cf category, and so a zero-width space or a bidi override in an
LLM-supplied TITLE rode straight into the message.

Both sides now call ``lib.safe_url`` / ``lib.strip_invisible``.

These tests drive the REAL egress entry points, ``build_headlines`` (the pushed message) and
``build_markdown`` (the archived digest), not the helper in the middle: a test that asserts on the
helper keeps passing while the dispatch site goes on calling the weak check.

Deterministic: stdlib only, no network, clock frozen by conftest.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lib  # noqa: E402
import digest as dg  # noqa: E402

ZWSP = "\u200b"      # zero width space
BIDI = "\u202e"      # right-to-left override
DATE = "2026-08-29"

# A url that is long, real-shaped, and carries a query string. This is the OVER-REJECTION control:
# the gate got stricter, and the way a stricter gate fails is by eating the traffic it exists to
# carry, which no hostile-input table can detect.
REAL_LONG_URL = ("https://news.example.com/technology/artificial-intelligence/"
                 "a-genuinely-long-article-slug-of-the-kind-newsrooms-publish-2026-08-29/"
                 "?utm_source=daily-hotspots&utm_medium=push&utm_campaign=digest&page=2")

# (id, url, the payload substring that must never appear downstream)
HOSTILE = [
    ("userinfo_authority", "https://good.example@evil.example/story/1", "evil.example"),
    ("bidi_override_in_host", "https://good.example" + BIDI + "/story/1", BIDI),
    ("zero_width_in_host", "https://good.example" + ZWSP + "/story/1", ZWSP),
    ("unbounded_length", "https://good.example/story/" + "a" * 4000, "a" * 300),
]
HOSTILE_PARAMS = [pytest.param(u, p, id=i) for i, u, p in HOSTILE]


def _card(**kw):
    """A minimal demand card: demand gets the rich headline treatment (numbered, linked)."""
    d = {"title": "一条正常的中文标题", "final_score": 90, "grade": "A", "track": "ai-agents",
         "independent_source_count": 2, "side": "demand", "why_now": "w"}
    d.update(kw)
    return d


# --------------------------------------------------------------- the four shapes accepted before

@pytest.mark.parametrize("url,payload", HOSTILE_PARAMS)
def test_a_hostile_url_never_reaches_the_pushed_message(url, payload):
    """Every one of these was emitted as a live link by the old shape check.

    Mutation proof: restore the old body of _clean_url and all four go red.
    """
    assert dg._clean_url(url) == ""
    out = dg.build_headlines([_card(evidence=[{"source": "hackernews", "url": url}])],
                             {}, date=DATE)
    assert payload not in out
    assert "good.example" not in out
    # the card SAYS it lost its link (the honesty budget), it does not quietly link somewhere else
    assert "链接被拒收" in out


@pytest.mark.parametrize("url,payload", HOSTILE_PARAMS)
def test_a_hostile_url_never_reaches_the_archived_digest(url, payload):
    """The archive is the other egress. It reads the same gate, so it must refuse the same set."""
    out = dg.build_markdown([_card(evidence=[{"source": "hackernews", "url": url,
                                              "signal": "s"}])], {}, date=DATE)
    assert payload not in out
    assert "good.example" not in out
    assert "(无效链接)" in out


def test_the_digest_gate_is_the_shared_gate_and_not_a_second_opinion():
    """The defect was two implementations, so the property under test is that there is one.

    Every input, hostile and benign, must get the identical verdict from the module the push calls
    and from the shared gate. A digest-side check that drifts back to its own rules fails here.
    """
    for url in [u for _i, u, _p in HOSTILE] + [REAL_LONG_URL, "https://a.example/x",
                                               "javascript:alert(1)", "https://", "", None, 12345]:
        assert dg._clean_url(url) == lib.safe_url(url), url


def test_angle_brackets_stay_refused_so_the_gate_did_not_get_looser_anywhere():
    """``<`` and ``>`` were the one thing the old shape check caught that safe_url did not.

    They matter precisely because the url is pasted into markdown that gets pushed to Discord.
    Moving to the stronger gate must not trade one hole for another.
    """
    assert dg._clean_url("https://a.example/<script>alert(1)</script>") == ""
    assert dg._clean_url("https://a.example/x>") == ""
    assert lib.safe_url("https://a.example/<", ("a.example",)) == ""


def test_host_pinning_survives_the_move_out_of_run_py():
    """The demand parsers' control: an untrusted review body cannot publish a link under this
    source's origin tag. Compared on parsed host segments, never on the raw string."""
    ok = "https://www.trustpilot.com/review/example.com?stars=1"
    assert lib.safe_url(ok, ("trustpilot.com",)) == ok
    assert lib.safe_url("https://www.trustpilot.com.attacker.example/x", ("trustpilot.com",)) == ""
    assert lib.safe_url("https://nottrustpilot.com/x", ("trustpilot.com",)) == ""


# ------------------------------------------------------------------- over-rejection control

def test_a_real_long_article_url_with_a_query_string_still_renders():
    """The control on the control: a stricter gate that eats real links is a worse bug than the
    one it fixed, and it would look identical from the hostile-input table alone."""
    assert dg._clean_url(REAL_LONG_URL) == REAL_LONG_URL
    out = dg.build_headlines([_card(evidence=[{"source": "feeds", "url": REAL_LONG_URL}])],
                             {}, date=DATE)
    assert "<" + REAL_LONG_URL + ">" in out
    assert "链接被拒收" not in out


# ----------------------------------------------------------------- invisible characters in text

def test_an_invisible_character_in_a_title_never_reaches_the_pushed_message():
    """Python's \\s does not match the Cf category, so the whitespace collapse alone let a
    zero-width space and a bidi override ride an LLM-supplied title into Discord."""
    out = dg.build_headlines([_card(title="AI" + ZWSP + "代理" + BIDI + "崩溃")], {}, date=DATE)
    assert ZWSP not in out and BIDI not in out
    assert "AI代理崩溃" in out


def test_an_invisible_character_in_a_title_never_reaches_the_archived_digest():
    out = dg.build_markdown([_card(title="AI" + ZWSP + "代理" + BIDI + "崩溃")], {}, date=DATE)
    assert ZWSP not in out and BIDI not in out
    assert "AI代理崩溃" in out


def test_an_ordinary_cjk_title_is_untouched():
    """The over-rejection control for the text sanitizer: stripping the Cf category must not touch
    ordinary Chinese, punctuation or emoji."""
    title = "国产大模型集体涨价，开发者连夜迁移 🚀"
    out = dg.build_headlines([_card(title=title)], {}, date=DATE)
    assert title in out


def test_inline_strips_the_whole_shared_class_not_just_the_two_famous_characters():
    """_inline and the collection side (run._clean_text) read the same lib.INVISIBLE_RE, so the
    renderer cannot quietly cover a narrower set than the ingester does."""
    for ch in (ZWSP, BIDI, "\u200f", "\u2066", "\ufeff", "\u0001", "\u007f"):
        assert lib.INVISIBLE_RE.search(ch), repr(ch)
        assert dg._inline("a" + ch + "b") == "ab", repr(ch)
    assert lib.strip_invisible("a" + ZWSP + "b") == "ab"


# ------------------------------------------------------------- one implementation, not two copies

def test_run_and_lib_are_the_same_function_object_not_two_copies():
    """The defect is duplication, so the assertion is identity, not equal behavior.

    Two copies that behave the same today are exactly the state the pipeline was in before: run.py
    held its own safe_url and its own invisible-character class, and they drifted, with run's copy
    ending up the weaker of the two. A behavioral test cannot see the drift the day it is
    introduced; ``is`` can.
    """
    import run as R
    assert R.safe_url is lib.safe_url
    assert R._clean_text is lib.clean_text
    for name in ("_INVISIBLE_RE", "_WS_RE", "_MAX_URL_CHARS"):
        assert not hasattr(R, name), "run.py re-declared " + name


def _trustpilot(url):
    return {"reviews": [{"stars": 1, "url": url, "date": "2026-08-28T10:00:00Z",
                         "text": "The billing page has been down for three days and support "
                                 "will not answer.", "title": "Cannot pay"}]}


def test_the_demand_parser_now_refuses_the_markup_characters_its_own_copy_accepted():
    """Driven through the real parser, not through safe_url.

    ``<`` and ``>`` end an inline span and open a tag in the markdown that gets pushed to Discord.
    run.py's private copy of safe_url had no such rule, so a Trustpilot permalink carrying them
    reached the card; the shared gate refuses it, and the ledger says ``bad_url`` rather than
    dropping it silently.
    """
    import run as R
    res = R.parse_trustpilot(_trustpilot("https://www.trustpilot.com/review/<script>x</script>"))
    assert res["signals"] == []
    assert res["skipped_reasons"]["bad_url"] == 1


def test_an_ordinary_trustpilot_permalink_still_becomes_a_signal():
    """Over-rejection control on the same entry point: the stricter gate must not eat the lane."""
    import run as R
    ok = "https://www.trustpilot.com/reviews/aaaa1111?utm_source=daily-hotspots&page=2"
    res = R.parse_trustpilot(_trustpilot(ok))
    assert len(res["signals"]) == 1
    assert res["signals"][0]["url"] == ok
    assert res["skipped_reasons"].get("bad_url", 0) == 0
