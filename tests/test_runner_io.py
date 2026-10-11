"""Persistence regressions observed during the actual Windows pilot."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scm.runner import atomic_json


class AtomicJsonTests(unittest.TestCase):
    def test_transient_permission_errors_retry_same_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            target.write_text('{"old": true}', encoding='utf-8')
            real_replace = os.replace
            attempts = []

            def replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) < 3:
                    raise PermissionError('temporary Windows reader lock')
                return real_replace(source, destination)

            with patch('scm.runner.os.replace', side_effect=replace), patch('scm.runner.time.sleep') as sleep:
                atomic_json(target, {'new': 42})
            self.assertEqual(json.loads(target.read_text()), {'new': 42})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(len({source for source, _ in attempts}), 1)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.1])
            self.assertEqual(list(Path(directory).glob('*.tmp-*')), [])

    def test_permanent_permission_error_preserves_prior_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            previous = '{"old": true}'
            target.write_text(previous, encoding='utf-8')
            with patch('scm.runner.os.replace', side_effect=PermissionError('permanent')) as replace, patch('scm.runner.time.sleep'):
                with self.assertRaises(PermissionError):
                    atomic_json(target, {'new': 42})
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(target.read_text(), previous)
            self.assertEqual(list(Path(directory).glob('*.tmp-*')), [])

    def test_other_os_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            with patch('scm.runner.os.replace', side_effect=OSError('storage failure')) as replace, patch('scm.runner.time.sleep') as sleep:
                with self.assertRaises(OSError):
                    atomic_json(target, {'new': 42})
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).glob('*.tmp-*')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing regression')
    def test_actual_windows_reader_lock_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            target.write_text('{"old": true}', encoding='utf-8')
            reader = target.open('rb')
            release = threading.Timer(0.08, reader.close)
            release.start()
            try:
                with patch('scm.runner.os.replace', wraps=os.replace) as replace:
                    atomic_json(target, {'new': 42})
                self.assertGreaterEqual(replace.call_count, 2)
                self.assertEqual(json.loads(target.read_text()), {'new': 42})
            finally:
                release.join(timeout=2)
                reader.close()


if __name__ == '__main__':
    unittest.main()
