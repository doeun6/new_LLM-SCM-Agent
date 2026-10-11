"""C/M/T treatment execution with explicit, stateless information boundaries."""

from copy import deepcopy
import math
import re
import statistics
import time

from .config import ROLES, canonical_hash
from .forecasting import METHODS, forecast, normalize_order, order_up_to
from .provider import RecoverableCallError
from . import prompts


STATE_FIELDS = ("D", "I", "B", "P_pre")
WINDOWS = {"C0": 10, "C1": 20, "C2": 5, "C3": 10}
# C3 order statistics use the same t-9..t-1 past orders that C0 shows as raw rows.
ORDER_STAT_FIELDS = ("Q_base", "Q_final", "Q_final_minus_Q_base")
ORDER_STAT_DEFINITIONS = {
    "Q_base": "reference support quantity calculated by the code in that period",
    "Q_final": "order actually executed and sent upstream in that period",
    "Q_final_minus_Q_base": ("executed order minus reference support in the same period; "
                             "positive means the executed order exceeded the support"),
}


def _summary(values):
    return {"mean": statistics.mean(values),
            "standard_deviation": statistics.stdev(values),
            "minimum": min(values), "maximum": max(values)}


def _order_statistics(past):
    """Past-order summary for C3; null when any past reference is unavailable."""
    series = {"Q_final": [row["Q_final"] for row in past]}
    if any(row["Q_base"] is None for row in past):
        series.update(Q_base=None, Q_final_minus_Q_base=None)
    else:
        series["Q_base"] = [row["Q_base"] for row in past]
        series["Q_final_minus_Q_base"] = [row["Q_final"]-row["Q_base"] for row in past]
    return {key: None if series[key] is None else _summary(series[key])
            for key in ORDER_STAT_FIELDS}


def build_C(level, history, currentrow):
    """Current-inclusive state windows; past orders have one fewer observation."""
    if level not in WINDOWS:
        raise ValueError(f"Unknown C level {level!r}")
    n = WINDOWS[level]
    t = currentrow["t"]
    past = [row for row in history if row["t"] < t][-(n-1):]
    if [row["t"] for row in past] != list(range(t-n+1, t)):
        raise ValueError("C requires contiguous completed local history ending at t-1")
    current = {key: currentrow[key] for key in STATE_FIELDS}
    rows = [*past, currentrow]
    result = {"current": {"t": t, **current}}
    if level == "C3":
        result.update({
            "representation": "statistics", "window": 10,
            "period_start": t-9, "period_end": t,
            "statistics": {key: _summary([row[key] for row in rows]) for key in STATE_FIELDS},
            # The current order is not yet decided, so order statistics end at t-1.
            "order_window": len(past),
            "order_period_start": t-len(past), "order_period_end": t-1,
            "order_definitions": dict(ORDER_STAT_DEFINITIONS),
            "order_statistics": _order_statistics(past),
        })
    else:
        result.update({
            "representation": "raw", "window": n,
            "states": [{"t": row["t"], **{key: row[key] for key in STATE_FIELDS}}
                       for row in rows],
            "past_orders": [{"t": row["t"], "Q_base": row["Q_base"],
                             "Q_final": row["Q_final"]} for row in past],
        })
    return result


build_context = build_C


def _schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


FORECAST_SCHEMA = _schema({"forecast_h1": {"type": "number"},
                           "forecast_h2": {"type": "number"}})
SELECTION_SCHEMA = _schema({"method": {"type": "string", "enum": list(METHODS)}})
ORDER_SCHEMA = _schema({"order_quantity": {"type": "number"},
                       "rationale": {"type": "string"}})
MESSAGE_SCHEMA = _schema({"message": {"type": "string"}})
PLAN_FIELDS = ("plan_t_plus_1", "plan_t_plus_2")


def plan_schema(order_cap):
    """M3: the sender's own planned order quantities for t+1 and t+2."""
    quantity = {"type": "integer", "minimum": 0, "maximum": order_cap}
    return _schema({key: dict(quantity) for key in PLAN_FIELDS})


def _validate_message(value, level, order_cap=None):
    if level == "M3":
        # validate_schema checks the integer type but not minimum/maximum,
        # so the range is enforced here and shares the common retry budget.
        if order_cap is None:
            raise ValueError("M3 validation requires the order cap")
        for key in PLAN_FIELDS:
            quantity = value[key]
            if type(quantity) is not int:
                raise ValueError(f"M3 {key} must be an integer order quantity")
            if not 0 <= quantity <= order_cap:
                raise ValueError(f"M3 {key} must be between 0 and {order_cap}")
        return
    if not value["message"].strip():
        raise ValueError("Message fields must not be empty")
    if level == "M2":
        message = value["message"]
        if re.search(r"\d", message):
            raise ValueError("M2 permits qualitative reasons only, without numeric quantities")
        if re.search(r"\b(?:I|we)\s+(?:will|shall|plan\b|intend\b)|\bnext\s+period\s+(?:I|we)\b",
                     message, re.IGNORECASE):
            raise ValueError("M2 must not include the sender's future action plans")


def _event_counts(events):
    """The provider's ledger is authoritative; do not count ordinary stages as retries."""
    result = {"api_call_count": len(events), "retry_count": 0,
              "format_failure_count": 0, "api_error_count": 0}
    for event in events:
        result["retry_count"] += int(event.get("attempt", 0) > 0)
        result["format_failure_count"] += int(bool(event.get("format_failure", False)))
        result["api_error_count"] += int(event.get("success") is False and
                                         not event.get("format_failure", False))
    for field in ("input_tokens", "output_tokens", "total_tokens", "reported_cost_usd"):
        values = [event.get(field) for event in events]
        available = [value for value in values if value is not None]
        result[field] = sum(available) if len(available) == len(values) else None
        result[field + "_reported_subtotal"] = sum(available)
        result[field + "_missing_count"] = len(values)-len(available)
    return result


class Harness:
    def __init__(self, config, condition, provider):
        self.config = config
        self.condition = condition
        self.provider = provider

    def decide(self, env, i, row, received_message):
        """Finish one already-begun agent exactly once, then construct its message."""
        started = time.perf_counter()
        t, role = row["t"], ROLES[i]
        condition = self.condition
        replayed = any((event.get("t"), event.get("agent"), event.get("condition")) ==
                       (t, i, condition.id) for event in self.provider.events)
        state = env.agents[i]
        context = build_C(condition.c, state.history, row)
        message = None if condition.m == "M0" else deepcopy(received_message)
        # A has no forecasting stage, including on failure. Its authorized C0 is
        # built from local records; the separate thirty-request series is never read.
        history = None
        if not condition.direct_order:
            history = list(state.demands[-self.config.forecast_window:])
            if len(history) != 30:
                raise ValueError("Every forecasting condition requires exactly thirty requests")
        meta = {"t": t, "agent": i, "role": role, "condition": condition.id,
                "objective": condition.objective, "message_level": condition.m,
                "tool_level": condition.t, "direct_order": condition.direct_order}
        diagnostics = {
            "context_sha256": canonical_hash(context), "context_payload": context,
            "received_message": message, "selected_method": None,
            "llm_selected_method": None,
            "forecast_fallback": False, "order_fallback": False,
            "message_fallback": False, "fallback_count": 0,
            "correction_count": 0, "forecast_correction_count": 0,
            "arithmetic_error": False, "arithmetic_error_count": 0,
            "forecast_arithmetic_error": None,
        }

        def ask(task, instructions, payload, schema, **kwargs):
            return self.provider.ask(task, instructions, payload, schema, meta, **kwargs)

        if condition.direct_order:
            raw_support = None
            diagnostics.update(forecast=None, forecast_raw=None, support_policy=None)
        elif condition.reference:
            if condition.reference == "LR30":
                values = forecast("linear_regression", history, self.config.ses_alpha)
                raw_support = values[0]
                method = "linear_regression"
            elif condition.reference == "EOQ30":
                if self.config.holding_cost <= 0:
                    raise ValueError("EOQ requires strictly positive holding cost")
                raw_support = math.sqrt(2*statistics.mean(history)*
                                        self.config.fixed_order_cost/self.config.holding_cost)
                values = None
                method = "EOQ30"
            else:
                raise ValueError(f"Unknown reference {condition.reference!r}")
            diagnostics.update(selected_method=method, forecast=values,
                               forecast_raw=values, support_policy=condition.reference)
        else:
            method = "linear_regression" if condition.t in ("T1", "T2") else None
            values = None
            try:
                if condition.t in ("T3", "T4"):
                    selection = ask("select", prompts.selection_instructions(role),
                                    {"C": context, "M": message,
                                     "candidates": prompts.METHOD_DESCRIPTIONS}, SELECTION_SCHEMA)
                    method = selection["method"]
                    diagnostics["llm_selected_method"] = method
                if condition.t in ("T0", "T1", "T3"):
                    payload = {"history_30": history,
                               "targets": ["D[t+1]", "D[t+2]"]}
                    if method:
                        payload.update(method=method,
                                       formula=prompts.formula(method, self.config.ses_alpha))
                    result = ask("forecast", prompts.forecast_instructions(role, method),
                                 payload, FORECAST_SCHEMA)
                    raw_values = [result["forecast_h1"], result["forecast_h2"]]
                    values = [max(0.0, float(value)) for value in raw_values]
                    corrected = sum(a != b for a, b in zip(raw_values, values))
                    diagnostics["forecast_correction_count"] = corrected
                    diagnostics["correction_count"] += corrected
                    diagnostics["forecast_raw"] = raw_values
                    if condition.t in ("T1", "T3"):
                        exact = forecast(method, history, self.config.ses_alpha)
                        errors = [float(value)-expected for value, expected in zip(values, exact)]
                        diagnostics["forecast_arithmetic_error"] = errors
                        diagnostics["arithmetic_error_count"] = sum(abs(e) > 1e-8 for e in errors)
                        diagnostics["arithmetic_error"] = any(abs(e) > 1e-8 for e in errors)
                        diagnostics["forecast_audit_exact"] = exact
                elif condition.t in ("T2", "T4"):
                    values = forecast(method, history, self.config.ses_alpha)
                    diagnostics["forecast_raw"] = list(values)
                else:
                    raise ValueError(f"Unknown T level {condition.t!r}")
            except RecoverableCallError as exc:
                method = "linear_regression"
                values = forecast(method, history, self.config.ses_alpha)
                diagnostics.update(forecast_fallback=True, forecast_fallback_reason=str(exc),
                                   forecast_raw=list(values))
                diagnostics["fallback_count"] += 1
            diagnostics.update(selected_method=method or "llm_direct", forecast=list(values),
                               support_policy="order_up_to")
            raw_support = order_up_to(values, row["I"], row["B"], row["P_pre"], self.config)

        # Q_base_raw = 정수·상한 보정 전 기준량; OUT의 max(0, ...)는 이미 적용됨.
        # Q_base는 보정 후 최종 판단에 제공하는 참고량이며, 실제 발주는 Q_final입니다.
        # A에는 기준량 자체가 없으므로 두 필드 모두 None(null)을 기록합니다.
        base = None if condition.direct_order else normalize_order(raw_support, self.config.order_cap)
        diagnostics["Q_base_raw"] = raw_support
        diagnostics["base_corrected"] = base != raw_support
        diagnostics["correction_count"] += int(base != raw_support)
        order_payload = ({"C": context, "D": row["D"]} if condition.direct_order
                         else {"C": context, "M": message, "Q_base": base})
        order_prompt = (prompts.direct_order_instructions(role, condition.objective, self.config)
                        if condition.direct_order else
                        prompts.order_instructions(role, condition.objective,
                                                   self.config, condition.reference))
        try:
            result = ask("order", order_prompt, order_payload, ORDER_SCHEMA)
            raw_order = result["order_quantity"]
            final = normalize_order(raw_order, self.config.order_cap)
            rationale = result["rationale"]
            diagnostics["order_corrected"] = raw_order != final
            diagnostics["correction_count"] += int(raw_order != final)
        except RecoverableCallError as exc:
            raw_order = None
            if condition.direct_order:
                final = normalize_order(row["D"], self.config.order_cap)
                rationale = "Final-order call failed; executed the normalized current incoming request."
                fallback_policy = "current_request"
                fallback_corrected = final != row["D"]
                diagnostics["fallback_quantity_raw"] = row["D"]
                diagnostics["correction_count"] += int(fallback_corrected)
            else:
                final = base
                rationale = "Final-order call failed; executed the normalized current support quantity."
                fallback_policy = "current_reference_order"
                fallback_corrected = False
            diagnostics.update(order_fallback=True, order_fallback_reason=str(exc),
                               order_corrected=fallback_corrected, fallback_policy=fallback_policy)
            diagnostics["fallback_count"] += 1
        # Q_final_raw = LLM이 제안한 보정 전 수량; Q_final = 실제 실행 수량.
        # 최종 호출 실패 시 유효한 제안이 없으므로 raw는 None, 실행량은 fallback 기록 참조.
        diagnostics.update(Q_final_raw=raw_order, rationale=rationale,
                           order_deviation=None if base is None else abs(final-base))
        committed = env.finish_agent(i, t, base, final, diagnostics)

        outgoing = None
        if i < len(ROLES)-1 and condition.m == "M1":
            demands = state.demands[-10:]
            if len(demands) != 10:
                raise ValueError("M1 requires exactly ten current-inclusive local requests")
            outgoing = {"t": t, "sender": role, "I": committed["I"], "B": committed["B"],
                        "P_post": committed["P_post"], "D": committed["D"],
                        "D_mean_10": statistics.mean(demands),
                        "D_std_10": statistics.stdev(demands)}
        elif i < len(ROLES)-1 and condition.m in ("M2", "M3", "M4"):
            payload = {"C": context, "M": message, "Q_base": base, "Q_final": final,
                       "rationale": rationale, "P_post": committed["P_post"]}
            cap = self.config.order_cap
            schema = plan_schema(cap) if condition.m == "M3" else MESSAGE_SCHEMA
            try:
                output = ask("message", prompts.message_instructions(role, condition.m, cap),
                             payload, schema,
                             validate=lambda value: _validate_message(value, condition.m, cap))
                outgoing = deepcopy(output) if condition.m == "M3" else output["message"]
            except RecoverableCallError as exc:
                diagnostics.update(message_fallback=True, message_fallback_reason=str(exc))
                diagnostics["fallback_count"] += 1
        elif condition.m not in ("M0", "M1", "M2", "M3", "M4"):
            raise ValueError(f"Unknown M level {condition.m!r}")
        # Include the original physical attempts when a resumed provider replays its journal.
        decision_events = [event for event in self.provider.events
                           if (event.get("t"), event.get("agent"), event.get("condition")) ==
                           (t, i, condition.id)]
        diagnostics.update(_event_counts(decision_events))
        elapsed_ms = (time.perf_counter()-started)*1000
        diagnostics.update(outgoing_message=outgoing, latency_replayed=replayed,
                           decision_latency_ms=None if replayed else elapsed_ms,
                           replay_wall_time_ms=elapsed_ms if replayed else None)
        return env.update_agent_record(i, t, diagnostics)
