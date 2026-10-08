"""Configuration and DATA must select one companion before any runtime write."""
from pathlib import Path

import pytest
import lib
import roster
import archive


@pytest.mark.parametrize("alias", [False, True])
def test_settings_and_runtime_share_explicit_selection(tmp_path, monkeypatch, alias):
    selected = tmp_path / "selected"
    selected.mkdir()
    for key in ("DAILY_HOTSPOTS_CONFIG", "DAILY_HOTSPOTS_CONFIG_DIR", "DAILY_HOTSPOTS_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DAILY_HOTSPOTS_CONFIG_DIR" if alias else "DAILY_HOTSPOTS_CONFIG", str(selected))
    assert lib.find_config_dir() == selected
    assert roster.find_roster_path() == selected / "roster.json"
    assert archive.find_archive_dir() == selected / "archive"


def test_data_override_cannot_mix_two_companions(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("DAILY_HOTSPOTS_CONFIG", str(first))
    monkeypatch.setenv("DAILY_HOTSPOTS_DATA_DIR", str(second))
    with pytest.raises((ValueError, RuntimeError), match="same companion|conflict"):
        lib.find_config_dir()
    assert roster.resolve_config_roster_path(first) == first / "roster.json"


def test_explicit_missing_config_never_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_HOTSPOTS_CONFIG", str(tmp_path / "missing"))
    with pytest.raises((ValueError, RuntimeError)):
        lib.find_config_dir()
