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
