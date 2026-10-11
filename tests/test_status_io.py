"""Standalone status tests; fixtures exist only in OS temporary directories."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from status import completed_status, main


class StatusReadTests(unittest.TestCase):
    def make_fixture(self, directory):
        root = Path(directory)
        (root / "batch_manifest.json").write_text(json.dumps({
            "conditions": [{"id": "A"}, {"id": "D"}], "seeds": [1, 2],
        }), encoding="utf-8")
        completed = root / "episodes" / "A_seed1"
        completed.mkdir(parents=True)
        (completed / "summary.json").write_text(json.dumps({
            "complete": True, "condition": "A", "seed": 1,
            "metrics": {"reported_cost_usd": 0.012,
                        "known_reported_cost_usd_sum": 0.012},
        }), encoding="utf-8")
        active = root / "episodes" / "D_seed1"
        active.mkdir()
        # Invalid content makes any accidental live-file JSON read fail.
        for name in ("progress.json", "checkpoint.json", "api_calls.jsonl"):
            (active / name).write_text("DO NOT OPEN ACTIVE FILES", encoding="utf-8")
        return root

    def test_completed_summary_only_and_explicit_partial_cost(self):
        with tempfile.TemporaryDirectory(prefix="scm_status_io_") as directory:
            root = self.make_fixture(directory)
            original_read = Path.read_text
            opened = []

            def checked_read(path, *args, **kwargs):
                opened.append(path.name)
                self.assertIn(path.name, {"batch_manifest.json", "summary.json"})
                return original_read(path, *args, **kwargs)

            with patch.object(Path, "read_text", checked_read):
                result = completed_status(root)
            self.assertEqual(opened, ["batch_manifest.json", "summary.json"])
            self.assertEqual(result["completed_episodes"], 1)
            self.assertEqual(result["planned_episodes"], 4)
            self.assertEqual(result["completed_episode_percent"], 25.0)
            self.assertEqual(result["completed_episodes_reported_cost_usd"], 0.012)
            self.assertFalse(result["unfinished_episode_spend_included"])
            self.assertFalse(result["unfinished_episode_progress_included"])
            self.assertNotIn("progress_percent", result)
            self.assertNotIn("reported_cost_so_far_usd", result)

    def test_missing_cost_retains_null_and_known_subtotal(self):
        with tempfile.TemporaryDirectory(prefix="scm_status_io_") as directory:
            root = self.make_fixture(directory)
            second = root / "episodes" / "D_seed1" / "summary.json"
            second.write_text(json.dumps({
                "complete": True, "condition": "D", "seed": 1,
                "metrics": {"reported_cost_usd": None,
                            "known_reported_cost_usd_sum": 0.003},
            }), encoding="utf-8")
            result = completed_status(root)
            self.assertIsNone(result["completed_episodes_reported_cost_usd"])
            self.assertEqual(result["completed_episodes_known_cost_subtotal_usd"], 0.015)
            self.assertEqual(result["completed_episodes_missing_cost_reports"], 1)

    def test_main_and_incomplete_summary_ignored(self):
        with tempfile.TemporaryDirectory(prefix="scm_status_io_") as directory:
            root = self.make_fixture(directory)
            (root / "episodes" / "D_seed1" / "summary.json").write_text(
                '{"complete":false}', encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                main([str(root)])
            self.assertEqual(json.loads(output.getvalue())["completed_episodes"], 1)

    def test_missing_manifest_is_clear(self):
        with tempfile.TemporaryDirectory(prefix="scm_status_io_") as directory:
            with self.assertRaisesRegex(SystemExit, "No batch_manifest.json"):
                completed_status(directory)


if __name__ == "__main__":
    unittest.main()
