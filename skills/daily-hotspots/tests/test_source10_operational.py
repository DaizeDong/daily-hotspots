"""Synthetic source10 contracts. All scenario payloads come from the generator."""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import archive
import lib
import push_card
import run
import score

_GENERATOR = Path(__file__).parents[3] / 'tools' / 'make_fixtures.py'
_spec = importlib.util.spec_from_file_location('source10_fixtures', _GENERATOR)
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)

class OperationalContracts(unittest.TestCase):
    def setUp(self):
        self.case = _gen.source10_scenario()
        self.cfg = copy.deepcopy(lib.DEFAULT_CONFIG)

    def test_origin_identity(self):
        evidence = self.case['card']['evidence']
        for row, origin in zip(evidence, self.case['same_origins']):
            row['origin'] = origin
        self.assertEqual(run.count_independent_sources(evidence, self.cfg), 1)
        for row, origin in zip(evidence, self.case['distinct_origins']):
            row['origin'] = origin
        self.assertEqual(run.count_independent_sources(evidence, self.cfg), 2)

    def test_scoring_rejects_nonfinite_inputs(self):
        for field in ('velocity', 'crowdedness', 'age_h', 'track_weight'):
            for value in self.case['invalid_numbers']:
                with self.subTest(field=field, kind=type(value).__name__):
                    args = {'breakdown': self.case['card']['score_breakdown'],
                            'n_sources': 2, 'age_h': 0, 'cfg': self.cfg}
                    args[field] = value
                    with self.assertRaises(ValueError):
                        score.score_opportunity(**args)

    def test_host_gate_rejects_ambiguous_authority(self):
        self.assertEqual(lib.safe_url(self.case['ambiguous_url'], ['example.com']), '')
        self.assertEqual(lib.safe_url(self.case['safe_url'], ['example.com']), self.case['safe_url'])

    def test_preview_is_not_production_delivery(self):
        with patch.dict(os.environ, {'DAILY_HOTSPOTS_DRYRUN': '1'}):
            with patch.object(push_card, '_relay_cmd', side_effect=AssertionError('no send')):
                self.assertFalse(push_card.deliver('Synthetic preview', dry_run=False)[0])
                self.assertTrue(push_card.deliver('Synthetic preview', dry_run=True)[0])

    def test_url_credentials_are_scrubbed(self):
        import redact
        result = redact.redact_egress(self.case['credential_url'])
        self.assertNotIn(self.case['token'], result['redacted'])
        self.assertIn('SECRET', result['found'])
        self.assertEqual(redact.scrub_egress(self.case['numeric_id_url']), self.case['numeric_id_url'])

    def test_demand_replay_uses_raw_dimensions(self):
        raw = self.case['card']['score_breakdown']
        result = score.score_opportunity(raw, 2, 0, cfg=self.cfg, side='demand', crowdedness=80)
        item = dict(result, id='synthetic-replay', n_sources=2, age_h=0)
        self.assertEqual(score._final_map([item], None, self.cfg)['synthetic-replay'], result['final_score'])

    def test_archive_reasserts_evidence(self):
        card = copy.deepcopy(self.case['card'])
        card.update(canonical_key='synthetic-archive', independent_source_count=2, final_score=80)
        card['evidence'] = []
        self.assertEqual(archive.archive_card(card, cfg=self.cfg, dry_run=True)[0], 'refused')


class HistoryContracts(unittest.TestCase):
    def setUp(self):
        self.case = _gen.source10_history_scenario()
        self.cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        self.cfg.setdefault('yield', {})['min_history_days'] = 0

    def test_corrupt_denominator_blocks_prune(self):
        import importlib
        engine = importlib.import_module('yield')
        result = engine.run_yield(self.case['roster'], self.case['records'], self.case['pulls'],
                                 cfg=self.cfg, denominator_status={'state': 'corrupt', 'bad_lines': 1})
        self.assertIsNotNone(result['prune_blocked_reason'])
        self.assertFalse(result['applied'])
        self.assertEqual(result['prune'], [])

    def test_audited_reader_rejects_unusable_opportunities(self):
        import importlib, json
        engine = importlib.import_module('yield')
        class FakeFile:
            def __init__(self, row): self.row = row
            def is_file(self): return True
            def read_bytes(self): return (json.dumps(self.row) + '\n').encode()
        for row in self.case['invalid_rows']:
            records, state = engine.read_jsonl_audited(FakeFile(row))
            self.assertEqual(state['state'], 'corrupt')
            self.assertEqual(records, [])
        records, state = engine.read_jsonl_audited(FakeFile(self.case['records'][0]))
        self.assertEqual(state['state'], 'ok')
        self.assertEqual(len(records), 1)

    def test_persisted_bandit_is_finite(self):
        import bandit, math
        arm = bandit.deserialize_arms(self.case['bad_arm'], self.cfg)['synthetic-track']
        self.assertTrue(math.isfinite(arm['alpha']))
        self.assertTrue(math.isfinite(arm['beta']))
        self.assertIsInstance(arm['n'], int)

    def test_singleton_read_failure_is_not_absence(self):
        import dedup
        client = object.__new__(dedup.LedgerClient)
        with patch.object(client, 'list_active', side_effect=OSError('synthetic unavailable')):
            with self.assertRaises(OSError): client.get_pulse_seen()
            with self.assertRaises(OSError): client.get_bandit_arms()


class AdapterContracts(unittest.TestCase):
    def setUp(self): self.case = _gen.source10_adapter_scenario()

    def test_roster_container_is_validated(self):
        import roster
        for raw in self.case['bad_rosters']:
            self.assertFalse(roster.validate_roster(raw)[0])
            with self.assertRaises(ValueError): roster.normalize_roster(raw)
        self.assertTrue(roster.validate_roster({'entries': []})[0])

    def test_error_status_is_not_empty_success(self):
        import collect
        self.assertIsNotNone(collect.roster_payload_status(self.case['failed_roster'])[1])
        self.assertIsNotNone(collect.community_payload_status(self.case['failed_community'])[1])
        self.assertIsNone(collect.roster_payload_status({'tweets': []})[1])
        self.assertIsNone(collect.community_payload_status({'items': []})[1])

    def test_health_collection_matches_parser_shape(self):
        import sourcehealth
        result = sourcehealth.evaluate_content(self.case['health_payload'], self.case['health_control'])
        self.assertTrue(result['empty'])

    def test_invalid_review_numeric_value_is_counted(self):
        import collect
        result = collect.parse_trustpilot(self.case['malformed_review'])
        self.assertEqual(result['signals'], [])
        self.assertEqual(result['skipped_reasons'].get('invalid_rating'), 1)

    def test_identity_provider_error_is_retried(self):
        import identity_sweep, json
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(inner): return json.dumps(self.case['identity_error']).encode()
        with patch.object(identity_sweep.urllib.request, 'urlopen', return_value=Response()) as call:
            with patch.object(identity_sweep.time, 'sleep'):
                with self.assertRaises(RuntimeError): identity_sweep.fetch_one('synth_user', 'synthetic-token', 1, retries=2)
        self.assertEqual(call.call_count, 2)


class DeliveryContracts(unittest.TestCase):
    def test_accounting_matches_rendered_columns(self):
        case = _gen.source10_delivery_scenario()
        cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        cfg['push']['max_per_day'] = 1
        with patch.object(push_card, 'deliver', return_value=(True, 'synthetic acknowledgement')) as send:
            result = run.process(case['candidates'], cfg, dry_run=True, collection={})
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['pushed']), 2, {key: result[key] for key in ('built', 'blocked', 'archived', 'pushed')})
        for title in result['pushed']:
            self.assertIn(title, send.call_args.args[0])

    def test_catch_up_reports_partial_failure(self):
        import digest
        dates = _gen.source10_delivery_scenario()['dates']
        with patch.object(digest, 'missed_digest_dates', return_value=dates):
            with patch.object(digest, 'register_digest_item', side_effect=[None, OSError('synthetic failure')]):
                with self.assertRaises(RuntimeError) as error:
                    digest.catch_up_digests(object(), None)
        self.assertEqual(error.exception.ensured, dates[:1])
        self.assertEqual(error.exception.failures[0]['date'], dates[1])


class FinalContracts(unittest.TestCase):
    def test_generated_fixtures_match_declared_bytes(self):
        for name, relative in _gen.TARGETS.items():
            actual = (Path(__file__).parents[3] / relative).read_bytes().replace(b'\r\n', b'\n')
            self.assertEqual(actual, _gen.render(name).replace(b'\r\n', b'\n'), relative)

    def test_documented_extraction_schema_feeds_parser(self):
        import collect
        request = _gen.trustpilot_request()
        schema = next(value['schema'] for value in request['formats'] if isinstance(value, dict))
        row = _gen.source10_adapter_scenario()['extracted_review']
        self.assertEqual(set(schema['properties']['reviews']['items']['required']), set(row))
        result = collect.parse_trustpilot({'success': True, 'data': {'json': {'reviews': [row]}}})
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['signals']), 1)

    def test_replay_retains_original_track_age_and_velocity(self):
        cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        raw = _gen.source10_scenario()['card']['score_breakdown']
        result = score.score_opportunity(raw, 2, 36, velocity=0.5, track_weight=1.3, cfg=cfg)
        replay = score._final_map([dict(result, id='synthetic-context')], None, cfg)
        self.assertEqual(replay['synthetic-context'], result['final_score'])

    def test_archive_read_failure_precedes_append(self):
        card = _gen.source10_scenario()['card']
        card.update(canonical_key='synthetic-archive', independent_source_count=2, final_score=80)
        with tempfile.TemporaryDirectory(prefix='synthetic-archive-') as temporary:
            base = Path(temporary) / 'archive'
            base.mkdir()
            state = base / 'dedup-state.json'
            state.write_text('{}', encoding='utf-8')
            read_text, open_file = Path.read_text, Path.open

            def read(path, *args, **kwargs):
                if path == state:
                    raise OSError('synthetic unreadable state')
                return read_text(path, *args, **kwargs)

            def open_checked(path, *args, **kwargs):
                if path == base / 'opportunities.jsonl':
                    raise AssertionError('append must not begin')
                return open_file(path, *args, **kwargs)

            with patch.object(Path, 'read_text', read), patch.object(Path, 'open', open_checked):
                with self.assertRaisesRegex(OSError, 'synthetic unreadable state'):
                    archive.archive_card(card, archive_dir=str(base), cfg=copy.deepcopy(lib.DEFAULT_CONFIG))
            self.assertFalse((base / 'opportunities.jsonl').exists())


class PublicationContracts(unittest.TestCase):
    def test_runtime_roster_is_bound_to_private_publication_repository(self):
        import private_storage, roster
        case = _gen.source10_publication_scenario()
        repo = Path(__file__).parents[3]
        def git_result(argv):
            if 'symbolic-ref' in argv: return case['local']
            if 'check-ref-format' in argv: return ''
            if 'config' in argv: return case['remote'] if argv[-1].endswith('.remote') else case['merge']
            if 'rev-parse' in argv: return case['upstream']
            if 'remote' in argv: return case['url']
            raise AssertionError('unexpected metadata request')
        with patch.object(private_storage, 'prove', side_effect=Path):
            with patch.object(private_storage, '_repository_root', return_value=repo):
                with patch.object(private_storage, '_run', side_effect=git_result):
                    with patch.object(private_storage, '_prove_repository'):
                        with patch.object(roster, 'resolve_config_roster_path', return_value=repo / case['roster_name']):
                            result = private_storage.publication_target(repo)
                            self.assertEqual(result['roster_pathspec'], case['roster_name'])
                            self.assertEqual(result['repository_root'], str(repo))
                        with patch.object(roster, 'resolve_config_roster_path', return_value=repo.parent / case['roster_name']):
                            with self.assertRaises(RuntimeError): private_storage.publication_target(repo)

if __name__ == '__main__':
    unittest.main()
