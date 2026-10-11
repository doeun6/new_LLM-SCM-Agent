from copy import deepcopy
import json
import statistics
import unittest
from unittest.mock import patch

from scm.config import SimulationConfig
from scm.engine import make_warmup
from scm.forecasting import forecast, normalize_order, order_up_to
from scm.harness import Harness, build_C, _validate_message, plan_schema
from scm.provider import MockProvider, RecoverableCallError, FatalAPIError, validate_schema
from scm.registry import get_condition


class ScriptProvider(MockProvider):
    def __init__(self, outputs=None, failures=None):
        super().__init__()
        self.outputs = outputs or {}
        self.failures = failures or {}

    def ask(self, task, instructions, payload, schema, meta, validate=None):
        if task in self.failures:
            error = self.failures[task]
            for attempt in (0, 1):
                self.events.append(dict(meta, task=task, attempt=attempt, success=False,
                                        format_failure=True, input_tokens=5, output_tokens=1,
                                        total_tokens=6, reported_cost_usd=0.001,
                                        payload=deepcopy(payload), instructions=instructions))
            raise error
        if task in self.outputs:
            result = deepcopy(self.outputs[task])
            validate_schema(result, schema)
            if validate:
                validate(result)
            self.events.append(dict(meta, task=task, attempt=0, success=True,
                                    format_failure=False, input_tokens=5, output_tokens=1,
                                    total_tokens=6, reported_cost_usd=0.001,
                                    payload=deepcopy(payload), parsed_output=result,
                                    instructions=instructions))
            return result
        return super().ask(task, instructions, payload, schema, meta, validate=validate)


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = SimulationConfig(evaluation_periods=2)
        cls.warmup, cls.demands = make_warmup(cls.config, 17)

    def begin(self):
        env = self.warmup.clone()
        row = env.begin_agent(0, 31, self.demands[30])
        return env, row

    def decide(self, condition, provider=None, received=None):
        env, row = self.begin()
        provider = provider or MockProvider()
        result = Harness(self.config, get_condition(condition), provider).decide(
            env, 0, row, received)
        return env, row, result, provider

    def test_context_windows_and_pre_order_timestamps(self):
        env, row = self.begin()
        for level, n in (("C0", 10), ("C1", 20), ("C2", 5)):
            with self.subTest(level=level):
                context = build_C(level, env.agents[0].history, row)
                self.assertEqual([r["t"] for r in context["states"]], list(range(32-n, 32)))
                self.assertEqual(len(context["past_orders"]), n-1)
                self.assertEqual(context["past_orders"][-1]["t"], 30)
                self.assertEqual(context["current"]["P_pre"], row["P_pre"])
                for record in context["states"]:
                    self.assertEqual(set(record), {"t", "I", "B", "P_pre", "D"})

    def test_summary_gives_order_statistics_without_raw_rows(self):
        env, row = self.begin()
        context = build_C("C3", env.agents[0].history, row)
        self.assertNotIn("states", context)
        self.assertNotIn("past_orders", context)
        self.assertNotIn("slope", json.dumps(context))
        expected = [r["D"] for r in env.agents[0].history[-9:]]+[row["D"]]
        self.assertEqual(context["statistics"]["D"]["mean"], statistics.mean(expected))
        self.assertEqual(context["statistics"]["D"]["standard_deviation"], statistics.stdev(expected))
        self.assertEqual(set(context["statistics"]["D"]),
                         {"mean", "standard_deviation", "minimum", "maximum"})
        # Orders: t-9..t-1 only, because the current order is not yet decided.
        past = env.agents[0].history[-9:]
        self.assertEqual((context["order_window"], context["order_period_start"],
                          context["order_period_end"]), (9, 22, 30))
        orders = context["order_statistics"]
        self.assertEqual(set(orders), {"Q_base", "Q_final", "Q_final_minus_Q_base"})
        self.assertEqual(set(context["order_definitions"]), set(orders))
        final = [r["Q_final"] for r in past]
        gap = [r["Q_final"]-r["Q_base"] for r in past]
        self.assertEqual(orders["Q_final"]["mean"], statistics.mean(final))
        self.assertEqual(orders["Q_final"]["standard_deviation"], statistics.stdev(final))
        self.assertEqual(orders["Q_final_minus_Q_base"]["minimum"], min(gap))
        # Warm-up executes the support quantity, so every warm-up gap is zero.
        self.assertEqual(orders["Q_final_minus_Q_base"],
                         {"mean": 0, "standard_deviation": 0.0, "minimum": 0, "maximum": 0})

    def test_summary_order_statistics_use_executed_signed_gaps(self):
        env, row = self.begin()
        history = deepcopy(env.agents[0].history)
        for record, (base, final) in zip(history[-9:], [(10, 12), (10, 8)]*4 + [(10, 10)]):
            record["Q_base"], record["Q_final"] = base, final
        orders = build_C("C3", history, row)["order_statistics"]
        gap = [2, -2]*4 + [0]
        self.assertEqual(orders["Q_final_minus_Q_base"]["mean"], statistics.mean(gap))
        self.assertEqual(orders["Q_final_minus_Q_base"]["standard_deviation"], statistics.stdev(gap))
        self.assertEqual((orders["Q_final_minus_Q_base"]["minimum"],
                          orders["Q_final_minus_Q_base"]["maximum"]), (-2, 2))
        history[-1]["Q_base"] = None
        orders = build_C("C3", history, row)["order_statistics"]
        self.assertIsNone(orders["Q_base"])
        self.assertIsNone(orders["Q_final_minus_Q_base"])
        self.assertIsNotNone(orders["Q_final"])

    def test_raw_context_levels_are_unchanged_by_c3_order_statistics(self):
        env, row = self.begin()
        for level in ("C0", "C1", "C2"):
            with self.subTest(level=level):
                context = build_C(level, env.agents[0].history, row)
                self.assertNotIn("order_statistics", context)
                self.assertIn("past_orders", context)

    def test_A_is_one_direct_order_call_with_only_own_C0_and_current_D(self):
        env, row = self.begin()

        class UnreadableDemandHistory(list):
            def __getitem__(self, key):
                raise AssertionError("A must never access forecasting history")

        env.agents[0].demands = UnreadableDemandHistory(env.agents[0].demands)
        provider = ScriptProvider({"order": {"order_quantity": 10.5,
                                             "rationale": "Current needs and local state."}})
        with patch("scm.harness.forecast", side_effect=AssertionError("No forecast for A")), \
             patch("scm.harness.order_up_to", side_effect=AssertionError("No OUT for A")):
            result = Harness(self.config, get_condition("A"), provider).decide(
                env, 0, row, {"unavailable_downstream": "must not enter A"})
        self.assertEqual([event["task"] for event in provider.events], ["order"])
        payload = provider.events[0]["payload"]
        self.assertEqual(set(payload), {"C", "D"})
        self.assertEqual(payload["D"], row["D"])
        self.assertEqual(payload["C"]["window"], 10)
        self.assertEqual(payload["C"]["current"]["D"], row["D"])
        self.assertNotIn("history_30", json.dumps(payload))
        for field in ("Q_base", "Q_base_raw", "forecast", "forecast_raw",
                      "support_policy", "selected_method", "order_deviation"):
            self.assertIsNone(result[field], field)
        self.assertIsNone(result["received_message"])
        self.assertEqual(result["Q_final"], 11)
        self.assertEqual(result["P_post"], row["P_pre"] + 11)
        self.assertNotIn("order-up-to", provider.events[0]["instructions"])
        self.assertNotIn("M1", provider.events[0]["instructions"])
        self.assertNotIn("Consider Q_base", provider.events[0]["instructions"])

    def test_A_failed_order_uses_current_request_without_creating_forecast_or_reference(self):
        env = self.warmup.clone()
        row = env.begin_agent(0, 31, 150)

        class UnreadableDemandHistory(list):
            def __getitem__(self, key):
                raise AssertionError("Failed A must not access forecasting history")

        env.agents[0].demands = UnreadableDemandHistory(env.agents[0].demands)
        provider = ScriptProvider(failures={"order": RecoverableCallError("exhausted")})
        with patch("scm.harness.forecast", side_effect=AssertionError("No forecast fallback for A")), \
             patch("scm.harness.order_up_to", side_effect=AssertionError("No OUT fallback for A")):
            result = Harness(self.config, get_condition("A"), provider).decide(env, 0, row, None)
        self.assertEqual([event["task"] for event in provider.events], ["order", "order"])
        self.assertEqual(result["Q_final"], normalize_order(row["D"], self.config.order_cap))
        self.assertIsNone(result["Q_base"])
        self.assertIsNone(result["Q_base_raw"])
        self.assertIsNone(result["forecast"])
        self.assertIsNone(result["order_deviation"])
        self.assertEqual(result["fallback_policy"], "current_request")
        self.assertEqual(result["fallback_quantity_raw"], 150)
        self.assertEqual(result["Q_final"], 100)
        self.assertTrue(result["order_corrected"])
        self.assertEqual(result["correction_count"], 1)
        self.assertTrue(result["order_fallback"])
        self.assertFalse(result["forecast_fallback"])
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(result["retry_count"], 1)

    def test_A_history_preserves_warmup_support_and_later_null_references(self):
        env = self.warmup.clone()
        provider = MockProvider()
        harness = Harness(self.config, get_condition("A"), provider)
        for t in (31, 32):
            incoming = self.demands[t-1]
            for i in range(4):
                row = env.begin_agent(i, t, incoming)
                result = harness.decide(env, i, row, None)
                incoming = result["Q_final"]
                self.assertIsNone(result["Q_base"])
                self.assertEqual(result["Q_final"], row["D"])
        self.assertEqual(len(provider.events), 8)
        latest_C = provider.events[4]["payload"]["C"]
        self.assertEqual(latest_C["past_orders"][-1]["t"], 31)
        self.assertIsNone(latest_C["past_orders"][-1]["Q_base"])
        self.assertIsInstance(latest_C["past_orders"][-2]["Q_base"], int)
        env.clone().validate_invariants()

    def test_each_tool_call_layout_and_information_isolation(self):
        expected = {"T0": ["forecast", "order"], "T1": ["forecast", "order"],
                    "T2": ["order"], "T3": ["select", "forecast", "order"],
                    "T4": ["select", "order"]}
        for tool, tasks in expected.items():
            with self.subTest(tool=tool):
                env, row, result, provider = self.decide("C2M0"+tool,
                                                       received={"forbidden_extra": "must be absent"})
                self.assertEqual([e["task"] for e in provider.events], tasks)
                self.assertEqual(len(env.agents[0].history), 31)
                self.assertEqual(result["P_post"], row["P_pre"]+result["Q_final"])
                self.assertIsNone(result["outgoing_message"])
                order = next(e for e in provider.events if e["task"] == "order")
                self.assertEqual(set(order["payload"]), {"C", "M", "Q_base"})
                self.assertIsNone(order["payload"]["M"])
                self.assertEqual(len(order["payload"]["C"]["states"]), 5)
                self.assertNotIn("history_30", json.dumps(order["payload"]))
                if tool in ("T3", "T4"):
                    selection = provider.events[0]["payload"]
                    self.assertEqual(set(selection), {"C", "M", "candidates"})
                    self.assertNotIn("history_30", json.dumps(selection))
                if tool in ("T0", "T1", "T3"):
                    payload = next(e["payload"] for e in provider.events if e["task"] == "forecast")
                    self.assertEqual(payload["history_30"], env.agents[0].demands[-30:])
                    self.assertNotIn("C", payload)
                    self.assertNotIn("M", payload)
                    self.assertEqual("formula" in payload, tool != "T0")

    def test_m1_is_environment_generated_after_commit_and_independent_of_C2_window(self):
        env, row, result, provider = self.decide("C2M1T2")
        outgoing = result["outgoing_message"]
        self.assertEqual([e["task"] for e in provider.events], ["order"])
        self.assertEqual(outgoing["P_post"], row["P_pre"]+result["Q_final"])
        self.assertEqual(outgoing["D_mean_10"], statistics.mean(env.agents[0].demands[-10:]))
        self.assertEqual(outgoing["D_std_10"], statistics.stdev(env.agents[0].demands[-10:]))
        self.assertEqual(outgoing["t"], 31)
        upstream_row = env.begin_agent(1, 31, result["Q_final"])
        upstream_provider = MockProvider()
        Harness(self.config, get_condition("C2M1T2"), upstream_provider).decide(
            env, 1, upstream_row, outgoing)
        self.assertEqual(upstream_provider.events[0]["payload"]["M"], outgoing)

    def test_generated_messages_use_only_permitted_information_after_order(self):
        for level in ("M2", "M3", "M4"):
            with self.subTest(level=level):
                env, row, result, provider = self.decide("C3"+level+"T3")
                self.assertEqual([e["task"] for e in provider.events],
                                 ["select", "forecast", "order", "message"])
                payload = provider.events[-1]["payload"]
                self.assertEqual(set(payload), {"C", "M", "Q_base", "Q_final", "rationale", "P_post"})
                self.assertNotIn("history_30", json.dumps(payload))
                self.assertEqual(payload["P_post"], result["P_post"])
                if level == "M3":
                    self.assertEqual(set(result["outgoing_message"]), {"plan_t_plus_1", "plan_t_plus_2"})
                    self.assertTrue(all(type(v) is int for v in result["outgoing_message"].values()))
                self.assertEqual(env.agents[0].history[-1]["outgoing_message"], result["outgoing_message"])

    def test_direct_arithmetic_error_remains_used_and_logged(self):
        for tool in ("T1", "T3"):
            with self.subTest(tool=tool):
                provider = ScriptProvider({"forecast": {"forecast_h1": 300.25, "forecast_h2": 301.75}})
                env, row, result, _ = self.decide("C0M0"+tool, provider)
                self.assertEqual(result["forecast"], [300.25, 301.75])
                self.assertTrue(result["arithmetic_error"])
                self.assertEqual(result["arithmetic_error_count"], 2)
                self.assertEqual(result["Q_base"], 100)
                self.assertEqual(result["fallback_count"], 0)
                self.assertEqual(result["retry_count"], 0)

    def test_forecast_and_selection_failures_use_logged_lr_fallback(self):
        for condition, task in (("C0M0T1", "forecast"), ("C0M0T4", "select")):
            with self.subTest(condition=condition):
                provider = ScriptProvider(failures={task: RecoverableCallError("exhausted")})
                env, row, result, _ = self.decide(condition, provider)
                expected = forecast("linear_regression", env.agents[0].demands)
                self.assertEqual(result["forecast"], expected)
                self.assertTrue(result["forecast_fallback"])
                self.assertEqual(result["fallback_count"], 1)
                self.assertEqual(result["retry_count"], 1)
                self.assertEqual(result["format_failure_count"], 2)
                self.assertEqual(result["input_tokens"], 10)

    def test_final_order_failure_uses_current_base_not_previous_order(self):
        provider = ScriptProvider(failures={"order": RecoverableCallError("exhausted")})
        env, row, result, _ = self.decide("C0M0T2", provider)
        self.assertEqual(result["Q_final"], result["Q_base"])
        self.assertTrue(result["order_fallback"])
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(len(env.agents[0].history), 31)

    def test_selected_method_is_not_lost_when_its_direct_calculation_falls_back(self):
        provider = ScriptProvider(
            outputs={"select": {"method": "simple_exponential_smoothing"}},
            failures={"forecast": RecoverableCallError("exhausted")})
        _, _, result, _ = self.decide("C0M0T3", provider)
        self.assertEqual(result["llm_selected_method"], "simple_exponential_smoothing")
        self.assertEqual(result["selected_method"], "linear_regression")
        self.assertTrue(result["forecast_fallback"])

    def test_message_failure_preserves_committed_order(self):
        provider = ScriptProvider(outputs={"order": {"order_quantity": 42, "rationale": "A deliberate order."}},
                                  failures={"message": RecoverableCallError("exhausted")})
        env, row, result, _ = self.decide("C0M4T2", provider)
        self.assertEqual(result["Q_final"], 42)
        self.assertEqual(result["P_post"], row["P_pre"]+42)
        self.assertIsNone(result["outgoing_message"])
        self.assertTrue(result["message_fallback"])
        self.assertEqual(len(env.agents[0].history), 31)

    def test_references_bypass_order_up_to_and_all_still_call_order_llm(self):
        for condition in ("D_cost", "D_bullwhip"):
            with self.subTest(condition=condition):
                message = "The downstream order reflects current replenishment needs."
                env, row, result, provider = self.decide(condition, received=message)
                self.assertEqual([e["task"] for e in provider.events], ["order", "message"])
                values = env.agents[0].demands[-30:]
                raw = (forecast("linear_regression", values)[0] if condition == "D_cost"
                       else (2*statistics.mean(values))**0.5)
                self.assertEqual(result["Q_base"], normalize_order(raw))
                self.assertIn("NOT been adjusted", provider.events[0]["instructions"])
                self.assertEqual(set(provider.events[0]["payload"]), {"C", "M", "Q_base"})
                self.assertEqual(provider.events[0]["payload"]["C"]["window"], 10)
                self.assertEqual(provider.events[0]["payload"]["M"], message)
                self.assertEqual(provider.events[0]["message_level"], "M4")

    def test_supported_order_prompt_explains_and_integrates_three_sources(self):
        _, _, _, provider = self.decide("C3M1T2")
        instructions = provider.events[0]["instructions"]
        self.assertIn("C describes your own current state", instructions)
        self.assertIn("immediate downstream agent", instructions)
        self.assertIn("already calculated", instructions)
        self.assertIn("Integrate these three information sources", instructions)

    def test_shared_order_rounding_clipping_records_raw_and_correction(self):
        for raw, expected in ((10.5, 11), (-9.2, 0), (101.3, 100)):
            with self.subTest(raw=raw):
                provider = ScriptProvider({"order": {"order_quantity": raw, "rationale": "Test normalization."}})
                _, _, result, _ = self.decide("C0M0T2", provider)
                self.assertEqual(result["Q_final"], expected)
                self.assertEqual(result["Q_final_raw"], raw)
                self.assertTrue(result["order_corrected"])

    def test_fatal_and_software_errors_are_not_policy_fallbacks(self):
        for error in (FatalAPIError("bad route"), RuntimeError("software bug")):
            env, row = self.begin()
            provider = ScriptProvider(failures={"order": error})
            with self.assertRaises(type(error)):
                Harness(self.config, get_condition("C0M0T2"), provider).decide(env, 0, row, None)
            self.assertEqual(len(env.agents[0].history), 30)

    def test_m2_semantic_checks_are_narrow(self):
        for message in ("Inventory is 10.", "I will increase replenishment.", "We plan to order more."):
            with self.subTest(message=message), self.assertRaises(ValueError):
                _validate_message({"message": message}, "M2")
        _validate_message({"message": "A shortage risk explains the current replenishment."}, "M2")

    def test_m3_accepts_only_integer_plans_within_order_cap(self):
        cap = self.config.order_cap
        schema = plan_schema(cap)
        for good in ({"plan_t_plus_1": 0, "plan_t_plus_2": cap}, {"plan_t_plus_1": 12, "plan_t_plus_2": 10}):
            validate_schema(good, schema)
            _validate_message(good, "M3", cap)
        bad_values = (-1, cap+1, 10.5, "10", True, None)
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate = {"plan_t_plus_1": value, "plan_t_plus_2": 10}
                validate_schema(candidate, schema)
                _validate_message(candidate, "M3", cap)
        for candidate in ({"plan_t_plus_1": 10}, {"plan_t_plus_1": 10, "plan_t_plus_2": 10, "note": "x"}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_schema(candidate, schema)

    def test_m3_numeric_plan_reaches_upstream_selection_and_order(self):
        plan = {"plan_t_plus_1": 13, "plan_t_plus_2": 9}
        provider = ScriptProvider({"message": plan})
        env, row, result, _ = self.decide("C0M3T3", provider)
        self.assertEqual(result["outgoing_message"], plan)
        upstream_row = env.begin_agent(1, 31, result["Q_final"])
        upstream_provider = MockProvider()
        Harness(self.config, get_condition("C0M3T3"), upstream_provider).decide(
            env, 1, upstream_row, result["outgoing_message"])
        by_task = {e["task"]: e for e in upstream_provider.events}
        self.assertEqual(by_task["select"]["payload"]["M"], plan)
        self.assertEqual(by_task["order"]["payload"]["M"], plan)
        self.assertIn("planned order quantities", by_task["order"]["instructions"])

    def test_m3_invalid_plan_exhausts_to_empty_message_but_keeps_order(self):
        provider = ScriptProvider(outputs={"order": {"order_quantity": 42, "rationale": "A deliberate order."}},
                                  failures={"message": RecoverableCallError("plan out of range")})
        env, row, result, _ = self.decide("C0M3T2", provider)
        self.assertEqual(result["Q_final"], 42)
        self.assertIsNone(result["outgoing_message"])
        self.assertTrue(result["message_fallback"])

    def test_factory_does_not_generate_a_message_to_nonexistent_agent(self):
        env = self.warmup.clone()
        provider = MockProvider()
        incoming, received = self.demands[30], None
        for i in range(4):
            row = env.begin_agent(i, 31, incoming)
            result = Harness(self.config, get_condition("C0M4T2"), provider).decide(env, i, row, received)
            incoming, received = result["Q_final"], result["outgoing_message"]
        self.assertIsNone(received)
        self.assertEqual(sum(e["task"] == "message" for e in provider.events), 3)
        self.assertEqual(env.period_completed, 31)

    def test_replayed_calls_keep_usage_but_do_not_fabricate_original_wall_latency(self):
        class CachedProvider(MockProvider):
            def ask(self, task, instructions, payload, schema, meta, validate=None):
                for event in self.events:
                    if (event["task"], event["t"], event["agent"], event["condition"]) == (
                            task, meta["t"], meta["agent"], meta["condition"]):
                        return deepcopy(event["parsed_output"])
                return super().ask(task, instructions, payload, schema, meta, validate=validate)
        provider = CachedProvider()
        _, _, original, _ = self.decide("C0M4T2", provider)
        self.assertFalse(original["latency_replayed"])
        self.assertIsNotNone(original["decision_latency_ms"])
        _, _, replayed, _ = self.decide("C0M4T2", provider)
        self.assertTrue(replayed["latency_replayed"])
        self.assertIsNone(replayed["decision_latency_ms"])
        self.assertGreaterEqual(replayed["replay_wall_time_ms"], 0)
        self.assertEqual(len(provider.events), 2)
        self.assertEqual(replayed["api_call_count"], original["api_call_count"])


if __name__ == "__main__":
    unittest.main()
