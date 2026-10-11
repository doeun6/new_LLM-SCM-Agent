"""Pre-registered paired-seed pilot analysis without third-party packages."""

from fractions import Fraction
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import statistics

from .config import ROLES


PLAN_PATH = Path(__file__).resolve().parents[1] / "docs" / "pilot_plan.json"
# This analyzer belongs to the completed, immutable pilot. Its historical A
# included a T0 forecast and OUT; the current registry's direct-order A does not.
LEGACY_PILOT_SOURCE_SHA256 = "e6c3297ea3398bd84552313f2fa5c3fd69d43fe8e0064d1bcba6037c8eb59525"
LEGACY_PILOT_OBJECTIVE = "cost"
LEGACY_BASELINE_DEFINITION = {
    "logged_condition": "A",
    "factorial_condition": "C0M0T0",
    "forecast": "T0_LLM_30_incoming_orders_next_two_periods",
    "reference_quantity": "order_up_to_from_forecast_and_inventory_position",
    "final_order_inputs": "C0_M0_Q_base",
    "direct_order": False,
}
LEGACY_NOTICE = (
    "이 분석은 과거 4조건×8개 seed 예비실험 전용입니다. 당시 A는 C0M0T0 "
    "(30기 T0 예측 → OUT 기준 주문량 → 최종 주문)이었으며, 현재의 C0·당기 수주만으로 "
    "직접 주문하는 A의 결과가 아닙니다. 현재 A와 새 실험에는 별도의 분석 계획이 필요합니다."
)


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def paired_sign_flip(differences):
    """Exact two-sided test; one sign per paired seed, never per period."""
    values = [_number(value, "paired difference") for value in differences]
    if not values or len(values) > 20:
        raise ValueError("Exact enumeration requires between 1 and 20 pairs")
    exact = [Fraction(str(value)) for value in values]
    threshold = abs(sum(exact))
    extreme = sum(
        abs(sum(sign * value for sign, value in zip(signs, exact))) >= threshold
        for signs in itertools.product((-1, 1), repeat=len(values))
    )
    patterns = 2 ** len(values)
    return {
        "n_pairs": len(values),
        "mean_difference": statistics.fmean(values),
        "sign_patterns": patterns,
        "extreme_patterns": extreme,
        "p_value": extreme / patterns,
    }


def holm_adjust(p_values):
    """Return monotone Holm-adjusted p-values with original keys retained."""
    checked = {key: _number(value, "p-value") for key, value in p_values.items()}
    if any(value < 0 or value > 1 for value in checked.values()):
        raise ValueError("p-values must be between zero and one")
    ordered = sorted(checked, key=lambda key: checked[key])
    running = 0.0
    adjusted = {}
    for index, key in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - index) * checked[key]))
        adjusted[key] = running
    return {key: adjusted[key] for key in checked}


def _quantile(ordered, probability):
    position = (len(ordered) - 1) * probability
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def paired_bootstrap_interval(differences, *, resamples=10000, random_seed=20261011, confidence=0.95):
    """Descriptive percentile interval, resampling complete paired differences."""
    values = [_number(value, "paired difference") for value in differences]
    if not values or not isinstance(resamples, int) or resamples < 1 or not 0 < confidence < 1:
        raise ValueError("Invalid bootstrap configuration")
    rng = random.Random(random_seed)
    samples = sorted(statistics.fmean(values[rng.randrange(len(values))] for _ in values) for _ in range(resamples))
    tail = (1 - confidence) / 2
    return {
        "low": _quantile(samples, tail),
        "high": _quantile(samples, 1 - tail),
        "confidence": confidence,
        "method": "paired_seed_percentile_bootstrap",
        "resamples": resamples,
        "random_seed": random_seed,
        "descriptive_only": True,
    }


def _descriptive(values):
    known = [_number(value, "metric") for value in values if value is not None]
    return {
        "mean": statistics.fmean(known) if known else None,
        "sample_sd": statistics.stdev(known) if len(known) > 1 else None,
        "known_n": len(known),
        "missing_n": len(values) - len(known),
    }


def analyze(results):
    """Analyze only the immutable legacy 4-condition, 8-seed pilot.

    All submitted executions must be complete. Missing executions are displayed
    explicitly, and all inferential contrasts wait for the complete 32-cell set.
    """
    results = list(results)
    actual_api = any(item.get("backend") == "openrouter" for item in results)
    provenance = {"verified_actual_api": False, "source_hash": None, "paired_seed_hashes": {}}
    if actual_api:
        if any(item.get("backend") != "openrouter" for item in results):
            raise ValueError("Actual API executions cannot be combined with mock or unlabelled executions")
        for item in results:
            if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("source_hash", "warmup_hash", "demand_hash")):
                raise ValueError("Actual API executions require source, warm-up and demand hashes")
        sources = {item["source_hash"] for item in results}
        if len(sources) != 1:
            raise ValueError("Actual API executions have different source hashes")
        if sources != {LEGACY_PILOT_SOURCE_SHA256}:
            raise ValueError(
                "This analyzer accepts only the legacy pilot source hash "
                f"{LEGACY_PILOT_SOURCE_SHA256}. Its A was C0M0T0 with forecast and OUT, "
                "not the current direct-order A; register a separate analysis for new executions."
            )
        if any(item.get("objective") != LEGACY_PILOT_OBJECTIVE for item in results):
            raise ValueError("The legacy actual-API pilot requires an explicit cost objective for every execution")
        seed_hashes = {}
        for item in results:
            seed = item.get("seed")
            hashes = {"warmup_hash": item["warmup_hash"], "demand_hash": item["demand_hash"]}
            if seed in seed_hashes and seed_hashes[seed] != hashes:
                raise ValueError(f"Unmatched warm-up snapshot or demand path for paired seed {seed}")
            seed_hashes[seed] = hashes
        provenance = {"verified_actual_api": True, "source_hash": next(iter(sources)), "paired_seed_hashes": seed_hashes}
    plan_bytes = PLAN_PATH.read_bytes()
    plan = json.loads(plan_bytes)
    conditions, seeds = plan["conditions"], plan["seeds"]
    expected = {(condition, seed) for condition in conditions for seed in seeds}
    by_key = {}
    for result in results:
        condition, seed = result.get("condition"), result.get("seed")
        if result.get("objective", LEGACY_PILOT_OBJECTIVE) != LEGACY_PILOT_OBJECTIVE:
            raise ValueError("Executions with a different objective are outside the legacy cost-objective pilot")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("Each seed must be an integer")
        key = (condition, seed)
        if key not in expected:
            raise ValueError(f"Execution outside the registered pilot: {key}")
        if key in by_key:
            raise ValueError(f"Duplicate execution: {key}")
        metrics = result.get("metrics", {})
        if metrics.get("status") != "completed":
            raise ValueError(f"Submit only complete episode metrics; list interrupted executions as missing: {key}")
        if metrics.get("periods_evaluated") != plan["evaluation_periods"] or metrics.get("agent_periods") != 4 * plan["evaluation_periods"]:
            raise ValueError(f"Execution has the wrong registered evaluation length: {key}")
        cost = _number(metrics.get("cost_per_period"), "cost_per_period")
        if cost < 0:
            raise ValueError("cost_per_period cannot be negative")
        by_key[key] = metrics

    missing = [{"condition": condition, "seed": seed} for condition in conditions for seed in seeds if (condition, seed) not in by_key]
    complete = not missing
    summaries = {}
    scalar_metrics = (
        "cost_per_period", "total_cost", "retailer_immediate_fill_rate", "global_bullwhip",
        "input_tokens", "output_tokens", "total_tokens", "reported_cost_usd",
        "retry_count", "format_failure_count", "fallback_count", "api_calls",
    )
    for condition in conditions:
        available_seeds = [seed for seed in seeds if (condition, seed) in by_key]
        observations = [by_key[(condition, seed)] for seed in available_seeds]
        summary = {
            "n": len(observations),
            "seeds": available_seeds,
            "metrics": {metric: _descriptive([item.get(metric) for item in observations]) for metric in scalar_metrics},
            "order_cv_i": {role: _descriptive([item.get("order_cv_i", {}).get(role) for item in observations]) for role in ROLES},
            "decision_latency_ms_mean": _descriptive([item.get("decision_latency_ms", {}).get("mean") for item in observations]),
        }
        summaries[condition] = summary

    contrasts = []
    for planned in plan["primary_contrasts"]:
        condition, reference = planned["condition"], planned["reference"]
        paired_seeds = [seed for seed in seeds if (condition, seed) in by_key and (reference, seed) in by_key]
        differences = [by_key[(condition, seed)]["cost_per_period"] - by_key[(reference, seed)]["cost_per_period"] for seed in paired_seeds]
        comparison = {
            "condition": condition,
            "reference": reference,
            "metric": plan["primary_metric"],
            "direction": "condition_minus_reference",
            "paired_seeds": paired_seeds,
            "n_pairs": len(paired_seeds),
            "paired_differences": differences,
            "mean_difference": statistics.fmean(differences) if differences else None,
            "percent_difference": None,
            "p_value": None,
            "p_value_holm": None,
            "reject_holm_0_05": None,
            "bootstrap_95_ci": None,
            "inference_status": "complete" if complete else "withheld_incomplete_registered_pilot",
        }
        if paired_seeds:
            baseline_mean = statistics.fmean(by_key[(reference, seed)]["cost_per_period"] for seed in paired_seeds)
            comparison["percent_difference"] = 100 * comparison["mean_difference"] / baseline_mean if baseline_mean else None
        if complete:
            comparison.update(paired_sign_flip(differences))
            interval = plan["interval"]
            comparison["bootstrap_95_ci"] = paired_bootstrap_interval(
                differences, resamples=interval["resamples"], random_seed=interval["random_seed"], confidence=interval["confidence"]
            )
        contrasts.append(comparison)
    if complete:
        adjusted = holm_adjust({item["condition"]: item["p_value"] for item in contrasts})
        for item in contrasts:
            item["p_value_holm"] = adjusted[item["condition"]]
            item["reject_holm_0_05"] = item["p_value_holm"] <= plan["alpha"]

    return {
        "protocol_id": plan["protocol_id"],
        "protocol_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "protocol_scope": "legacy_pilot_only",
        "required_actual_api_source_hash": LEGACY_PILOT_SOURCE_SHA256,
        "objective": LEGACY_PILOT_OBJECTIVE,
        "reference_definition": dict(LEGACY_BASELINE_DEFINITION),
        "legacy_notice": LEGACY_NOTICE,
        "complete": complete,
        "planned_executions": len(expected),
        "completed_executions": len(by_key),
        "planned_seeds": seeds,
        "missing_executions": missing,
        "unit_of_analysis": "paired_external_demand_seed",
        "provenance": provenance,
        "alpha": plan["alpha"],
        "conditions": summaries,
        "primary_contrasts": contrasts,
        "limitations": [
            LEGACY_NOTICE,
            "탐색적 예비실험이며, 8개 seed의 결과로 전체 C×M×T 효과나 상호작용을 추론할 수 없습니다.",
            "검정의 표본 단위는 같은 수요 seed로 짝지은 실행입니다. 100개 평가 기간은 독립 반복 표본이 아닙니다.",
            "정확 부호 반전 검정은 귀무가설에서 seed별 차이의 부호 교환 가능성을 가정합니다.",
            "95% bootstrap 구간은 기술적 참고 구간이며, 표본 8개에서 정밀한 포괄률을 보장하지 않습니다.",
            "세 가지 비용 비교에만 Holm 보정을 적용합니다. 서비스율·채찍효과·운영 지표는 기술통계입니다.",
            "유리하거나 유의한 결과가 나올 때까지 seed를 추가하거나 조건을 조정하지 않습니다.",
        ],
    }


def _fmt(value, places=4):
    return "미제공/정의 불가" if value is None else f"{value:.{places}f}"


def make_report(data):
    """Render all registered conditions and contrasts in Korean Markdown."""
    lines = [
        "# SCM LLM Agent 과거 예비실험 결과",
        "",
        f"**{data.get('legacy_notice', LEGACY_NOTICE)}**",
        "",
        f"사전 계획 {data['planned_executions']}회 중 {data['completed_executions']}회 완료. 분석 단위는 공통 수요 seed이며, 조건마다 사전에 정한 8개 seed를 사용합니다.",
        "",
        "기간당 비용은 4개 에이전트의 평가기간 비용 합계를 100기로 나눈 값입니다. 보유·백로그·변동 발주·고정 발주 비용을 포함합니다.",
        "",
    ]
    if not data["complete"]:
        lines.extend([
            "**계획된 실행이 미완료되어 검정과 신뢰구간을 산출하지 않았습니다.** 아래 값은 완료된 실행의 기술통계입니다.",
            "",
            "미완료: " + ", ".join(f"{item['condition']} / seed {item['seed']}" for item in data["missing_executions"]),
            "",
        ])
    lines.extend([
        "| 조건 | 완료 seed 수 | 기간당 비용 평균 (표본 SD) | 즉시 충족률 평균 | Global bullwhip 평균 |",
        "|---|---:|---:|---:|---:|",
    ])
    for condition, item in data["conditions"].items():
        metrics = item["metrics"]
        cost = metrics["cost_per_period"]
        lines.append(f"| {condition} | {item['n']} | {_fmt(cost['mean'])} ({_fmt(cost['sample_sd'])}) | {_fmt(metrics['retailer_immediate_fill_rate']['mean'])} | {_fmt(metrics['global_bullwhip']['mean'])} |")
    lines.extend([
        "",
        "차이는 해당 조건 − A이며, 음수는 비용 감소를 뜻합니다. 아래 세 비교를 모두 보고합니다.",
        "",
        "| 비교 | 짝 수 | 평균 비용 차이 | 차이율 (%) | 기술적 95% bootstrap 구간 | p | Holm 보정 p |",
        "|---|---:|---:|---:|---|---:|---:|",
    ])
    for item in data["primary_contrasts"]:
        ci = item["bootstrap_95_ci"]
        interval = f"[{_fmt(ci['low'])}, {_fmt(ci['high'])}]" if ci else "미산출"
        lines.append(f"| {item['condition']} − {item['reference']} | {item['n_pairs']} | {_fmt(item['mean_difference'])} | {_fmt(item['percent_difference'], 2)} | {interval} | {_fmt(item['p_value'], 6)} | {_fmt(item['p_value_holm'], 6)} |")
    lines.extend([
        "",
        "| 조건 | API 호출 수 평균 | 전체 토큰 평균 | 보고 API 비용 평균 (USD) | 재시도 평균 | 형식/검증 실패 평균 | fallback 평균 | 의사결정 지연 평균 (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for condition, item in data["conditions"].items():
        metrics = item["metrics"]
        cells = [_fmt(metrics[key]["mean"], 4 if key == "reported_cost_usd" else 2) for key in ("api_calls", "total_tokens", "reported_cost_usd", "retry_count", "format_failure_count", "fallback_count")]
        lines.append(f"| {condition} | " + " | ".join(cells) + f" | {_fmt(item['decision_latency_ms_mean']['mean'], 2)} |")
    lines.extend([
        "",
        "API 사용량은 실패·재시도를 포함한 물리적 생성 호출을 집계합니다. 미보고 사용량은 0으로 대체하지 않습니다. 표의 평균은 값이 확인된 실행에 한정되며, 정확한 제공/누락 수는 분석 JSON에 기록합니다.",
        "",
    ])
    lines.extend(f"- {item}" for item in data["limitations"])
    lines.extend(["", f"사전 계획 SHA-256: `{data['protocol_sha256']}`", ""])
    return "\n".join(lines)
