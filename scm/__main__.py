import argparse
import json
from pathlib import Path
import sys

from .config import SimulationConfig
from .registry import registry, get_condition
from .runner import atomic_json, read_json, run_batch

def main(argv=None):
    parser = argparse.ArgumentParser(description="SCM C/M/T experiment; paid conditions and seeds are always explicit")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List the 103 registered conditions (no API calls)")
    run = sub.add_parser("run", help="Execute explicitly selected conditions")
    run.add_argument("--conditions", nargs="+", required=True)
    run.add_argument("--seeds", nargs="+", type=int, required=True)
    run.add_argument("--backend", choices=("mock", "openrouter"), required=True)
    run.add_argument("--objective", choices=("cost", "bullwhip"))
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--budget-usd", type=float, default=8)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="Validate selection and estimate calls without API requests")
    run.add_argument("--evaluation-periods", type=int, default=100, help="Non-100 values are diagnostic only, not the registered full experiment")
    analyze = sub.add_parser("analyze", help="Analyze the frozen eight-seed pilot only")
    analyze.add_argument("--input", type=Path, required=True, help="results.json from the complete real-API pilot")
    analyze.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "list":
        print("condition       C   M   T       objective     support")
        for c in registry().values():
            support = "direct LLM order (C0 + D)" if c.direct_order else (c.reference or "forecast + OUT")
            print(f"{c.id:15} {c.c:3} {c.m:3} {(c.t or '-'):7} {c.objective:13} {support}")
        return 0
    if args.command == "run":
        conditions = [get_condition(c, args.objective) for c in args.conditions]
        if args.dry_run:
            calls = sum(4 * (1 + {"T0":1,"T1":1,"T2":0,"T3":2,"T4":1,None:0}[c.t]) +
                        (3 if c.m in ("M2", "M3", "M4") else 0) for c in conditions) * len(args.seeds) * args.evaluation_periods
            print(json.dumps({"conditions": [c.id for c in conditions], "seeds": args.seeds,
                              "objectives": {c.id: c.objective for c in conditions},
                              "episodes": len(conditions)*len(args.seeds), "estimated_calls_without_retry": calls,
                              "estimated_usd_at_2000_input_200_output": calls*0.00014,
                              "budget_usd": args.budget_usd, "paid_requests_made": 0}, indent=2))
            return 0
        run_batch(args.conditions, args.seeds, args.output, backend=args.backend,
                  objective=args.objective, workers=args.workers, budget_usd=args.budget_usd,
                  resume=args.resume, config=SimulationConfig(evaluation_periods=args.evaluation_periods))
        return 0
    if args.command == "analyze":
        from .analysis import analyze as analyze_pilot, make_report
        results = read_json(args.input)
        if any(r.get("backend") != "openrouter" or not r.get("complete") for r in results):
            raise ValueError("Only completed real-API results can enter the pilot analysis")
        data = analyze_pilot(results)
        args.output.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output / "pilot_analysis.json", data)
        (args.output / "pilot_report.md").write_text(make_report(data), encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
