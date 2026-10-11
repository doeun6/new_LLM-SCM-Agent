from contextlib import redirect_stdout
import io
import tempfile
import unittest
from pathlib import Path

from scm.config import APIConfig, SimulationConfig
from scm.engine import make_warmup
from scm.harness import Harness
from scm.metrics import summarize
from scm.provider import MockProvider
from scm.registry import get_condition, registry
from scm.runner import run_batch

class IntegrationTests(unittest.TestCase):
    def test_all_103_registered_conditions_complete_sequentially(self):
        config = SimulationConfig(evaluation_periods=2)
        warmup, demand = make_warmup(config, 123)
        conditions = registry()
        self.assertEqual(len(conditions), 103)
        self.assertEqual(sum(c.id == 'A' for c in conditions.values()), 1)
        self.assertIn('C0M0T0', conditions)
        self.assertEqual(sum(c.id.startswith('C') for c in conditions.values()), 100)
        self.assertFalse(any(c.id.startswith('E') for c in conditions.values()))
        for condition in conditions.values():
            with self.subTest(condition=condition.id):
                env, provider = warmup.clone(), MockProvider()
                harness = Harness(config, condition, provider)
                for t in (31,32):
                    message = None
                    for i in range(4):
                        D = demand[t-1] if i == 0 else env.agents[i-1].history[-1]['Q_final']
                        row = env.begin_agent(i,t,D)
                        result = harness.decide(env,i,row,message)
                        message = result['outgoing_message']
                rows = [r for a in env.agents for r in a.history if r['t']>30]
                summary = summarize(rows,provider.events,config)
                self.assertGreaterEqual(summary['cost_per_period'],0)
                self.assertEqual(env.period_completed,32)
                self.assertEqual(summary['reported_cost_usd'],0)
                if condition.id == 'A':
                    self.assertTrue(all(row['Q_base'] is None for row in rows))
                    self.assertEqual(summary['forecast_method_counts'], {})
                    self.assertEqual(summary['order_adjustment_observations'], 0)
                    self.assertEqual({event['task'] for event in provider.events}, {'order'})
                    self.assertEqual(summary['api_calls'], 8)
                elif condition.id == 'C0M0T0':
                    self.assertTrue(all(row['Q_base'] is not None for row in rows))
                    self.assertEqual(summary['order_adjustment_observations'], 8)
                    self.assertEqual(summary['api_calls'], 16)

    def test_explicit_batch_resume_and_warmup_pairing(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            cfg = SimulationConfig(evaluation_periods=2)
            first = run_batch(['A','C3M1T2'],[89],tmp,workers=2,config=cfg)
            resumed = run_batch(['A','C3M1T2'],[89],tmp,workers=2,config=cfg,resume=True,budget_usd=9)
            self.assertEqual(first,resumed)
            self.assertEqual(first[0]['warmup_hash'],first[1]['warmup_hash'])
            self.assertEqual(first[0]['demand_hash'],first[1]['demand_hash'])
            self.assertTrue((Path(tmp)/'budget_changes.json').exists())
            with self.assertRaises(ValueError): run_batch(['A'],[89],tmp,config=cfg,resume=True)

    def test_missing_selection_and_unknown_reference_rejected(self):
        with self.assertRaises(ValueError): get_condition('E_COST')
        with self.assertRaises(ValueError): get_condition('D_cost','bullwhip')
        self.assertEqual(get_condition('C0M0T0').id, 'C0M0T0')
        self.assertFalse(get_condition('C0M0T0').direct_order)
        self.assertTrue(get_condition('A').direct_order)
        self.assertIsNone(get_condition('A').t)

if __name__ == '__main__': unittest.main()
