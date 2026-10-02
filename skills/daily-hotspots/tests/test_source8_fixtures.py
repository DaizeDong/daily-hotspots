"""The planner sample is reproducible synthetic data, never an observed roster."""
import json
from pathlib import Path

import roster
from test_source7_repairs import fixtures

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = 'skills/daily-hotspots/tests/fixtures/roster.sample.json'


def test_sample_builder_covers_the_planner_without_real_accounts(tmp_path):
    assert 'roster.sample.json' in fixtures.BUILDERS
    blob = fixtures.render('roster.sample.json')
    (tmp_path / 'roster.sample.json').write_bytes(blob)
    sample = json.loads(blob)
    assert len(sample['entries']) == 49
    assert roster.validate_roster(sample) == (True, [])
    assert all(entry['handle'].startswith('synth_') for entry in sample['entries'])
    assert len({entry['track'] for entry in sample['entries']}) == 6


def test_sample_is_declared_and_byte_identical_to_generator():
    declaration = json.loads((ROOT / '.dataclass.json').read_text(encoding='utf-8'))
    assert SAMPLE in declaration['fixture']
    assert fixtures.TARGETS['roster.sample.json'] == SAMPLE
    assert (ROOT / SAMPLE).read_bytes() == fixtures.render('roster.sample.json')


def test_all_declared_fixtures_are_reproduced_in_scratch(tmp_path):
    declaration = json.loads((ROOT / '.dataclass.json').read_text(encoding='utf-8'))
    assert set(declaration['fixture']) == set(fixtures.TARGETS.values())
    assert fixtures.main(['--out', str(tmp_path)]) == 0
    for name, relative in fixtures.TARGETS.items():
        assert (tmp_path / name).read_bytes() == (ROOT / relative).read_bytes(), relative
