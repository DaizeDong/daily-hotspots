"""Source11 boundary regressions using generator-owned fictional scenarios."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import collect
import digest
import lib
import push_card

_spec = importlib.util.spec_from_file_location(
    "source11_fixtures", Path(__file__).parents[3] / "tools" / "make_fixtures.py")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)

class Source11Contracts(unittest.TestCase):
    def setUp(self):
        self.case = _gen.source11_scenario()
        self.cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
        self.cfg["sources"] = {}
        self.now = lib.parse_ts(self.case["now"])

    def relay_capture(self, message):
        captured = []
        def send(args, **kwargs):
            captured.append(args[-1])
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"DAILY_HOTSPOTS_DRYRUN": "0"}), \
             patch.object(push_card, "_relay_cmd", return_value=["synthetic-relay"]), \
             patch.object(push_card.subprocess, "run", side_effect=send):
            self.assertTrue(push_card.deliver(message)[0])
        self.assertTrue(captured)
        return "\n".join(captured)

    def test_encoded_addresses_removed_at_relay(self):
        for url in self.case["address_urls"]:
            with self.subTest(url=url):
                delivered = self.relay_capture("<" + url + ">")
                decoded = delivered
                while unquote(decoded) != decoded:
                    decoded = unquote(decoded)
                self.assertNotIn(self.case["address"], decoded)

    def test_clean_citations_and_delimiters_survive(self):
        for citation in (self.case["clean_commit"], self.case["clean_citation"]):
            with self.subTest(citation=citation):
                self.assertEqual(self.relay_capture(citation), citation)

    def test_credential_urls_still_removed(self):
        delivered = self.relay_capture(self.case["credential_url"])
        self.assertNotIn(self.case["credential_value"], delivered)

    def run_cli(self, value):
        calls = []
        def send(args, **kwargs):
            calls.append(args[-1])
            return SimpleNamespace(returncode=0)
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(self.case["card"]).encode()))
        with patch.dict(os.environ, {"DAILY_HOTSPOTS_DRYRUN": value}), \
             patch.object(push_card.sys, "stdin", stdin), \
             patch.object(push_card.sys, "stdout", io.StringIO()), \
             patch.object(push_card, "_relay_cmd", return_value=["synthetic-relay"]), \
             patch.object(push_card.subprocess, "run", side_effect=send):
            status = push_card.main()
        return status, calls

    def test_cli_false_preview_tokens_deliver(self):
        for token in self.case["preview_false"]:
            with self.subTest(token=token):
                status, calls = self.run_cli(token)
                self.assertEqual(status, 0)
                self.assertTrue(calls)

    def test_cli_true_preview_tokens_do_not_deliver(self):
        for token in self.case["preview_true"]:
            with self.subTest(token=token):
                status, calls = self.run_cli(token)
                self.assertEqual(status, 0)
                self.assertEqual(calls, [])

    def test_cli_invalid_preview_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_cli(self.case["preview_invalid"])

    def test_empty_headlines_keep_full_digest_link(self):
        message = digest.build_headlines([], date=self.case["now"][:10],
                                         digest_url=self.case["digest_url"])
        self.assertIn(self.case["digest_url"], message)
        self.assertIn("今日无合格机会", message)
        self.assertNotIn("javascript:", digest.build_headlines(
            [], date=self.case["now"][:10], digest_url="javascript:invalid"))

    def test_bad_normalizers_keep_compatibility_and_failure_status(self):
        parsers = [(collect.parse_rss, raw) for raw in self.case["bad_rss"]]
        parsers += [(collect.parse_v2ex, raw) for raw in self.case["bad_v2ex"]]
        for parser, raw in parsers:
            with self.subTest(parser=parser.__name__, raw=raw):
                items = parser(raw)
                self.assertEqual(items, [])
                result = collect.collect_sources(
                    community={"failed-lane": items,
                               "healthy-lane": self.case["normalized_healthy"]},
                    cfg=self.cfg, now=self.now)
                self.assertEqual(len(result["signals"]), 1)
                failed = next(p for p in result["pulls"] if p["source"] == "failed-lane")
                self.assertFalse(failed["observed"])
                self.assertTrue(failed["error"])
                payload = json.loads(json.dumps(items.as_payload()))
                self.assertTrue(collect.community_payload_status(payload)[1])

    def test_valid_empty_normalizers_remain_successful(self):
        for items in (collect.parse_rss(self.case["empty_rss"]), collect.parse_v2ex([])):
            with self.subTest(items=items):
                rows, error = collect.community_payload_status(items)
                self.assertEqual(rows, [])
                self.assertIsNone(error)
                result = collect.collect_community_source(
                    "synthetic-empty", items, cfg=self.cfg, now=self.now)
                self.assertIsNone(result["filtered"]["error"])
                self.assertNotIn("observed", result["pulls"][0])

    def test_malformed_title_is_counted_and_siblings_survive(self):
        for title in self.case["bad_titles"]:
            raw = copy.deepcopy(self.case["v2ex"])
            raw[0]["title"] = title
            result = collect.collect_sources(
                community={"v2ex": collect.parse_v2ex(raw),
                           "healthy-lane": self.case["normalized_healthy"]},
                cfg=self.cfg, now=self.now)
            self.assertEqual(len(result["signals"]), 1)
            self.assertEqual(result["signals"][0]["source"], "healthy-lane")
            self.assertEqual(result["filtered"]["v2ex"]["dropped_malformed"], 1)

    def test_lane_exception_is_contained(self):
        original = collect.collect_community_source
        def one_failed(source, *args, **kwargs):
            if source == "failed-lane":
                raise TypeError("synthetic malformed record")
            return original(source, *args, **kwargs)
        with patch.object(collect, "collect_community_source", side_effect=one_failed):
            result = collect.collect_sources(
                community={"failed-lane": [], "healthy-lane": self.case["normalized_healthy"]},
                cfg=self.cfg, now=self.now)
        self.assertEqual(len(result["signals"]), 1)
        self.assertTrue(result["filtered"]["failed-lane"]["error"])
        self.assertFalse(result["pulls"][0]["observed"])

    def test_invalid_attempt_metadata_does_not_abort_healthy_siblings(self):
        result = collect.collect_sources(
            community={"failed-lane": {"items": self.case["normalized_healthy"],
                                        "attempts": self.case["invalid_attempts"]},
                       "healthy-lane": self.case["normalized_healthy"]},
            cfg=self.cfg, now=self.now)
        self.assertEqual(len(result["signals"]), 1)
        self.assertEqual(result["signals"][0]["source"], "healthy-lane")
        self.assertIn("invalid attempt metadata", result["filtered"]["failed-lane"]["error"])
        self.assertFalse(result["pulls"][0]["observed"])
        self.assertEqual(result["pulls"][0]["attempts"], 1)

    def test_explicit_demand_failure_flags_do_not_count_as_observations(self):
        for lane, healthy in self.case["demand"].items():
            for flag, value in (("ok", False), ("isError", True), ("success", False)):
                with self.subTest(lane=lane, flag=flag):
                    bad = copy.deepcopy(healthy)
                    bad[flag] = value
                    result = collect.collect_new_source(lane, bad, now=self.now)
                    self.assertEqual(result["signals"], [])
                    self.assertFalse(result["pulls"][0]["observed"])
                    self.assertTrue(result["filtered"]["error"])

    def test_healthy_demand_responses_preserve_populated_and_empty_observations(self):
        for lane, healthy in self.case["demand"].items():
            with self.subTest(lane=lane):
                full = collect.collect_new_source(lane, healthy, now=self.now)
                self.assertEqual(len(full["signals"]), 1)
                self.assertIsNone(full["filtered"]["error"])
                empty = copy.deepcopy(healthy)
                empty["results"] = []
                result = collect.collect_new_source(lane, empty, now=self.now)
                self.assertEqual(result["signals"], [])
                self.assertIsNone(result["filtered"]["error"])
                self.assertNotIn("observed", result["pulls"][0])

    def test_ancillary_secret_scan_reports_scope_and_hits(self):
        spec = importlib.util.spec_from_file_location(
            "source11_security", Path(__file__).with_name("test_security.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = Path("synthetic-module.py")
        for text, expected in ((self.case["security_clean"], 0),
                               (self.case["security_detected"], 1)):
            with patch.object(Path, "read_text", return_value=text):
                report = module.scan_for_secrets([target], Path("."))
            self.assertEqual(report["examined"], 1)
            self.assertEqual(len(report["hits"]), expected)
            self.assertNotIn(self.case["security_detected"], repr(report))

    def test_ancillary_secret_scan_fails_for_unexamined_targets(self):
        spec = importlib.util.spec_from_file_location(
            "source11_security_failure", Path(__file__).with_name("test_security.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaises(ValueError):
            module.scan_for_secrets([], Path("."))
        with patch.object(Path, "read_text", side_effect=OSError("synthetic unreadable")), \
             self.assertRaises(OSError):
            module.scan_for_secrets([Path("synthetic-module.py")], Path("."))
        def broken_walk(root, onerror):
            onerror(OSError("synthetic enumeration failure"))
            return iter(())
        with patch.object(module.os, "walk", side_effect=broken_walk), self.assertRaises(OSError):
            list(module._files())

    def test_generated_scenarios_are_reproducible_and_independent(self):
        for factory in (_gen.source11_scenario, _gen.source11_story_scenarios,
                        _gen.source11_demand_parity_cases, _gen.source11_temporal_schedule,
                        _gen.source11_temporal_history):
            with self.subTest(factory=factory.__name__):
                first, second = factory(), factory()
                self.assertEqual(first, second)
                self.assertIsNot(first, second)
        first = _gen.source11_story_scenarios()
        first[0]["title"] = "changed only in this caller"
        self.assertNotEqual(first, _gen.source11_story_scenarios())
        first = _gen.source11_temporal_schedule()
        first["days"].clear()
        self.assertEqual(len(_gen.source11_temporal_schedule()["days"]), 49)

if __name__ == "__main__":
    unittest.main()
