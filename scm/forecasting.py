"""Fixed thirty-observation forecast definitions and common quantity handling."""

import math
from numbers import Real

METHODS = ("linear_regression", "moving_average_30", "simple_exponential_smoothing")
WINDOW = 30


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite real number")
    return result


def normalize_order(raw, cap=100):
    """Round half up, floor at zero, then cap; cap=None is used for demand."""
    # raw = 수량 보정 전 값. 예: 12.6 -> 13, cap=100일 때 120.2 -> 100.
    # C의 기간별 원자료(raw representation)와는 다른 뜻입니다.
    value = _number(raw, "quantity")
    if cap is not None and (type(cap) is not int or cap < 0):
        raise ValueError("cap must be a nonnegative integer or None")
    if cap is not None and value >= cap:
        return cap
    return int(math.floor(max(0.0, value) + 0.5))


def forecast(method, history, alpha=0.3):
    """Return continuous h=1 and h=2 forecasts, using only the latest 30 D."""
    if method not in METHODS:
        raise ValueError(f"Unknown forecasting method: {method}")
    values = list(history)
    if len(values) < WINDOW:
        raise ValueError("Forecasting requires at least 30 incoming-order observations")
    y = [_number(value, "historical incoming order") for value in values[-WINDOW:]]
    if any(value < 0 for value in y):
        raise ValueError("Historical incoming orders cannot be negative")
    if method == "linear_regression":
        mean = math.fsum(y) / WINDOW
        x_mean = (WINDOW + 1) / 2
        denominator = math.fsum((x - x_mean) ** 2 for x in range(1, WINDOW + 1))
        slope = math.fsum((x - x_mean) * (value - mean)
                          for x, value in enumerate(y, 1)) / denominator
        result = [mean + slope * (WINDOW + h - x_mean) for h in (1, 2)]
    elif method == "moving_average_30":
        result = [math.fsum(y) / WINDOW] * 2
    else:
        weight = _number(alpha, "SES alpha")
        if not 0 < weight <= 1:
            raise ValueError("SES alpha must be in (0, 1]")
        level = y[0]
        for value in y[1:]:
            level = weight * value + (1 - weight) * level
        result = [level, level]
    return [max(0.0, _number(value, "forecast")) for value in result]


def order_up_to(predictions, I, B, P, config):
    """Compute the unrounded reference order from the post-fulfillment state."""
    values = list(predictions)
    if len(values) != 2:
        raise ValueError("Order-up-to requires the h=1 and h=2 forecasts")
    estimates = [max(0.0, _number(value, "forecast")) for value in values]
    inventory = _number(I, "inventory")
    backlog = _number(B, "backlog")
    pipeline = _number(P, "outstanding orders")
    safety = _number(config.safety_stock, "safety stock")
    if min(inventory, backlog, pipeline, safety) < 0:
        raise ValueError("Inventory, backlog, pipeline and safety stock cannot be negative")
    return max(0.0, math.fsum(estimates) + safety - (inventory + pipeline - backlog))
