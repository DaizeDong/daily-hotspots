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


# --------------------------------------------------------------------------- audit HIGH#2
def test_scheduled_wrapper_no_blanket_skip_permissions():
    """The cron wrapper ingests UNTRUSTED web content; it must not run the headless agent with
    blanket --dangerously-skip-permissions (prompt-injection -> unrestricted Bash -> RCE). It must
    instead pass an explicit allow-list with Bash SCOPED (never a bare unrestricted Bash) and no
    web-fetch pivot tool. (audit HIGH#2 regression guard)"""
    src = (REPO / "skills/daily-hotspots/scripts/wrapper.ps1").read_text(encoding="utf-8")
    assert "--dangerously-skip-permissions" not in src, "blanket permission skip on untrusted-ingest run"
    assert "--allowedTools" in src or "--allowed-tools" in src, "must pass an explicit tool allow-list"
    # Bash, if granted at all, must be scoped — `Bash(...)`, never a bare `"Bash"` token
    import re as _re
    assert not _re.search(r'"Bash"', src), "bare unrestricted Bash must not be in the allow-list"
    assert "Bash(python" in src, "Bash must be scoped to the python interpreter"
    # no broad web-fetch/exec pivots in the scheduled allow-list
    for forbidden in ("WebFetch", "WebSearch"):
        assert forbidden not in src, f"{forbidden} must not be in the scheduled allow-list"


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
