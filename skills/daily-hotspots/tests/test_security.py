"""T8 secret-safety: no hardcoded keys/tokens anywhere in the skill repo tree."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # daily-hotspots/

# Discord bot token, OpenAI/Anthropic style, AWS, generic 32+ hex, bearer with long value
PATTERNS = [
    re.compile(r"[MN][A-Za-z0-9_-]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),  # discord bot token
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-]{30,}"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
TEXT_EXT = {".py", ".md", ".json", ".txt", ".ps1", ".cmd", ".sh", ".jsonc", ".template", ".env"}


def _files():
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in TEXT_EXT or p.name.endswith(".template"):
            yield p


def test_no_hardcoded_secrets():
    hits = []
    for p in _files():
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pat in PATTERNS:
            for m in pat.finditer(txt):
                # the regex literals inside THIS test file are not secrets
                if p.name == "test_security.py":
                    continue
                hits.append((str(p.relative_to(REPO)), m.group(0)[:12] + "..."))
    assert not hits, f"possible secrets found: {hits}"


def test_push_card_never_reads_token():
    src = (REPO / "skills/daily-hotspots/scripts/push_card.py").read_text(encoding="utf-8")
    # the relay owns the token; this module must not load config.json / bot_token
    assert "bot_token" not in src
    assert "config.json" not in src


# --------------------------------------------------------------------------- audit HIGH#2 (revised 2026-07-13)
def _wrapper_prompt_block(src: str) -> str:
    """The literal prompt text the wrapper hands to the orchestration transport.

    Sliced out on purpose rather than searching the whole file: the injection defense has to reach
    the AGENT, and a defense that only exists in a PowerShell comment reaches nobody. Anchoring on
    the assignment is what makes the assertions below able to fail when the sentence is moved out of
    the prompt and into prose.
    """
    i = src.index("$prompt =")
    tail = src[i:]
    out = []
    for line in tail.splitlines():
        if out and line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


def test_scheduled_wrapper_prompt_block_is_locatable():
    """Negative control for the slicer itself.

    If `_wrapper_prompt_block` silently returned "" (assignment renamed, file restructured), every
    assertion in the posture test below would pass vacuously on an empty string that contains no
    forbidden thing either. "clean" and "found nothing to check" must not look the same, so the
    slice is asserted to be non-trivial before anything is asserted about its contents.
    """
    src = (REPO / "skills/daily-hotspots/scripts/wrapper.ps1").read_text(encoding="utf-8")
    block = _wrapper_prompt_block(src)
    assert len(block) > 200, "prompt block slice came back trivial; the slicer no longer finds the prompt"
    assert "run.py" in block, "prompt block slice does not look like the orchestration prompt"


def test_scheduled_wrapper_permission_posture_is_deliberate():
    """Permission posture of the cron wrapper (revised 2026-08-27 for the transport rewrite).

    History: an earlier revision passed an explicit MCP+`Bash(python:*)` allow-list to avoid a
    blanket permission skip on this untrusted-ingest run. That allow-list OMITTED the tools the
    SKILL needs to orchestrate (Skill/Agent/WebSearch/WebFetch per SKILL.md allowed-tools), so the
    headless agent could not run and collected NOTHING (rc=0, empty archive). A partial allow-list
    is a footgun here: too narrow => the skill can't run; wide enough to run => it already grants
    Skill/Agent, at which point scoping Bash buys little.

    The wrapper no longer invokes an agent CLI itself. It hands the day's prompt to llmcall
    (mode="agent") or to the agent-runner adapter, and THOSE own the permission flags, so this
    wrapper contains no `--dangerously-skip-permissions` and no `--allowedTools` to assert on. The
    old string check therefore went red on a rewrite that changed nothing about the risk.

    What still has to hold, and is still checkable HERE, is the part this file owns:
      * the posture is a written decision, not an omission; and
      * whatever the transport's flags are, the prompt itself carries the untrusted-data defense.
    The second one is the load-bearing assertion, and it is scoped to the prompt block so it cannot
    be satisfied by a comment.
    """
    src = (REPO / "skills/daily-hotspots/scripts/wrapper.ps1").read_text(encoding="utf-8")
    uses_skip = "--dangerously-skip-permissions" in src
    uses_allowlist = "--allowedTools" in src or "--allowed-tools" in src

    if uses_allowlist and not uses_skip:
        # An allow-list must be complete enough to actually run the skill (mirror SKILL.md
        # allowed-tools); a partial allow-list silently no-ops the run.
        for needed in ("Skill", "Agent"):
            assert needed in src, f"allow-list must include {needed} or the skill orchestration can't start"
    else:
        # Either an explicit skip, or delegation to a transport that runs with permissions skipped.
        # Both are the same risk and carry the same obligation: the decision is written down.
        assert "SECURITY posture" in src,             "wrapper must state its permission posture deliberately (skip, allow-list, or delegated)"
        assert "permissions skipped" in src or uses_skip,             "wrapper must say what posture the run actually gets"

    # The in-prompt defense is required under EVERY posture above, and must live in the prompt the
    # transport receives.
    prompt = _wrapper_prompt_block(src).lower()
    assert "untrusted" in prompt, "the orchestration prompt must mark collected content as untrusted"
    assert "never obey" in prompt or "never as instructions" in prompt,         "the orchestration prompt must instruct the agent to never obey embedded instructions"


# --------------------------------------------------------------------------- audit LOW#2
import subprocess


def _git_ignored(path: str) -> bool:
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path],
                       capture_output=True)
    return r.returncode == 0


def test_public_repo_gitignore_defensive_secret_patterns():
    """This public skill repo ships no secrets, but the README guides users to configure env/
    watchlist. A stray local credential or tuned config must be gitignored so it can't be committed
    by accident. (audit LOW#2 regression guard)"""
    if not (REPO / ".git").exists():
        import pytest
        pytest.skip("not a git checkout")
    for p in (".env", ".credentials.json", "secrets.json", "secrets/x.json",
              "foo.local.json", "watchlist.json"):
        assert _git_ignored(p), f"{p} is NOT gitignored in the public skill repo"
