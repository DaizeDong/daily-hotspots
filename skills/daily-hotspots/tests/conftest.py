import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Freeze the clock for every test so freshness/age/timestamps are deterministic.
os.environ.setdefault("DAILY_HOTSPOTS_NOW", "2026-06-25T12:00:00Z")
