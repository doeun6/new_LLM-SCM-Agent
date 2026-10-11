"""Independent, read-only pilot audit. No scm imports or network operations.

Invoke only after the registered 32 episodes and primary analysis are complete:
python docs/audit_completed_pilot.py results/pilot --analysis PATH_TO_pilot_analysis.json

The script writes no files. Redirect its aggregate JSON output to retain a report.
It checks completeness before computing any inferential statistic.
"""

import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import sys


CONDITIONS = ("A", "C0M0T2", "C3M1T2", "C3M1T4")
ROLES = ("retailer", "wholesaler", "distributor", "factory")
SEEDS = tuple(range(1, 9))


def require(test, label):
    if not test:
        raise ValueError(label)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def same_number(actual, expected, label):
    if actual is None or expected is None:
        require(actual is expected, label + " null mismatch")
    else:
        require(type(actual) in (int, float) and math.isfinite(actual), label + " invalid numeric value")
        require(math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9), label + " arithmetic mismatch")


def independent_cv(values):
    # Explicit sample-variance calculation, separate from production statistics.stdev.
    mean = sum(values) / len(values)
    if len(values) < 2 or mean == 0:
        return None
    sample_variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(sample_variance) / mean


def independent_exact_p(differences):
    # Compare integer sums of rationalized differences, with no tolerance at tails.
    values = [Fraction(str(value)) for value in differences]
    common_denominator = math.lcm(*(value.denominator for value in values))
    integers = [value.numerator * (common_denominator // value.denominator) for value in values]
    observed = abs(sum(integers))
    extreme = 0
    for bits in range(1 << len(integers)):
        statistic = abs(sum(value if bits & (1 << index) else -value for index, value in enumerate(integers)))
        extreme += statistic >= observed
    return extreme / (1 << len(integers))


def independent_holm(values):
    indexed = sorted(enumerate(values), key=lambda pair: pair[1])
    adjusted = [0.0] * len(values)
    for rank, (original_index, _) in enumerate(indexed):
        adjusted[original_index] = min(1.0, max((len(values) - earlier) * indexed[earlier][1] for earlier in range(rank + 1)))
    return adjusted


def audit(pilot_root, analysis_path):
    root = Path(pilot_root)
    results = read_json(root / "results.json")
    expected = set(itertools.product(CONDITIONS, SEEDS))
    require(len(results) == 32, "Incomplete pilot: exactly 32 completed executions required; no inference computed")
    require({(item["condition"], item["seed"]) for item in results} == expected, "Missing/duplicate/unregistered pilot cell; no inference computed")
    require(all(item.get("complete") and item.get("backend") == "openrouter" for item in results), "Incomplete/non-API execution; no inference computed")
    for item in results:
        folder = root / "episodes" / f"{item['condition']}__cost__seed-{item['seed']}"
        require((folder / "summary.json").is_file() and (folder / "periods.json").is_file(), "Missing complete episode artifacts; no inference computed")
        require(not (folder / ".running.lock").exists(), "An episode is still running; no inference computed")
        require(item["metrics"].get("periods_evaluated") == 100 and item["metrics"].get("agent_periods") == 400, "Incomplete evaluation length; no inference computed")
    status = read_json(root / "batch_status.json")
    require(status.get("complete") and status.get("completed") == 32 and status.get("expected") == 32 and not status.get("errors"), "Batch did not finish successfully; no inference computed")

    manifest = read_json(root / "batch_manifest.json")
    require(manifest["backend"] == "openrouter", "Batch backend mismatch")
    require({item["id"] for item in manifest["conditions"]} == set(CONDITIONS), "Batch conditions mismatch")
    require(set(manifest["seeds"]) == set(SEEDS), "Batch seeds mismatch")
    config = manifest["simulation"]
    require(config["warmup_periods"] == 30 and config["evaluation_periods"] == 100, "Registered episode length mismatch")
    source = manifest["source_hash"]
    snapshot_code = root / "source_snapshot" / "scm"
    require(snapshot_code.is_dir(), "Missing frozen source snapshot")
    snapshot_hash = digest({path.name: path.read_text(encoding="utf-8") for path in sorted(snapshot_code.glob("*.py"))})
    require(snapshot_hash == source, "Frozen source snapshot hash differs from executed batch")
    current_code = Path(__file__).resolve().parents[1] / "scm"
    current_sources = {path.name: path.read_text(encoding="utf-8") for path in sorted(current_code.glob("*.py"))}
    preserved_sources = {path.name: path.read_text(encoding="utf-8") for path in sorted(snapshot_code.glob("*.py"))}
    current_hash = digest(current_sources)
    changed_current_files = sorted(name for name in set(current_sources) | set(preserved_sources) if current_sources.get(name) != preserved_sources.get(name))
    frozen_plan_path = root / "source_snapshot" / "docs" / "pilot_plan.json"
    plan_bytes = frozen_plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    require(tuple(plan["conditions"]) == CONDITIONS and tuple(plan["seeds"]) == SEEDS, "Registered pilot plan differs")
    require(plan["primary_metric"] == "cost_per_period" and plan["alpha"] == .05, "Registered primary analysis differs")

    warmups = {}
    for seed in SEEDS:
        saved = read_json(root / "warmup" / f"seed-{seed}.json")
        require(saved["source_hash"] == source, f"Warm-up source mismatch seed {seed}")
        require(saved["config_hash"] == digest(config), f"Warm-up config mismatch seed {seed}")
        require(saved["snapshot_hash"] == digest(saved["snapshot"]), f"Warm-up snapshot corruption seed {seed}")
        require(saved["demand_hash"] == digest(saved["demand"]), f"Demand path corruption seed {seed}")
        warmups[seed] = saved

    verified_costs = {}
    ledger_totals = Counter()
    usage_fields = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "reported_cost_usd")
    aggregate_known = Counter()
    aggregate_missing = Counter()
    checked_rows = 0
    expected_base_calls = 0
    response_ids = set()
    replayed_decisions = missing_latencies = 0
    historical_failures = []
    aggregate_budget_charge = 0.0
    for result in results:
        condition, seed = result["condition"], result["seed"]
        label = f"{condition}/seed-{seed}"
        folder = root / "episodes" / f"{condition}__cost__seed-{seed}"
        require(read_json(folder / "summary.json") == result, label + " result index differs from summary")
        require(result["source_hash"] == source, label + " source differs")
        require(result["warmup_hash"] == warmups[seed]["snapshot_hash"], label + " common warm-up differs")
        require(result["demand_hash"] == warmups[seed]["demand_hash"], label + " common demand differs")
        signature = read_json(folder / "manifest.json")["signature"]
        require(signature["source_hash"] == source and signature["config"] == config and signature["api"] == manifest["api"], label + " execution configuration differs")
        rows = read_json(folder / "periods.json")
        require(len(rows) == 400, label + " must have 400 rows")
        require({(row["t"], row["agent"]) for row in rows} == set(itertools.product(range(31, 131), range(4))), label + " incomplete/duplicate row grid")
        require(all(row["role"] == ROLES[row["agent"]] for row in rows), label + " agent/role mismatch")
        checkpoint = read_json(folder / "checkpoint.json")
        require(checkpoint["signature_hash"] == digest(signature) and checkpoint["warmup_hash"] == result["warmup_hash"], label + " checkpoint provenance mismatch")
        environment = checkpoint["environment"]
        require(environment["period_completed"] == 130 and environment["next_agent"] == 0, label + " checkpoint is not complete")
        checkpoint_rows = [row for agent in environment["agents"] for row in agent["history"] if row["t"] > 30]
        require(sorted(checkpoint_rows, key=lambda row: (row["t"], row["agent"])) == sorted(rows, key=lambda row: (row["t"], row["agent"])), label + " resumed checkpoint differs from final records")
        failure_path = folder / "failure.json"
        if failure_path.is_file():
            failure = read_json(failure_path)
            historical_failures.append({"condition": condition, "seed": seed, "failure_type": failure.get("type"), "at": failure.get("at"), "classification": "historical_interruption_superseded_by_verified_complete_summary"})
        metrics = result["metrics"]
        order_values = {role: [] for role in ROLES}
        components = Counter()
        all_costs = []
        new_demand, immediate = 0, 0
        fallback_count = 0
        llm_selections = Counter()
        local_replayed = local_missing_latency = 0
        for row in rows:
            require(type(row["Q_final"]) is int and 0 <= row["Q_final"] <= config["order_cap"], label + " invalid executed order")
            expected_components = {
                "holding_cost": row["I"] * config["holding_cost"],
                "backlog_cost": row["B"] * config["backlog_cost"],
                "variable_order_cost": row["Q_final"] * config["variable_order_cost"],
                "fixed_order_cost": (row["Q_final"] > 0) * config["fixed_order_cost"],
            }
            for key, value in expected_components.items():
                same_number(row[key], value, label + " row " + key)
                components[key] += value
            row_cost = sum(expected_components.values())
            same_number(row["total_cost"], row_cost, label + " row total")
            all_costs.append(row_cost)
            same_number(row["P_post"], row["P_pre"] + row["Q_final"], label + " post-order pipeline")
            order_values[row["role"]].append(row["Q_final"])
            if row["role"] == "retailer":
                require(row["D"] == warmups[seed]["demand"][row["t"] - 1], label + " observed customer demand differs")
                expected_immediate = min(row["D"], max(0, row["X"] - row["B_previous"]))
                same_number(row["immediate_filled"], expected_immediate, label + " immediate filling")
                new_demand += row["D"]
                immediate += expected_immediate
            fallback_count += row["fallback_count"]
            replayed = bool(row.get("latency_replayed", False))
            missing_latency = row.get("decision_latency_ms") is None
            require(replayed == missing_latency, label + " unclassified decision latency omission")
            local_replayed += replayed
            local_missing_latency += missing_latency
            if row.get("llm_selected_method") is not None:
                llm_selections[row["llm_selected_method"]] += 1
        total_cost = sum(all_costs)
        verified_costs[(condition, seed)] = total_cost / 100
        same_number(metrics["total_cost"], total_cost, label + " total cost")
        same_number(metrics["cost_per_period"], total_cost / 100, label + " period cost")
        for key, value in components.items():
            same_number(metrics["cost_components"][key], value, label + " cost component total")
        same_number(metrics["retailer_immediate_fill_rate"], immediate / new_demand if new_demand else None, label + " immediate fill rate")
        cvs = [independent_cv(order_values[role]) for role in ROLES]
        for role, cv in zip(ROLES, cvs):
            same_number(metrics["order_cv_i"][role], cv, label + " CV " + role)
        global_cv = None if any(value is None for value in cvs) else math.prod(cvs)
        same_number(metrics["global_bullwhip"], global_cv, label + " global CV product")
        same_number(metrics["fallback_count"], fallback_count, label + " fallback count")
        require(metrics["llm_selection_counts"] == dict(llm_selections), label + " LLM selection counts")
        require(metrics["replayed_decisions"] == local_replayed, label + " replayed decision count")
        require(metrics["decision_latency_ms"]["observed_decisions"] == 400 - local_missing_latency, label + " latency availability count")
        replayed_decisions += local_replayed
        missing_latencies += local_missing_latency
        checked_rows += len(rows)

        known, available = Counter(), Counter()
        attempts, decision_tasks = set(), set()
        key_tasks = {}
        task_attempts = {}
        calls = retries = failures = 0
        with (folder / "api_calls.jsonl").open(encoding="utf-8") as journal:
            for line in journal:
                event = json.loads(line)
                identity = (event["t"], event["agent"], event["task"], event["attempt"])
                require(identity not in attempts, label + " duplicate physical API attempt")
                attempts.add(identity)
                require(31 <= event["t"] <= 130 and event["agent"] in range(4), label + " call outside evaluation grid")
                require(event["condition"] == condition and event["objective"] == "cost", label + " API condition mismatch")
                decision_tasks.add(identity[:3])
                call_key = event["call_key"]
                require(call_key not in key_tasks or key_tasks[call_key] == identity[:3], label + " call key reused by different logical tasks")
                key_tasks[call_key] = identity[:3]
                prior = task_attempts.setdefault(identity[:3], [])
                require(event["attempt"] == len(prior) and len(prior) < 2, label + " noncontiguous or excess physical attempts")
                if prior:
                    require(not prior[-1]["success"] and prior[-1]["call_key"] == call_key, label + " duplicate generation after successful attempt or changed retry key")
                prior.append({"success": bool(event.get("success")), "call_key": call_key})
                response_id = event.get("response_id")
                if response_id:
                    require(response_id not in response_ids, label + " duplicate provider response identifier")
                    response_ids.add(response_id)
                aggregate_budget_charge += event["budget_charge_usd"]
                calls += 1
                retries += event["attempt"] > 0
                failures += bool(event.get("format_failure", False))
                for key in usage_fields:
                    value = event.get(key)
                    if value is not None:
                        require(type(value) in (int, float) and math.isfinite(value) and value >= 0, label + " invalid usage")
                        known[key] += value
                        available[key] += 1
                if event.get("success"):
                    require(event.get("actual_model") == manifest["api"]["model"] and event.get("actual_service_tier") == "flex" and event.get("actual_provider") == "Google AI Studio", label + " successful call routing mismatch")
        task_names = ("forecast", "order") if condition == "A" else (("select", "order") if condition == "C3M1T4" else ("order",))
        require(decision_tasks == set(itertools.product(range(31, 131), range(4), task_names)), label + " missing/unexpected decision task")
        require(len(key_tasks) == len(decision_tasks), label + " logical call key count mismatch")
        expected_base_calls += len(decision_tasks)
        same_number(metrics["api_calls"], calls, label + " API call count")
        same_number(metrics["retry_count"], retries, label + " retry count")
        same_number(metrics["format_failure_count"], failures, label + " format failure count")
        for key in usage_fields:
            expected_value = known[key] if available[key] == calls else None
            same_number(metrics[key], expected_value, label + " usage " + key)
            same_number(metrics[f"known_{key}_sum"], known[key], label + " known usage " + key)
            require(metrics[f"{key}_availability"]["reported_calls"] == available[key], label + " availability mismatch")
            aggregate_known[key] += known[key]
            aggregate_missing[key] += calls - available[key]
        ledger_totals.update(api_calls=calls, retries=retries, format_failures=failures, fallbacks=fallback_count)

    require(expected_base_calls == 19200, "Expected logical call count differs from 19200")
    require(ledger_totals["api_calls"] == expected_base_calls + ledger_totals["retries"], "Physical call count indicates missing or duplicate generation")
    same_number(status["budget_accounted_usd"], aggregate_budget_charge, "Resumed batch budget accounting")

    # Only now, after all 32 complete trajectories pass arithmetic/provenance checks,
    # compare the pre-planned primary inferential calculations.
    analysis = read_json(analysis_path)
    require(analysis["complete"] and analysis["completed_executions"] == 32, "Primary analysis incomplete")
    require(analysis["protocol_sha256"] == hashlib.sha256(plan_bytes).hexdigest(), "Analysis protocol hash mismatch")
    require(analysis["provenance"]["source_hash"] == source and analysis["provenance"]["verified_actual_api"], "Analysis provenance mismatch")
    by_condition = {item["condition"]: item for item in analysis["primary_contrasts"]}
    require(len(analysis["primary_contrasts"]) == 3 and set(by_condition) == set(CONDITIONS[1:]), "Wrong primary contrast family")
    raw_p = []
    audit_contrasts = []
    for condition in CONDITIONS[1:]:
        differences = [verified_costs[(condition, seed)] - verified_costs[("A", seed)] for seed in SEEDS]
        calculated_p = independent_exact_p(differences)
        raw_p.append(calculated_p)
        stated = by_condition[condition]
        require(stated["paired_seeds"] == list(SEEDS) and stated["n_pairs"] == 8 and stated["reference"] == "A", "Paired seed unit mismatch")
        same_number(stated["mean_difference"], statistics.fmean(differences), "Primary mean difference " + condition)
        same_number(stated["p_value"], calculated_p, "Primary exact p " + condition)
        require(stated["sign_patterns"] == 256, "Wrong exact sign enumeration")
        audit_contrasts.append({"condition": condition, "mean_difference": statistics.fmean(differences), "p_value": calculated_p})
    adjusted_p = independent_holm(raw_p)
    for item, adjusted in zip(audit_contrasts, adjusted_p):
        stated = by_condition[item["condition"]]
        same_number(stated["p_value_holm"], adjusted, "Primary Holm p " + item["condition"])
        require(stated["reject_holm_0_05"] == (adjusted <= .05), "Holm conclusion mismatch")
        item["p_value_holm"] = adjusted
    return {
        "audit_status": "passed", "independent_implementation": True,
        "episodes_checked": 32, "agent_period_rows_checked": checked_rows,
        "source_hash": source, "paired_seed_provenance_checked": 8,
        "current_delivered_source_hash": current_hash,
        "current_source_matches_execution": current_hash == source,
        "current_source_files_changed_since_execution": changed_current_files,
        "frozen_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "checks": ["complete registered grid", "four cost components and period denominator", "immediate fill excludes prior backlog", "sample CV and four-stage product", "source and common warmup/demand integrity", "physical API-attempt ledger and nullable usage", "independent exact paired p-values and Holm correction"],
        "ledger_totals": dict(ledger_totals),
        "known_usage_totals": dict(aggregate_known),
        "missing_usage_calls": dict(aggregate_missing),
        "resume_checks": {"expected_logical_calls": expected_base_calls, "duplicate_generation_detected": False, "unique_provider_response_ids": len(response_ids), "replayed_decisions": replayed_decisions, "missing_decision_latencies": missing_latencies, "latency_observed_decisions": checked_rows - missing_latencies, "historical_failure_files": historical_failures, "budget_accounted_usd": aggregate_budget_charge},
        "primary_contrast_checks": audit_contrasts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_root", type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = audit(args.pilot_root, args.analysis)
    except (ValueError, KeyError, OSError, TypeError) as error:
        print(json.dumps({"audit_status": "not_passed", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
