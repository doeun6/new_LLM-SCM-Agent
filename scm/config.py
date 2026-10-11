from dataclasses import asdict, dataclass
import hashlib
import json

ROLES = ("retailer", "wholesaler", "distributor", "factory")

@dataclass(frozen=True)
class SimulationConfig:
    warmup_periods: int = 30
    evaluation_periods: int = 100
    lead_time: int = 2
    demand_mean: float = 10.0
    demand_std: float = 3.0
    initial_inventory: int = 0
    initial_backlog: int = 0
    initial_arrival: int = 10
    synthetic_demand: int = 10
    safety_stock: float = 0.0
    order_cap: int = 100
    holding_cost: float = 1.0
    backlog_cost: float = 1.0
    variable_order_cost: float = 1.0
    fixed_order_cost: float = 1.0
    forecast_window: int = 30
    ses_alpha: float = 0.3
    std_ddof: int = 1

    def __post_init__(self):
        if self.warmup_periods < 30 or self.evaluation_periods < 1:
            raise ValueError("At least 30 warm-up periods and one evaluation period required")
        if self.lead_time != 2 or self.forecast_window != 30 or self.std_ddof != 1:
            raise ValueError("Registered design fixes lead time=2, forecast window=30, sample SD")
        if self.demand_std < 0 or self.order_cap < 1 or not 0 < self.ses_alpha <= 1:
            raise ValueError("Invalid simulation parameters")

@dataclass(frozen=True)
class APIConfig:
    model: str = "google/gemini-2.5-flash-lite"
    endpoint: str = "https://openrouter.ai/api/v1/responses"
    provider: str = "google-ai-studio/flex"
    service_tier: str = "flex"
    temperature: float = 0.1
    timeout_seconds: float = 60.0
    max_retries: int = 1
    retry_backoff_seconds: float = 1.0
    max_output_tokens: int = 300
    input_usd_per_million: float = 0.05
    output_usd_per_million: float = 0.20

    def __post_init__(self):
        import math
        for key in ("timeout_seconds", "retry_backoff_seconds", "input_usd_per_million", "output_usd_per_million"):
            value = getattr(self,key)
            if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid API setting {key}")
        if self.timeout_seconds <= 0 or self.max_retries != 1 or self.max_output_tokens != 300:
            raise ValueError("Registered G fixes one retry and 300 output tokens; timeout must be positive")

def canonical_hash(value):
    data = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
