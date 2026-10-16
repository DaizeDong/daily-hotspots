"""Bind shared companion discovery to this skill's supported runtime layout."""
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = "daily-hotspots"


@lru_cache(maxsize=1)
def resolver():
    try:
        from fleet_guards.runtime import companion_resolver
    except ImportError as exc:
        raise RuntimeError(
            'fleet_guards is not installed; install it: pip install -e "$(git config --get fleet.guards)"'
        ) from exc
    return companion_resolver(source_root=ROOT)


def _directory(value, label):
    if not value or not str(value).strip():
        raise ValueError(label + " is empty")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(label + " is not an existing config directory")
    resolver().assert_outside_own_repo(path, SKILL)
    return path


def companion_root(override=None):
    """An explicit CLI path, else the shared resolver's companion root (or None)."""
    if override is not None:
        return _directory(override, "explicit companion")
    root = resolver().resolve_companion_root(SKILL)
    return None if root is None else Path(root).resolve()


def data_directory(override=None):
    """The resolver's DATA dir, or for an explicit companion its data/ child when present, else its root."""
    if override is None:
        found = resolver().resolve_data_dir(SKILL)
        return None if found is None else Path(found).resolve()
    root = companion_root(override)
    return root / "data" if (root / "data").is_dir() else root


def storage_mode(root):
    """Read the declared credential policy from secrets/README.md; a missing declaration is an error."""
    import re
    path = Path(root) / "secrets/README.md"
    if not path.is_file():
        raise ValueError("declare Active storage mode: A or B in secrets/README.md")
    text = path.read_text(encoding="utf-8-sig")
    declarations = re.findall(r"^\s*Active storage mode:\s*(.*)$", text, re.I | re.M)
    if not declarations:
        raise ValueError("declare Active storage mode: A or B in secrets/README.md")
    modes = set()
    for declaration in declarations:
        match = re.match(r"(?:Mode\s+)?([AB])\b", declaration.strip(" *`"), re.I)
        if not match:
            raise ValueError("declare Active storage mode: A or B in secrets/README.md")
        modes.add(match.group(1).upper())
    if len(modes) != 1:
        raise ValueError("conflicting credential storage modes")
    return modes.pop()
