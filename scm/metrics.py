"""Evaluation over complete episodes and physical API-attempt ledgers."""

from collections import Counter, defaultdict
import math
import statistics
from typing import Mapping

from .config import ROLES


def _get(config, key):
    return config[key] if isinstance(config, Mapping) else getattr(config, key)


def _finite(value, label, *, nonnegative=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    if not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"{label} must be finite and nonnegative")
    return float(value)


def _integer(value, label):
    number = _finite(value, label)
    if number != int(number):
        raise ValueError(f"{label} must be an integer")
    return int(number)


def _percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _sample_cv(values):
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    return statistics.stdev(values) / mean if mean != 0 else None


def _usage(events):
    result = {"api_calls": len(events)}
    for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "reported_cost_usd"):
        observed = []
        for event in events:
            value = event.get(key)
            if value is not None:
                number = _finite(value, key)
                if key.endswith("tokens") and number != int(number):
                    raise ValueError(f"{key} must be an integer")
                observed.append(number)
        total = math.fsum(observed)
        if key.endswith("tokens"):
            total = int(total)
        available = len(observed)
        result[key] = total if available == len(events) else None
        result[f"known_{key}_sum"] = total
        result[f"{key}_availability"] = {
            "reported_calls": available,
            "total_calls": len(events),
            "missing_calls": len(events) - available,
            "fraction": available / len(events) if events else 1.0,
        }
    result["retry_count"] = sum(_integer(event.get("attempt", 0), "attempt") > 0 for event in events)
    result["format_failure_count"] = sum(bool(event.get("format_failure", False)) for event in events)
    result["failed_attempt_count"] = sum(event.get("success") is False for event in events)
    result["error_types"] = dict(Counter(str(event["error_type"]) for event in events if event.get("error_type")))
    durations = [_finite(event["latency_seconds"], "latency_seconds") for event in events if event.get("latency_seconds") is not None]
    result["api_latency_seconds_sum"] = math.fsum(durations) if len(durations) == len(events) else None
    result["known_api_latency_seconds_sum"] = math.fsum(durations)
    return result


def summarize(rows, api_events, config):
    """Return metrics for exactly four agents in every planned evaluation period.

    Warm-up rows are ignored, never used to change the planned denominator.
    Usage totals include each supplied evaluation API attempt exactly once.
    Missing provider usage is null; the known subtotal and coverage are separate.
    """
    first = _integer(_get(config, "warmup_periods"), "warmup_periods") + 1
    count = _integer(_get(config, "evaluation_periods"), "evaluation_periods")
    if count < 1:
        raise ValueError("evaluation_periods must be positive")
    expected_times = set(range(first, first + count))
    records = []
    identities = set()
    agent_roles = {}
    role_agents = {}
    for raw in rows:
        t = _integer(raw.get("t"), "row.t")
        if t not in expected_times:
            if t >= first:
                raise ValueError("Row lies beyond the registered evaluation window")
            continue
        role = raw.get("role")
        agent = raw.get("agent")
        if role not in ROLES or agent is None:
            raise ValueError("Every evaluation row requires a registered role and agent")
        key = (t, role)
        if key in identities:
            raise ValueError(f"Duplicate evaluation row: {key}")
        identities.add(key)
        if agent in agent_roles and agent_roles[agent] != role:
            raise ValueError("Agent-to-role mapping changed within episode")
        if role in role_agents and role_agents[role] != agent:
            raise ValueError("Role-to-agent mapping changed within episode")
        agent_roles[agent] = role
        role_agents[role] = agent
        records.append(raw)
    expected = {(t, role) for t in expected_times for role in ROLES}
    if identities != expected:
        missing = sorted(expected - identities)
        raise ValueError(f"Incomplete evaluation: expected {4 * count} unique rows; missing {missing[:8]}")

    totals = Counter()
    orders = defaultdict(list)
    forecasts = Counter()
    llm_selections = Counter()
    retailer_demand = []
    retailer_immediate = []
    deviations = []
    latencies = []
    fallback_decisions = 0
    fallback_count = 0
    fallback_by_stage = Counter()
    corrections = 0
    replayed_decisions = 0
    arithmetic_error_count = 0
    arithmetic_error_decisions = 0
    cost_names = ("holding_cost", "backlog_cost", "variable_order_cost", "fixed_order_cost")
    for row in sorted(records, key=lambda item: (item["t"], ROLES.index(item["role"]))):
        inventory = _finite(row.get("I"), "I")
        backlog = _finite(row.get("B"), "B")
        q = _finite(row.get("Q_final"), "Q_final")
        costs = {key: _finite(row.get(key), key) for key in cost_names}
        expected_costs = {
            "holding_cost": inventory * _finite(_get(config, "holding_cost"), "holding_cost coefficient"),
            "backlog_cost": backlog * _finite(_get(config, "backlog_cost"), "backlog_cost coefficient"),
            "variable_order_cost": q * _finite(_get(config, "variable_order_cost"), "variable_order_cost coefficient"),
            "fixed_order_cost": (q > 0) * _finite(_get(config, "fixed_order_cost"), "fixed_order_cost coefficient"),
        }
        if any(not math.isclose(costs[key], expected_costs[key], rel_tol=1e-10, abs_tol=1e-10) for key in cost_names):
            raise ValueError("Logged cost components do not match state, executed order and configured coefficients")
        period_cost = _finite(row.get("total_cost"), "total_cost")
        if not math.isclose(period_cost, math.fsum(costs.values()), rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError("total_cost does not equal its four components")
        for key, value in costs.items():
            totals[key] += value
        totals["total_cost"] += period_cost
        totals["inventory"] += inventory
        totals["backlog"] += backlog
        orders[row["role"]].append(q)
        if row["role"] == "retailer":
            demand = _finite(row.get("D"), "retailer D")
            filled = _finite(row.get("immediate_filled"), "immediate_filled")
            if filled > demand:
                raise ValueError("Immediate fulfillment cannot exceed new demand")
            retailer_demand.append(demand)
            retailer_immediate.append(filled)
        if row.get("Q_base") is not None:
            deviations.append(abs(q - _finite(row["Q_base"], "Q_base")))
        if row.get("decision_latency_ms") is not None:
            latencies.append(_finite(row["decision_latency_ms"], "decision_latency_ms"))
        stages = {stage: bool(row.get(f"{stage}_fallback", False)) for stage in ("forecast", "order", "message")}
        this_fallback = _integer(row.get("fallback_count", sum(stages.values())), "fallback_count")
        fallback_count += this_fallback
        fallback_decisions += this_fallback > 0
        fallback_by_stage.update({stage: int(value) for stage, value in stages.items()})
        corrections += _integer(row.get("correction_count", 0), "correction_count")
        replayed_decisions += bool(row.get("latency_replayed", False))
        arithmetic_error_count += _integer(row.get("arithmetic_error_count", 0), "arithmetic_error_count")
        arithmetic_error_decisions += bool(row.get("arithmetic_error", False))
        method = row.get("selected_method", row.get("forecast_method"))
        if method:
            forecasts[str(method)] += 1
        if row.get("llm_selected_method") is not None:
            llm_selections[str(row["llm_selected_method"])] += 1

    events = []
    attempt_identities = set()
    for event in api_events:
        t = _integer(event.get("t"), "event.t")
        if t not in expected_times:
            if t >= first:
                raise ValueError("API event lies beyond evaluation window")
            continue
        if event.get("agent") not in agent_roles:
            raise ValueError("API event refers to an unknown agent")
        attempt = _integer(event.get("attempt", 0), "attempt")
        if not event.get("task"):
            raise ValueError("API event requires a task name")
        identity = (t, event["agent"], event["task"], attempt)
        if identity in attempt_identities:
            raise ValueError("Duplicate physical API attempt in ledger")
        attempt_identities.add(identity)
        events.append(event)
    usage = _usage(events)
    task_events = defaultdict(list)
    for event in events:
        task_events[event["task"]].append(event)
    cvs = {role: _sample_cv(orders[role]) for role in ROLES}
    total_demand = math.fsum(retailer_demand)
    result = {
        "status": "completed",
        "periods_evaluated": count,
        "evaluation_start": first,
        "evaluation_end": first + count - 1,
        "agent_periods": len(records),
        "total_cost": totals["total_cost"],
        "cost_per_period": totals["total_cost"] / count,
        "cost_components": {key: totals[key] for key in cost_names},
        "cost_components_per_period": {key: totals[key] / count for key in cost_names},
        "retailer_immediate_fill_rate": math.fsum(retailer_immediate) / total_demand if total_demand else None,
        "retailer_total_new_demand": total_demand,
        "retailer_total_immediate_filled": math.fsum(retailer_immediate),
        "order_cv_i": cvs,
        "global_bullwhip": math.prod(cvs.values()) if all(value is not None for value in cvs.values()) else None,
        "order_cv_ddof": 1,
        "global_bullwhip_definition": "product_of_four_sample_final_order_CVs",
        "decision_latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "observed_decisions": len(latencies),
            "total_decisions": len(records),
        },
        "fallback_count": fallback_count,
        "fallback_decisions": fallback_decisions,
        "fallback_decision_rate": fallback_decisions / len(records),
        "fallback_by_stage": dict(fallback_by_stage),
        "correction_count": corrections,
        "replayed_decisions": replayed_decisions,
        "arithmetic_error_count": arithmetic_error_count,
        "arithmetic_error_decisions": arithmetic_error_decisions,
        "forecast_method_counts": dict(forecasts),
        "llm_selection_counts": dict(llm_selections),
        "mean_absolute_order_adjustment": statistics.fmean(deviations) if deviations else None,
        "order_adjustment_observations": len(deviations),
        "mean_inventory_per_agent_period": totals["inventory"] / len(records),
        "mean_backlog_per_agent_period": totals["backlog"] / len(records),
        "terminal_state": {
            row["role"]: {key: row.get(key) for key in ("I", "B", "P_pre", "P_post")}
            for row in records if row["t"] == first + count - 1
        },
        "usage_by_task": {task: _usage(values) for task, values in sorted(task_events.items())},
    }
    result.update(usage)
    result["retry_decision_rate"] = len({(event["t"], event["agent"]) for event in events if event.get("attempt", 0) > 0}) / len(records)
    return result
