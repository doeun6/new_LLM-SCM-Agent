"""Sequential physical SCM engine implementing the agreed Word steps 1--7.

P is the outstanding-order ledger; arrivals contains only shipped physical
goods. At a completed period P equals transit plus the upstream backlog.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import math
import random

from .config import ROLES, SimulationConfig
from .forecasting import forecast, normalize_order, order_up_to


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass
class AgentState:
    I: int
    B: int
    P: int
    arrivals: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    demands: list = field(default_factory=list)


CORE_ROW_FIELDS = frozenset({
    "t", "agent", "role", "D", "A", "X", "I", "B", "P_pre", "B_previous",
    "immediate_filled", "Q_base", "Q_final", "P_post", "holding_cost",
    "backlog_cost", "variable_order_cost", "fixed_order_cost", "cost", "local_cost", "total_cost",
})


def _metadata(extra):
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise ValueError("extra must be a dictionary")
    overlap = CORE_ROW_FIELDS.intersection(extra)
    if overlap:
        raise ValueError(f"Metadata cannot replace core record fields: {sorted(overlap)}")
    return deepcopy(extra)


class SupplyChain:
    def __init__(self, config):
        self.config = config
        _integer(config.initial_inventory, "initial inventory")
        _integer(config.initial_backlog, "initial backlog")
        _integer(config.initial_arrival, "initial arrival")
        _integer(config.synthetic_demand, "synthetic demand")
        if config.initial_backlog != 0:
            raise ValueError("This initialized design requires zero initial backlog")
        for label in ("holding_cost", "backlog_cost", "variable_order_cost", "fixed_order_cost"):
            value = getattr(config, label)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and nonnegative")
        self.agents = [
            AgentState(
                I=config.initial_inventory, B=config.initial_backlog,
                P=config.lead_time * config.initial_arrival,
                arrivals={t: config.initial_arrival for t in range(1, config.lead_time + 1)},
                demands=[config.synthetic_demand] * config.forecast_window,
            ) for _ in ROLES
        ]
        self.period_completed = 0
        self.next_agent = 0
        self._pending = None
        self.validate_invariants()

    def _position(self, i, t):
        _integer(i, "agent index")
        _integer(t, "period", 1)
        if i >= len(self.agents):
            raise ValueError("Unknown agent index")
        if t != self.period_completed + 1 or i != self.next_agent:
            raise ValueError("Agents must execute in downstream-to-upstream order in the current period")
        if t > self.config.warmup_periods + self.config.evaluation_periods:
            raise ValueError("The configured episode is complete")

    def begin_agent(self, i, t, D):
        """Apply steps 1--3 once, then expose the pre-order decision snapshot."""
        self._position(i, t)
        if self._pending is not None:
            raise ValueError("The pending decision must be finished before another begin")
        _integer(D, "incoming order")
        if i and D != self.agents[i - 1].history[-1]["Q_final"]:
            raise ValueError("Incoming order must equal the downstream current final order")
        state = self.agents[i]
        arrival = state.arrivals.get(t, 0)
        _integer(arrival, "scheduled arrival")
        if arrival > state.P:
            raise ValueError("Actual arrival exceeds outstanding orders")
        previous_backlog = state.B
        available = state.I + arrival
        shipped = min(available, previous_backlog + D)
        state.arrivals.pop(t, None)
        state.I = available - shipped
        state.B = previous_backlog + D - shipped
        state.P -= arrival
        state.demands.append(D)
        if i and shipped:
            due = t + self.config.lead_time
            downstream = self.agents[i - 1]
            downstream.arrivals[due] = downstream.arrivals.get(due, 0) + shipped
        row = {
            "t": t, "agent": i, "role": ROLES[i], "D": D, "A": arrival,
            "X": shipped, "I": state.I, "B": state.B, "P_pre": state.P,
            "B_previous": previous_backlog,
            "immediate_filled": min(D, max(0, shipped - previous_backlog)),
        }
        self._pending = {"agent": i, "t": t, "row": row}
        return deepcopy(row)

    def finish_agent(self, i, t, Q_base, Q_final, extra=None):
        """Commit steps 6--7 exactly once after the caller completes its decision."""
        self._position(i, t)
        if self._pending is None or (self._pending["agent"], self._pending["t"]) != (i, t):
            raise ValueError("No matching begun decision to finish")
        for label, quantity in (("Q_base", Q_base), ("Q_final", Q_final)):
            if label == "Q_base" and quantity is None:
                continue  # Direct-order A has no reference order.
            _integer(quantity, label)
            if quantity > self.config.order_cap:
                raise ValueError(f"{label} exceeds the configured order cap")
        metadata = _metadata(extra)
        state = self.agents[i]
        row = deepcopy(self._pending["row"])
        state.P += Q_final
        if i == len(self.agents) - 1 and Q_final:
            due = t + self.config.lead_time
            state.arrivals[due] = state.arrivals.get(due, 0) + Q_final
        holding = self.config.holding_cost * state.I
        backlog = self.config.backlog_cost * state.B
        variable = self.config.variable_order_cost * Q_final
        fixed = self.config.fixed_order_cost if Q_final > 0 else 0.0
        cost = holding + backlog + variable + fixed
        row.update({
            "Q_base": Q_base, "Q_final": Q_final, "P_post": state.P,
            "holding_cost": holding, "backlog_cost": backlog,
            "variable_order_cost": variable, "fixed_order_cost": fixed,
            "cost": cost, "local_cost": cost, "total_cost": cost,
        })
        row.update(metadata)
        state.history.append(deepcopy(row))
        self._pending = None
        self.next_agent += 1
        if self.next_agent == len(self.agents):
            self.period_completed = t
            self.next_agent = 0
            self.validate_invariants()
        return deepcopy(row)

    def update_agent_record(self, i, t, extra):
        """Attach post-order message/usage diagnostics without committing again."""
        _integer(i, "agent index")
        _integer(t, "period", 1)
        if i >= len(self.agents):
            raise ValueError("Unknown agent index")
        history = self.agents[i].history
        if not history or history[-1]["t"] != t:
            raise ValueError("Only the latest completed agent record may be annotated")
        history[-1].update(_metadata(extra))
        return deepcopy(history[-1])

    def validate_invariants(self):
        """Check physical and checkpoint consistency, including period-end P."""
        _integer(self.period_completed, "completed period")
        _integer(self.next_agent, "next agent")
        if self.next_agent >= len(ROLES) or len(self.agents) != len(ROLES):
            raise ValueError("Invalid agent state count or next-agent index")
        if self.period_completed > self.config.warmup_periods + self.config.evaluation_periods:
            raise ValueError("Snapshot is beyond the configured horizon")
        if self._pending is not None:
            if self._pending["agent"] != self.next_agent or self._pending["t"] != self.period_completed + 1:
                raise ValueError("Invalid pending decision position")
        for index, state in enumerate(self.agents):
            for field_name in ("I", "B", "P"):
                _integer(getattr(state, field_name), field_name)
            for due, quantity in state.arrivals.items():
                _integer(due, "arrival period", self.period_completed + 1)
                _integer(quantity, "arrival quantity")
            expected_history = self.period_completed + int(index < self.next_agent)
            if len(state.history) != expected_history:
                raise ValueError("Checkpoint history length does not match its period")
            for period, row in enumerate(state.history, 1):
                if row.get("t") != period or row.get("agent") != index or row.get("role") != ROLES[index]:
                    raise ValueError("Checkpoint history has inconsistent period or agent labels")
                for label in ("Q_base", "Q_final"):
                    quantity = row.get(label)
                    if label == "Q_base" and quantity is None:
                        continue
                    _integer(quantity, label)
                    if quantity > self.config.order_cap:
                        raise ValueError(f"Historical {label} exceeds the configured order cap")
            pending_here = int(self._pending is not None and self._pending["agent"] == index)
            if len(state.demands) != self.config.forecast_window + expected_history + pending_here:
                raise ValueError("Checkpoint incoming-order history length is inconsistent")
            for demand in state.demands:
                _integer(demand, "historical incoming order")
            if self.next_agent == 0 and self._pending is None:
                upstream_backlog = self.agents[index + 1].B if index + 1 < len(self.agents) else 0
                if state.P != sum(state.arrivals.values()) + upstream_backlog:
                    raise ValueError("Outstanding P must equal physical transit plus upstream backlog")

    def to_dict(self):
        return {
            "schema_version": 1, "config": asdict(self.config),
            "period_completed": self.period_completed, "next_agent": self.next_agent,
            "pending": deepcopy(self._pending), "agents": [asdict(state) for state in self.agents],
        }

    @classmethod
    def from_dict(cls, data, config):
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported supply-chain checkpoint")
        if data.get("config") != asdict(config):
            raise ValueError("Checkpoint simulation configuration does not match")
        result = cls(config)
        result.period_completed = data["period_completed"]
        result.next_agent = data["next_agent"]
        result._pending = deepcopy(data.get("pending"))
        result.agents = []
        for record in data["agents"]:
            raw = deepcopy(record)
            raw["arrivals"] = {int(due): quantity for due, quantity in raw["arrivals"].items()}
            result.agents.append(AgentState(**raw))
        result.validate_invariants()
        return result

    def clone(self):
        return self.from_dict(self.to_dict(), self.config)


def generate_demand(seed, config):
    """Freeze an independent external path; index zero is real period one."""
    if type(seed) is not int:
        raise ValueError("Demand seed must be an integer")
    if not math.isfinite(config.demand_mean) or not math.isfinite(config.demand_std) or config.demand_std < 0:
        raise ValueError("Demand mean and standard deviation must be finite; SD cannot be negative")
    rng = random.Random(seed)
    return [normalize_order(rng.gauss(config.demand_mean, config.demand_std), cap=None)
            for _ in range(config.warmup_periods + config.evaluation_periods)]


def make_warmup(config, seed):
    """Create a reusable no-API, sequential LR30/OUT state at period 30."""
    demands = generate_demand(seed, config)
    env = SupplyChain(config)
    for t in range(1, config.warmup_periods + 1):
        incoming = demands[t - 1]
        for index in range(len(ROLES)):
            row = env.begin_agent(index, t, incoming)
            predictions = forecast("linear_regression", env.agents[index].demands)
            raw_base = order_up_to(predictions, row["I"], row["B"], row["P_pre"], config)
            quantity = normalize_order(raw_base, config.order_cap)
            env.finish_agent(index, t, quantity, quantity, {
                "phase": "warmup", "forecast_method": "linear_regression",
                "forecast_predictions": predictions, "api_calls": 0,
                "retry_count": 0, "format_failure_count": 0, "fallback_count": 0,
            })
            incoming = quantity
    return env, demands
