"""Offline tests for evaluation arithmetic and registered paired-seed analysis."""

from copy import deepcopy
import math
import unittest

from scm.analysis import (
    LEGACY_PILOT_SOURCE_SHA256, analyze, holm_adjust, make_report,
    paired_bootstrap_interval, paired_sign_flip,
)
from scm.config import ROLES, SimulationConfig
from scm.metrics import summarize


def sample_rows(periods=2):
    rows = []
    for offset in range(periods):
        for agent, role in enumerate(ROLES):
            q = 1 + 2 * offset
            row = {
                "t": 31 + offset, "agent": agent, "role": role,
                "D": 10 if offset == 0 else 20, "A": 10, "X": 8,
                "I": 2, "B": 3, "P_pre": 10, "P_post": 10 + q,
                "Q_base": 2, "Q_final": q,
                "holding_cost": 2, "backlog_cost": 3,
                "variable_order_cost": q, "fixed_order_cost": 1,
                "total_cost": 6 + q, "immediate_filled": 8 if offset == 0 else 10,
                "decision_latency_ms": 100 * (offset + 1),
                "fallback_count": 0, "correction_count": 0,
                "forecast_fallback": False, "order_fallback": False, "message_fallback": False,
                "selected_method": "linear_regression",
            }
            rows.append(row)
    return rows


def event(task="order", attempt=0, **changes):
    data = {
        "t": 31, "agent": 0, "task": task, "attempt": attempt,
        "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
        "reported_cost_usd": 0.01, "reasoning_tokens": 0,
        "latency_seconds": 0.5, "success": True, "format_failure": False,
    }
    data.update(changes)
    return data


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.config = SimulationConfig(evaluation_periods=2)

    def test_four_cost_terms_and_period_not_agent_denominator(self):
        result = summarize(sample_rows(), [], self.config)
        self.assertEqual(result["total_cost"], 64)
        self.assertEqual(result["cost_per_period"], 32)
        self.assertEqual(result["periods_evaluated"], 2)
        self.assertEqual(result["agent_periods"], 8)
        self.assertEqual(result["cost_components"], {
            "holding_cost": 16, "backlog_cost": 24, "variable_order_cost": 16, "fixed_order_cost": 8,
        })

    def test_weighted_immediate_fill_excludes_old_backlog_shipments(self):
        result = summarize(sample_rows(), [], self.config)
        self.assertAlmostEqual(result["retailer_immediate_fill_rate"], 18 / 30)
        self.assertNotAlmostEqual(result["retailer_immediate_fill_rate"], (8 / 10 + 10 / 20) / 2)

    def test_cv_sample_standard_deviation_and_four_stage_product(self):
        result = summarize(sample_rows(), [], self.config)
        for value in result["order_cv_i"].values():
            self.assertAlmostEqual(value, math.sqrt(2) / 2)
        self.assertAlmostEqual(result["global_bullwhip"], 0.25)

    def test_undefined_cv_never_uses_epsilon_or_zero_imputation(self):
        rows = sample_rows()
        for row in rows:
            if row["role"] == "factory":
                row.update(Q_final=0, variable_order_cost=0, fixed_order_cost=0, total_cost=5)
        result = summarize(rows, [], self.config)
        self.assertIsNone(result["order_cv_i"]["factory"])
        self.assertIsNone(result["global_bullwhip"])
        singleton = summarize(sample_rows(1), [], SimulationConfig(evaluation_periods=1))
        self.assertIsNone(singleton["global_bullwhip"])

    def test_constant_nonzero_orders_have_zero_cv(self):
        rows = sample_rows()
        for row in rows:
            row.update(Q_final=1, variable_order_cost=1, total_cost=7)
        self.assertEqual(summarize(rows, [], self.config)["global_bullwhip"], 0)

    def test_complete_unique_period_grid_required(self):
        rows = sample_rows()
        for invalid in (rows[:-1], rows + [rows[0]], rows + [dict(rows[0], t=33)]):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(ValueError):
                    summarize(invalid, [], self.config)

    def test_warmup_ignored_without_changing_denominator(self):
        rows = sample_rows()
        rows.append({"t": 30})
        result = summarize(rows, [{"t": 30}], self.config)
        self.assertEqual(result["cost_per_period"], 32)
        self.assertEqual(result["api_calls"], 0)

    def test_component_and_total_consistency(self):
        for changes in ({"total_cost": 99}, {"holding_cost": 99}, {"Q_final": -1}, {"I": math.nan}):
            rows = sample_rows()
            rows[0].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    summarize(rows, [], self.config)

    def test_physical_attempts_count_failures_retries_and_every_task(self):
        events = [
            event("forecast_select"),
            event("forecast_compute", success=False, format_failure=True, error_type="ValueError"),
            event("forecast_compute", attempt=1),
            event("order"), event("message"),
        ]
        result = summarize(sample_rows(), events, self.config)
        self.assertEqual(result["api_calls"], 5)
        self.assertEqual(result["input_tokens"], 500)
        self.assertEqual(result["output_tokens"], 50)
        self.assertEqual(result["total_tokens"], 550)
        self.assertAlmostEqual(result["reported_cost_usd"], .05)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["format_failure_count"], 1)
        self.assertEqual(result["failed_attempt_count"], 1)
        self.assertEqual(result["usage_by_task"]["forecast_compute"]["api_calls"], 2)
        self.assertEqual(result["retry_decision_rate"], 1 / 8)

    def test_missing_usage_retains_known_sum_and_missing_coverage(self):
        events = [event(), event(attempt=1, input_tokens=None, total_tokens=None, reported_cost_usd=None)]
        result = summarize(sample_rows(), events, self.config)
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertIsNone(result["reported_cost_usd"])
        self.assertEqual(result["known_input_tokens_sum"], 100)
        self.assertEqual(result["known_reported_cost_usd_sum"], .01)
        self.assertEqual(result["reported_cost_usd_availability"]["missing_calls"], 1)
        self.assertEqual(result["reported_cost_usd_availability"]["fraction"], .5)
        self.assertEqual(result["output_tokens"], 20)

    def test_duplicate_unknown_and_out_of_window_api_events_rejected(self):
        for events in ([event(), event()], [event(agent=5)], [event(t=33)]):
            with self.subTest(events=events):
                with self.assertRaises(ValueError):
                    summarize(sample_rows(), events, self.config)

    def test_stage_fallback_corrections_latency_and_order_adjustment(self):
        rows = sample_rows()
        rows[0].update(forecast_fallback=True, message_fallback=True, fallback_count=2, correction_count=3)
        result = summarize(rows, [], self.config)
        self.assertEqual(result["fallback_count"], 2)
        self.assertEqual(result["fallback_decisions"], 1)
        self.assertEqual(result["fallback_by_stage"]["forecast"], 1)
        self.assertEqual(result["fallback_by_stage"]["message"], 1)
        self.assertEqual(result["correction_count"], 3)
        self.assertEqual(result["decision_latency_ms"]["mean"], 150)
        self.assertEqual(result["mean_absolute_order_adjustment"], 1)

    def test_replayed_decision_latency_is_missing_not_zero(self):
        rows = sample_rows()
        rows[0].update(latency_replayed=True, decision_latency_ms=None, arithmetic_error=True, arithmetic_error_count=2)
        result = summarize(rows, [], self.config)
        self.assertEqual(result["replayed_decisions"], 1)
        self.assertEqual(result["decision_latency_ms"]["observed_decisions"], 7)
        self.assertAlmostEqual(result["decision_latency_ms"]["mean"], 1100 / 7)
        self.assertEqual(result["arithmetic_error_count"], 2)
        self.assertEqual(result["arithmetic_error_decisions"], 1)

    def test_selected_method_is_preserved_separately_from_fallback_method(self):
        rows = sample_rows()
        rows[0].update(llm_selected_method="ses", selected_method="linear_regression", forecast_fallback=True, fallback_count=1)
        result = summarize(rows, [], self.config)
        self.assertEqual(result["llm_selection_counts"], {"ses": 1})
        self.assertEqual(result["forecast_method_counts"], {"linear_regression": 8})


def pilot_results():
    results = []
    for condition, delta in (("A", 0), ("C0M0T2", -10), ("C3M1T2", 0), ("C3M1T4", 10)):
        for seed in range(1, 9):
            cost = 100 + seed + delta
            results.append({
                "condition": condition, "seed": seed, "objective": "cost",
                "metrics": {
                    "status": "completed", "periods_evaluated": 100, "agent_periods": 400,
                    "cost_per_period": cost, "total_cost": cost * 100,
                    "retailer_immediate_fill_rate": .8, "global_bullwhip": .1,
                    "order_cv_i": {role: .5 for role in ROLES},
                    "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                    "reported_cost_usd": .01, "api_calls": 4,
                    "retry_count": 0, "format_failure_count": 0, "fallback_count": 0,
                    "decision_latency_ms": {"mean": 20},
                },
            })
    return results


class AnalysisTests(unittest.TestCase):
    def test_exact_paired_sign_flip_known_extremes_and_null(self):
        result = paired_sign_flip([1] * 8)
        self.assertEqual(result["sign_patterns"], 256)
        self.assertEqual(result["extreme_patterns"], 2)
        self.assertEqual(result["p_value"], 2 / 256)
        self.assertEqual(paired_sign_flip([-1] * 8)["p_value"], result["p_value"])
        self.assertEqual(paired_sign_flip([0] * 8)["p_value"], 1)
        self.assertEqual(paired_sign_flip([-1, 1])["p_value"], 1)

    def test_holm_three_comparisons(self):
        self.assertEqual(holm_adjust({"a": .01, "b": .04, "c": .03}), {"a": .03, "b": .06, "c": .06})

    def test_bootstrap_pairing_constant_effect_and_fixed_seed(self):
        result = paired_bootstrap_interval([-10] * 8, resamples=100)
        self.assertEqual((result["low"], result["high"]), (-10, -10))
        a = paired_bootstrap_interval(range(8), resamples=100)
        self.assertEqual(a, paired_bootstrap_interval(range(8), resamples=100))

    def test_registered_comparisons_include_positive_null_and_adverse(self):
        result = analyze(pilot_results())
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_executions"], 32)
        self.assertEqual([item["n_pairs"] for item in result["primary_contrasts"]], [8, 8, 8])
        comparisons = {item["condition"]: item for item in result["primary_contrasts"]}
        self.assertEqual(comparisons["C0M0T2"]["mean_difference"], -10)
        self.assertEqual(comparisons["C3M1T2"]["p_value_holm"], 1)
        self.assertEqual(comparisons["C3M1T4"]["mean_difference"], 10)
        self.assertEqual(comparisons["C0M0T2"]["p_value_holm"], 3 * 2 / 256)
        report = make_report(result)
        for condition in ("A", "C0M0T2", "C3M1T2", "C3M1T4"):
            self.assertIn(condition, report)
        self.assertIn("100개 평가 기간은 독립 반복 표본이 아닙니다", report)

    def test_one_missing_cell_withholds_all_three_tests(self):
        result = analyze(pilot_results()[:-1])
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_executions"], [{"condition": "C3M1T4", "seed": 8}])
        for comparison in result["primary_contrasts"]:
            self.assertIsNone(comparison["p_value"])
            self.assertIsNone(comparison["p_value_holm"])
            self.assertIsNone(comparison["bootstrap_95_ci"])
        self.assertIn("미완료", make_report(result))

    def test_duplicates_unregistered_seeds_and_short_runs_rejected(self):
        results = pilot_results()
        duplicate = results + [deepcopy(results[0])]
        seed_changed = deepcopy(results)
        seed_changed[0]["seed"] = 9
        short = deepcopy(results)
        short[0]["metrics"]["periods_evaluated"] = 99
        for invalid in (duplicate, seed_changed, short):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(ValueError):
                    analyze(invalid)

    def test_actual_api_provenance_matches_source_globally_and_paths_per_seed(self):
        results = pilot_results()
        for item in results:
            item.update(backend="openrouter", source_hash=LEGACY_PILOT_SOURCE_SHA256, warmup_hash=f"warmup_{item['seed']}", demand_hash=f"demand_{item['seed']}")
        data = analyze(results)
        self.assertTrue(data["provenance"]["verified_actual_api"])
        self.assertEqual(data["provenance"]["source_hash"], LEGACY_PILOT_SOURCE_SHA256)
        self.assertEqual(data["required_actual_api_source_hash"], LEGACY_PILOT_SOURCE_SHA256)
        self.assertEqual(data["reference_definition"]["factorial_condition"], "C0M0T0")
        self.assertFalse(data["reference_definition"]["direct_order"])
        self.assertIn("현재의 C0·당기 수주만으로 직접 주문하는 A의 결과가 아닙니다", make_report(data))
        self.assertEqual(len(data["provenance"]["paired_seed_hashes"]), 8)
        for changes in ({"source_hash": "different"}, {"warmup_hash": "different"}, {"demand_hash": "different"}, {"demand_hash": None}, {"backend": "mock"}):
            invalid = deepcopy(results)
            invalid[0].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    analyze(invalid)

    def test_uniform_new_source_is_not_mislabelled_as_the_legacy_A_pilot(self):
        # Matching source hashes within a batch are insufficient: new A has a
        # different experimental treatment even if all 32 labels/seeds match.
        results = pilot_results()
        for item in results:
            item.update(backend="openrouter", source_hash="new_direct_order_A_source", warmup_hash=f"warmup_{item['seed']}", demand_hash=f"demand_{item['seed']}")
        with self.assertRaisesRegex(ValueError, "current direct-order A"):
            analyze(results)

    def test_legacy_api_objective_must_be_explicit_and_cost(self):
        results = pilot_results()
        for item in results:
            item.update(backend="openrouter", source_hash=LEGACY_PILOT_SOURCE_SHA256, warmup_hash=f"warmup_{item['seed']}", demand_hash=f"demand_{item['seed']}")
        for value in (None, "bullwhip"):
            invalid = deepcopy(results)
            if value is None:
                del invalid[0]["objective"]
            else:
                invalid[0]["objective"] = value
            with self.subTest(objective=value):
                with self.assertRaisesRegex(ValueError, "explicit cost objective"):
                    analyze(invalid)

    def test_explicit_different_objective_is_rejected_even_for_mock_results(self):
        results = pilot_results()[:1]
        results[0].update(backend="mock", objective="bullwhip")
        with self.assertRaisesRegex(ValueError, "different objective"):
            analyze(results)

    def test_mock_and_synthetic_results_do_not_require_provenance(self):
        synthetic = analyze(pilot_results()[:1])
        self.assertFalse(synthetic["provenance"]["verified_actual_api"])
        mock = pilot_results()[:1]
        mock[0]["backend"] = "mock"
        self.assertFalse(analyze(mock)["provenance"]["verified_actual_api"])


if __name__ == "__main__":
    unittest.main()
