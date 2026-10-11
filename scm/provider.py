"""Audited OpenRouter transport. One retry budget per logical task, no hidden SDK retries."""
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import APIConfig, canonical_hash

class RecoverableCallError(RuntimeError):
    pass

class FatalAPIError(RuntimeError):
    pass

class BudgetExceeded(FatalAPIError):
    pass

def validate_schema(value, schema, path="$"):
    """Validate the small JSON Schema subset used by this experiment, including finite numbers."""
    typ = schema.get("type")
    types = typ if isinstance(typ, list) else [typ]
    ok = any((t == "object" and isinstance(value, dict)) or
             (t == "array" and isinstance(value, list)) or
             (t == "string" and isinstance(value, str)) or
             (t == "number" and type(value) in (int, float) and math.isfinite(value)) or
             (t == "integer" and type(value) is int) or
             (t == "boolean" and type(value) is bool) or
             (t == "null" and value is None) for t in types)
    if not ok:
        raise ValueError(f"{path}: expected {typ}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid option")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path}: missing {sorted(missing)}")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValueError(f"{path}: unexpected fields")
        for k, v in value.items():
            if k in props:
                validate_schema(v, props[k], path + "." + k)
    if isinstance(value, list):
        for i, v in enumerate(value):
            validate_schema(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ValueError(f"{path}: string too long")

def strict_json(text):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError("Duplicate JSON field: " + k)
            d[k] = v
        return d
    def bad_constant(value):
        raise ValueError("Non-finite JSON constant: " + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)

class Budget:
    def __init__(self, limit_usd=8.0, already_spent=0.0):
        if type(limit_usd) not in (int,float) or not math.isfinite(limit_usd) or limit_usd <= 0:
            raise ValueError("Positive explicit API budget required")
        self._money(already_spent)
        self.limit = limit_usd
        self.spent = already_spent
        self.reserved = 0.0
        self.lock = threading.Lock()
    @staticmethod
    def _money(value):
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError("Budget amounts must be finite and nonnegative")
    def reserve(self, amount):
        self._money(amount)
        with self.lock:
            if self.spent + self.reserved + amount > self.limit:
                raise BudgetExceeded(f"API budget ${self.limit:g} reached; completed work saved")
            self.reserved += amount
    def settle(self, reserved, charged):
        self._money(reserved)
        self._money(charged)
        with self.lock:
            self.reserved -= reserved
            self.spent += charged

class OpenRouterProvider:
    def __init__(self, config=None, journal=None, budget=None, stop=None):
        self.config = config or APIConfig()
        self.journal = Path(journal) if journal else None
        self.budget = budget or Budget()
        self.stop = stop
        self.events = []
        self._by_key = {}
        self._key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self._key:
            raise FatalAPIError("OPENROUTER_API_KEY is missing; credentials are never stored in results")
        if self.journal and self.journal.exists():
            for line in self.journal.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                self.events.append(event)
                self._by_key.setdefault(event["call_key"], []).append(event)

    def _safe_error(self, error):
        return re.sub(r"sk-or-[A-Za-z0-9_-]+", "[REDACTED]", str(error).replace(self._key, "[REDACTED]"))[:700]

    def _record(self, event):
        self.events.append(event)
        self._by_key.setdefault(event["call_key"], []).append(event)
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def ask(self, task, instructions, payload, schema, meta, validate=None):
        base = {"task": task, "instructions": instructions, "payload": payload, "schema": schema, "meta": meta, "api": asdict(self.config)}
        call_key = canonical_hash(base)
        previous = self._by_key.get(call_key, [])
        if previous and previous[-1].get("success"):
            parsed = previous[-1]["parsed_output"]
            validate_schema(parsed, schema)
            if validate:
                validate(parsed)
            return parsed
        if previous and previous[-1].get("fatal"):
            raise FatalAPIError(previous[-1]["error"])
        feedback = previous[-1].get("error", "") if previous else ""
        for attempt in range(len(previous), self.config.max_retries + 1):
            if self.stop and self.stop.is_set():
                raise FatalAPIError("Batch stop requested before API call")
            task_payload = dict(payload)
            if feedback:
                task_payload["retry_feedback"] = feedback
            request_body = {
                "model": self.config.model, "instructions": instructions,
                "input": json.dumps(task_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                "text": {"format": {"type": "json_schema", "name": "scm_" + task, "strict": True, "schema": schema}},
                "temperature": self.config.temperature, "max_output_tokens": self.config.max_output_tokens,
                "store": False, "service_tier": self.config.service_tier,
                "provider": {"only": [self.config.provider], "allow_fallbacks": False,
                             "require_parameters": True, "data_collection": "deny"},
            }
            encoded = json.dumps(request_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            # Conservative byte-based allowance before each call; actual reported charges replace it.
            reservation = ((len(encoded) + 1024) * self.config.input_usd_per_million +
                           self.config.max_output_tokens * self.config.output_usd_per_million) / 1e6
            self.budget.reserve(reservation)
            event = dict(meta, task=task, attempt=attempt, call_key=call_key,
                         input_tokens=None, output_tokens=None, total_tokens=None, reported_cost_usd=None,
                         reasoning_tokens=None, success=False, format_failure=False, fatal=False,
                         instructions=instructions, payload=task_payload, response_schema=schema,
                         backend="openrouter", started_at_unix=time.time())
            started = time.perf_counter()
            parsed = None
            fatal = None
            try:
                req = Request(self.config.endpoint, data=encoded, headers={
                    "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
                    "X-OpenRouter-Metadata": "enabled", "X-OpenRouter-Title": "SCM_LLM_Agent"})
                with urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    event["http_status"] = resp.status
                usage = data.get("usage") or {}
                for field in ("input_tokens", "output_tokens", "total_tokens"):
                    v = usage.get(field)
                    event[field] = v if type(v) is int and v >= 0 else None
                cost = usage.get("cost")
                event["reported_cost_usd"] = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
                event["reasoning_tokens"] = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
                event["response_id"] = data.get("id")
                event["actual_model"] = data.get("model")
                event["actual_service_tier"] = data.get("service_tier")
                routing = data.get("openrouter_metadata") or {}
                selected = [x for x in (routing.get("endpoints") or {}).get("available", []) if x.get("selected") is True]
                event["actual_provider"] = selected[0].get("provider") if selected else None
                if event["actual_model"] != self.config.model or event["actual_service_tier"] != self.config.service_tier or event["actual_provider"] != "Google AI Studio":
                    raise FatalAPIError("Actual model/provider/Flex cannot be verified; execution stopped to preserve the registered route")
                output = data.get("output_text") or "".join(
                    p.get("text", "") for x in data.get("output", []) if x.get("type") == "message"
                    for p in x.get("content", []) if p.get("type") == "output_text")
                event["raw_output"] = output
                event["response_status"] = data.get("status")
                if data.get("error"):
                    raise ValueError("Provider returned a response error")
                if data.get("status") != "completed":
                    raise ValueError("Incomplete response (possibly output limit); no partial decision accepted")
                parsed = strict_json(output)
                validate_schema(parsed, schema)
                if validate:
                    validate(parsed)
                event["parsed_output"] = parsed
                event["success"] = True
            except HTTPError as exc:
                event["http_status"] = exc.code
                event["error_type"] = "HTTPError"
                try:
                    error = json.loads(exc.read().decode("utf-8")).get("error", {})
                    detail = error.get("message", str(exc.code)) if isinstance(error, dict) else str(error)
                except (ValueError, UnicodeError):
                    detail = str(exc.code)
                event["error"] = self._safe_error(detail)
                if exc.code not in (408, 409, 425, 429, 500, 502, 503, 504, 524, 529):
                    fatal = FatalAPIError(f"Non-retryable API HTTP {exc.code}: " + event["error"])
            except (URLError, TimeoutError, OSError) as exc:
                event["error_type"] = type(exc).__name__
                event["error"] = self._safe_error(exc)
            except (ValueError, TypeError) as exc:
                event["format_failure"] = True
                event["error_type"] = "ValidationError"
                event["error"] = self._safe_error(exc)
            except FatalAPIError as exc:
                fatal = exc
                event["error_type"] = "FatalAPIError"
                event["error"] = self._safe_error(exc)
            except BaseException as exc:
                fatal = FatalAPIError("Unexpected provider software error: " + type(exc).__name__)
                event["error_type"] = type(exc).__name__
                event["error"] = str(fatal)
            finally:
                event["latency_seconds"] = time.perf_counter() - started
                event["fatal"] = fatal is not None
                # Missing usage is not reported as free; budget conservatively reserves its maximum estimate.
                charge = event["reported_cost_usd"]
                event["budget_charge_usd"] = charge if charge is not None else reservation
                event["budget_charge_estimated"] = charge is None
                self.budget.settle(reservation, event["budget_charge_usd"])
                self._record(event)
            if fatal:
                raise fatal
            if event["success"]:
                return parsed
            feedback = event.get("error", "Invalid response")
            if attempt < self.config.max_retries:
                time.sleep(self.config.retry_backoff_seconds)
        raise RecoverableCallError(feedback or "Task exhausted its shared retry budget")

class MockProvider:
    """Deterministic transport double for tests; never evidence of LLM performance."""
    def __init__(self):
        self.events = []
    def ask(self, task, instructions, payload, schema, meta, validate=None):
        from .forecasting import forecast
        if task == "forecast":
            vals = forecast(payload.get("method", "moving_average_30"), payload["history_30"])
            parsed = {"forecast_h1": vals[0], "forecast_h2": vals[1]}
        elif task == "select":
            parsed = {"method": "moving_average_30"}
        elif task == "order":
            if "Q_base" in payload:
                parsed = {"order_quantity": payload["Q_base"], "rationale": "Retain the supplied reference quantity."}
            else:
                parsed = {"order_quantity": payload["D"], "rationale": "Replenish the current incoming request."}
        elif task == "message" and meta.get("message_level") == "M3":
            plan = int(payload["Q_final"])  # deterministic test double: keep the committed order
            parsed = {"plan_t_plus_1": plan, "plan_t_plus_2": plan}
        else:
            parsed = {"message": "The order reflects replenishment needs and the outstanding supply."}
        validate_schema(parsed, schema)
        if validate:
            validate(parsed)
        self.events.append(dict(meta, task=task, attempt=0, input_tokens=0, output_tokens=0, total_tokens=0,
                                reported_cost_usd=0.0, reasoning_tokens=0, latency_seconds=0,
                                backend="mock", success=True, format_failure=False,
                                instructions=instructions, payload=payload, parsed_output=parsed))
        return parsed
