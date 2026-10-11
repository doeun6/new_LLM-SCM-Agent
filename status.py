"""Read completed episode summaries only: python status.py results/pilot.

No active progress, checkpoint, or API ledger is opened. Completion and cost
figures exclude unfinished episodes and are not live whole-batch totals.
"""
import json
from pathlib import Path
import sys


def completed_status(root):
    root = Path(root)
    manifest_path = root / "batch_manifest.json"
    if not manifest_path.exists():
        raise SystemExit("No batch_manifest.json in " + str(root))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    planned = len(manifest["conditions"]) * len(manifest["seeds"])
    completed = []
    for path in sorted(root.glob("episodes/*/summary.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        if summary.get("complete") is True:
            completed.append(summary)
    costs = [summary["metrics"]["reported_cost_usd"] for summary in completed]
    known_cost = sum(summary["metrics"]["known_reported_cost_usd_sum"]
                     for summary in completed)
    return {
        "scope": "completed_episode_summaries_only",
        "completed_episodes": len(completed),
        "planned_episodes": planned,
        "completed_episode_percent": round(100 * len(completed) / planned, 2) if planned else None,
        "completed_episodes_reported_cost_usd": round(sum(costs), 6) if all(cost is not None for cost in costs) else None,
        "completed_episodes_known_cost_subtotal_usd": round(known_cost, 6),
        "completed_episodes_missing_cost_reports": sum(cost is None for cost in costs),
        "unfinished_episode_progress_included": False,
        "unfinished_episode_spend_included": False,
        "note": "Unfinished episodes are excluded. These figures are neither live decision progress nor total spend so far.",
        "completed": [{key: summary[key] for key in ("condition", "seed")}
                      for summary in completed],
    }


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    root = arguments[0] if arguments else "results/pilot"
    print(json.dumps(completed_status(root), indent=2))


if __name__ == "__main__":
    main()
