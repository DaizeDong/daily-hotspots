"""Run the production preview entry point with every discovery source disabled."""
from pathlib import Path
import os
import runpy
import sys

scripts = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
import archive
import lib

os.environ.pop('DAILY_HOTSPOTS_CONFIG', None)
os.environ.pop('DAILY_HOTSPOTS_DATA', None)
lib.CONFIG_FALLBACKS = ()
archive._datadir()._candidates = lambda skill: []
assert lib.find_config_dir() is None
assert archive.find_archive_dir() is None
sys.argv = [str(scripts / 'run.py'), '--dry-run', '--no-ledger']
runpy.run_path(sys.argv[0], run_name='__main__')
