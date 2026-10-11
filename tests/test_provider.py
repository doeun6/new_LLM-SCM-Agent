import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scm.config import APIConfig
from scm.provider import (Budget, BudgetExceeded, FatalAPIError, OpenRouterProvider,
                          RecoverableCallError, strict_json, validate_schema)

SCHEMA = {"type":"object", "properties":{"q":{"type":"number"}}, "required":["q"], "additionalProperties":False}

class Response:
    status = 200
    def __init__(self, text, tier="flex"):
        self.data = {"model":"google/gemini-2.5-flash-lite", "status":"completed", "service_tier":tier,
            "id":"test-response", "output_text":text,
            "openrouter_metadata":{"endpoints":{"available":[{"selected":True,"provider":"Google AI Studio"}]}},
            "usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"cost":0.0000015}}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.data).encode()

class ProviderTests(unittest.TestCase):
    def test_duplicate_nan_bool_rejected(self):
        for raw in ('{"q":1,"q":2}', '{"q":NaN}'):
            with self.assertRaises(ValueError): strict_json(raw)
        with self.assertRaises(ValueError): validate_schema({"q":True}, SCHEMA)
        with self.assertRaises(ValueError): validate_schema({"q":float("inf")}, SCHEMA)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY":"unit-test-placeholder"})
    def test_schema_retry_accounted_and_journal_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"calls.jsonl"
            cfg = APIConfig(retry_backoff_seconds=0)
            p = OpenRouterProvider(cfg, path, Budget(1))
            with patch("scm.provider.urlopen", side_effect=[Response('{}'), Response('{"q":10}')]) as call:
                answer = p.ask("order", "test", {}, SCHEMA, {"t":31,"agent":0})
                self.assertEqual(answer, {"q":10})
                self.assertEqual(call.call_count, 2)
                self.assertTrue(p.events[0]["format_failure"])
                self.assertEqual(p.events[1]["attempt"], 1)
                self.assertAlmostEqual(p.budget.spent, 0.000003)
            restored = OpenRouterProvider(cfg, path, Budget(1))
            with patch("scm.provider.urlopen") as call:
                self.assertEqual(restored.ask("order", "test", {}, SCHEMA, {"t":31,"agent":0}), answer)
                call.assert_not_called()

    @patch.dict(os.environ, {"OPENROUTER_API_KEY":"unit-test-placeholder"})
    def test_standard_tier_halts_without_retry(self):
        p = OpenRouterProvider(APIConfig(retry_backoff_seconds=0), budget=Budget(1))
        with patch("scm.provider.urlopen", return_value=Response('{"q":10}', "standard")) as call:
            with self.assertRaises(FatalAPIError): p.ask("order", "test", {}, SCHEMA, {})
            self.assertEqual(call.call_count, 1)
            self.assertTrue(p.events[0]["fatal"])

    @patch.dict(os.environ, {"OPENROUTER_API_KEY":"unit-test-placeholder"})
    def test_semantic_failure_shares_retry_budget(self):
        p = OpenRouterProvider(APIConfig(retry_backoff_seconds=0), budget=Budget(1))
        def reject(value): raise ValueError("Wrong semantic information")
        with patch("scm.provider.urlopen", return_value=Response('{"q":10}')) as call:
            with self.assertRaises(RecoverableCallError): p.ask("message", "test", {}, SCHEMA, {}, validate=reject)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(sum(x['format_failure'] for x in p.events), 2)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY":"unit-test-placeholder"})
    def test_m3_out_of_range_plan_shares_retry_budget_then_succeeds(self):
        from scm.harness import _validate_message, plan_schema
        p = OpenRouterProvider(APIConfig(retry_backoff_seconds=0), budget=Budget(1))
        replies = [Response('{"plan_t_plus_1":101,"plan_t_plus_2":10}'),
                   Response('{"plan_t_plus_1":12,"plan_t_plus_2":10}')]
        with patch("scm.provider.urlopen", side_effect=replies) as call:
            result = p.ask("message", "test", {}, plan_schema(100), {},
                           validate=lambda v: _validate_message(v, "M3", 100))
            self.assertEqual(result, {"plan_t_plus_1": 12, "plan_t_plus_2": 10})
            self.assertEqual(call.call_count, 2)
            self.assertEqual([x["format_failure"] for x in p.events], [True, False])

    def test_budget_reservation_prevents_parallel_overspend(self):
        b = Budget(1)
        b.reserve(.7)
        with self.assertRaises(BudgetExceeded): b.reserve(.4)
        b.settle(.7,.3)
        b.reserve(.6)
        b.settle(.6,.5)
        self.assertAlmostEqual(b.spent,.8)
        for invalid in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError): Budget(invalid)
            with self.assertRaises(ValueError): b.reserve(invalid)

if __name__ == '__main__': unittest.main()
