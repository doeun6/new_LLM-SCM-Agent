import json
import unittest

from scm.config import SimulationConfig
from scm.engine import SupplyChain, generate_demand, make_warmup
from scm.forecasting import METHODS, forecast, normalize_order, order_up_to


class ForecastTests(unittest.TestCase):
    def test_exact_trend_and_constant_forecasts(self):
        self.assertEqual(forecast("linear_regression", list(range(1, 31))), [31.0, 32.0])
        self.assertEqual(forecast("moving_average_30", list(range(1, 31))), [15.5, 15.5])
        for method in METHODS:
            with self.subTest(method=method):
                for value in forecast(method, [10] * 30):
                    self.assertAlmostEqual(value, 10)

    def test_ses_resets_to_oldest_value_of_exact_rolling_window(self):
        values = [100000] * 12 + [10] * 29 + [20]
        self.assertEqual(forecast("simple_exponential_smoothing", values), [13.0, 13.0])
        self.assertEqual(forecast("simple_exponential_smoothing", [10] * 30), [10.0, 10.0])
        with self.assertRaises(ValueError):
            forecast("linear_regression", [10] * 29)

    def test_negative_forecast_and_half_up_are_common_postprocessing(self):
        self.assertEqual(forecast("linear_regression", list(range(29, -1, -1))), [0.0, 0.0])
        self.assertEqual(normalize_order(2.5), 3)
        self.assertEqual(normalize_order(3.5), 4)
        self.assertEqual(normalize_order(-4), 0)
        self.assertEqual(normalize_order(120), 100)
        self.assertEqual(normalize_order(120, cap=None), 120)
        for bad in [True, "10", float("nan"), float("inf")]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_order(bad)

    def test_out_uses_post_fulfillment_ip_not_another_demand_subtraction(self):
        config = SimulationConfig()
        self.assertEqual(order_up_to([10.25, 11.25], 0, 2, 10, config), 13.5)
        self.assertEqual(normalize_order(order_up_to([10.25, 11.25], 0, 2, 10, config)), 14)


class EngineTests(unittest.TestCase):
    def period(self, env, t, customer, quantities):
        rows = []
        incoming = customer
        for index, quantity in enumerate(quantities):
            before = env.agents[index]
            old_I, old_B, old_P = before.I, before.B, before.P
            row = env.begin_agent(index, t, incoming)
            self.assertEqual(row["I"], old_I + row["A"] - row["X"])
            self.assertEqual(row["B"], old_B + incoming - row["X"])
            self.assertEqual(row["P_pre"], old_P - row["A"])
            self.assertLessEqual(row["X"], old_I + row["A"])
            final = env.finish_agent(index, t, quantity, quantity)
            self.assertEqual(final["P_post"], row["P_pre"] + quantity)
            rows.append(final)
            incoming = quantity
        return rows

    def test_default_constant_warmup_has_correct_fixed_point(self):
        env, demand = make_warmup(SimulationConfig(demand_std=0), 9)
        self.assertEqual(len(demand), 130)
        self.assertEqual(env.period_completed, 30)
        self.assertEqual(env.next_agent, 0)
        for state in env.agents:
            self.assertEqual((state.I, state.B, state.P), (0, 0, 20))
            self.assertEqual(state.arrivals, {31: 10, 32: 10})
            self.assertEqual(len(state.history), 30)
            self.assertEqual(len(state.demands), 60)
            self.assertTrue(all(row["Q_base"] == row["Q_final"] == 10 for row in state.history))
            self.assertTrue(all(row["cost"] == 11 for row in state.history))
            self.assertTrue(all(row["total_cost"] == row["cost"] for row in state.history))
            self.assertEqual(state.history[0]["t"], 1)

    def test_partial_shipment_backlog_and_outstanding_pipeline_are_distinct(self):
        env = SupplyChain(SimulationConfig())
        first = self.period(env, 1, 10, [20, 0, 0, 0])
        self.assertEqual(first[1]["X"], 10)
        self.assertEqual(first[1]["B"], 10)
        self.assertEqual(env.agents[0].P, 30)
        self.assertEqual(sum(env.agents[0].arrivals.values()), 20)
        self.assertEqual(first[0]["P_pre"], 10)
        self.assertEqual(first[0]["P_post"], 30)
        second = self.period(env, 2, 10, [0, 0, 0, 0])
        self.assertEqual(second[1]["X"], 10)
        third = self.period(env, 3, 10, [0, 0, 0, 0])
        fourth = self.period(env, 4, 10, [0, 0, 0, 0])
        self.assertEqual(third[0]["A"], first[1]["X"])
        self.assertEqual(fourth[0]["A"], second[1]["X"])
        self.assertEqual(env.agents[0].P, 0)

    def test_factory_order_arrives_exactly_two_periods_later(self):
        env = SupplyChain(SimulationConfig(initial_arrival=0))
        one = self.period(env, 1, 0, [0, 0, 0, 7])
        two = self.period(env, 2, 0, [0, 0, 0, 0])
        three = self.period(env, 3, 0, [0, 0, 0, 0])
        self.assertEqual([one[3]["A"], two[3]["A"], three[3]["A"]], [0, 0, 7])

    def test_old_backlog_is_not_counted_as_immediate_fulfillment(self):
        env = SupplyChain(SimulationConfig())
        first = self.period(env, 1, 15, [0, 0, 0, 0])
        second = self.period(env, 2, 10, [0, 0, 0, 0])
        self.assertEqual(first[0]["immediate_filled"], 10)
        self.assertEqual(second[0]["X"], 10)
        self.assertEqual(second[0]["B_previous"], 5)
        self.assertEqual(second[0]["immediate_filled"], 5)

    def test_sequence_double_commit_and_quantity_errors_are_rejected(self):
        env = SupplyChain(SimulationConfig())
        snapshot = env.to_dict()
        with self.assertRaises(ValueError):
            env.begin_agent(1, 1, 10)
        self.assertEqual(env.to_dict(), snapshot)
        env.begin_agent(0, 1, 10)
        with self.assertRaises(ValueError):
            env.begin_agent(0, 1, 10)
        for invalid in [1.5, -1, 101, True]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                env.finish_agent(0, 1, 10, invalid)
        with self.assertRaises(ValueError):
            env.finish_agent(0, 1, 10, 10, {"P_post": 0})
        row = env.finish_agent(0, 1, 10, 10)
        with self.assertRaises(ValueError):
            env.finish_agent(0, 1, 10, 10)
        with self.assertRaises(ValueError):
            env.begin_agent(1, 1, 9)
        row["P_post"] = 999
        self.assertEqual(env.agents[0].history[-1]["P_post"], 20)

    def test_nullable_reference_keeps_actual_order_domain_and_physics_strict(self):
        env = SupplyChain(SimulationConfig())
        env.begin_agent(0, 1, 10)
        for invalid in (None, True, -1, 1.5, 101):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                env.finish_agent(0, 1, None, invalid)
        row = env.finish_agent(0, 1, None, 12)
        self.assertIsNone(row["Q_base"])
        self.assertEqual(row["Q_final"], 12)
        self.assertEqual(row["P_post"], 22)
        clone = SupplyChain.from_dict(json.loads(json.dumps(env.to_dict())), env.config)
        self.assertIsNone(clone.agents[0].history[-1]["Q_base"])
        self.assertEqual(clone.agents[0].history[-1]["Q_final"], 12)

    def test_checkpoint_rejects_invalid_final_orders_even_with_null_reference(self):
        env = SupplyChain(SimulationConfig())
        env.begin_agent(0, 1, 10)
        env.finish_agent(0, 1, None, 10)
        for invalid in (None, True, -1, 1.5, 101):
            damaged = env.to_dict()
            damaged["agents"][0]["history"][0]["Q_final"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SupplyChain.from_dict(damaged, env.config)

    def test_seed_reproducibility_and_independent_warmup_clones(self):
        config = SimulationConfig()
        env, path = make_warmup(config, 42)
        self.assertEqual(path, generate_demand(42, config))
        self.assertNotEqual(path, generate_demand(43, config))
        self.assertEqual(env.to_dict(), make_warmup(config, 42)[0].to_dict())
        clone = SupplyChain.from_dict(json.loads(json.dumps(env.to_dict())), config)
        self.assertEqual(env.to_dict(), clone.to_dict())
        clone.agents[0].history[0]["diagnostic"] = "independent"
        self.assertNotIn("diagnostic", env.agents[0].history[0])
        clone.agents[0].demands.append(123)
        self.assertNotEqual(len(env.agents[0].demands), len(clone.agents[0].demands))

    def test_mid_decision_checkpoint_finishes_once_with_same_physics(self):
        config = SimulationConfig()
        env = SupplyChain(config)
        env.begin_agent(0, 1, 12)
        clone = SupplyChain.from_dict(json.loads(json.dumps(env.to_dict())), config)
        self.assertEqual(env.finish_agent(0, 1, 10, 10), clone.finish_agent(0, 1, 10, 10))
        self.assertEqual(env.to_dict(), clone.to_dict())
        updated = clone.update_agent_record(0, 1, {"message": "plan", "decision_latency_ms": 3})
        self.assertEqual(updated["message"], "plan")
        self.assertNotIn("message", env.agents[0].history[-1])
        with self.assertRaises(ValueError):
            clone.update_agent_record(0, 1, {"Q_final": 99})

    def test_demand_has_no_upper_cap(self):
        values = generate_demand(1, SimulationConfig(demand_mean=150, demand_std=0))
        self.assertEqual(set(values), {150})


if __name__ == "__main__":
    unittest.main()
