from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scm.__main__ import main
from scm.registry import registry


class CLITests(unittest.TestCase):
    def dry_run(self, conditions, objective=None):
        args = ['run', '--conditions', *conditions, '--seeds', '123',
                '--backend', 'openrouter', '--output', 'unused-dry-run-output', '--dry-run']
        if objective:
            args += ['--objective', objective]
        output = io.StringIO()
        with patch('scm.__main__.run_batch', side_effect=AssertionError('Dry run must not execute')), redirect_stdout(output):
            self.assertEqual(main(args), 0)
        return json.loads(output.getvalue())

    def test_direct_A_and_T0_have_distinct_call_estimates(self):
        for condition, calls in [('A', 400), ('C0M0T0', 800), ('D_cost', 700), ('D_bullwhip', 700)]:
            with self.subTest(condition=condition):
                report = self.dry_run([condition])
                self.assertEqual(report['conditions'], [condition])
                self.assertEqual(report['estimated_calls_without_retry'], calls)
                self.assertEqual(report['paid_requests_made'], 0)

    def test_objective_selection_and_fixed_D_objectives(self):
        report = self.dry_run(['A', 'C0M0T0'], 'bullwhip')
        self.assertEqual(report['objectives'], {'A': 'bullwhip', 'C0M0T0': 'bullwhip'})
        self.assertEqual(self.dry_run(['A'])['objectives'], {'A': 'cost'})
        self.assertEqual(self.dry_run(['D_cost', 'D_bullwhip'])['objectives'],
                         {'D_cost': 'cost', 'D_bullwhip': 'bullwhip'})
        with self.assertRaisesRegex(ValueError, 'fixed'):
            self.dry_run(['D_cost'], 'bullwhip')

    def test_requested_objective_is_forwarded_to_runner(self):
        with patch('scm.__main__.run_batch') as run:
            self.assertEqual(main(['run', '--conditions', 'A', '--seeds', '123', '--backend', 'mock',
                                   '--output', 'unused-test-output', '--objective', 'bullwhip']), 0)
        self.assertEqual(run.call_args.kwargs['objective'], 'bullwhip')

    def test_registry_artifact_matches_executable_conditions(self):
        project = Path(__file__).resolve().parents[1]
        saved = json.loads((project / 'configs' / 'registered_conditions.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, [asdict(condition) for condition in registry().values()])


if __name__ == '__main__':
    unittest.main()
