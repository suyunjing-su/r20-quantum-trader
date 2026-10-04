"""Reproducible synthetic wall-clock comparison for Council proposal fan-out.

The workload is an injected provider stub with fixed per-call latency. These are
real measured wall-clock samples for the harness, not live-provider claims.
The primary comparison performs the same Group x Trader work in both paths:
legacy runs one Group at a time; optimized runs all Groups through the bounded
Council runner. A historical single-market topology is reported separately and
is not used as the before/after comparison.
Run from repository root: python scripts/bench_council_groups.py --samples 5
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astra_backend.council.debate import _bounded_parallel_calls

SYMBOL_COUNTS = (7, 14, 21, 28)
TRADER_ROLES = ("trend", "momentum", "quant")
MAX_CONCURRENCY = 4
STUB_LATENCY_SECONDS = 0.04


def _invoke_group(group_index: int) -> None:
    """Run the same injected Group x Trader work as the optimized path."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TRADER_ROLES)) as pool:
        list(pool.map(lambda _role: time.sleep(STUB_LATENCY_SECONDS), TRADER_ROLES))


def _old_single_market(symbol_count: int) -> float:
    """Historical topology: each role calls once for the full market."""
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TRADER_ROLES)) as pool:
        list(pool.map(lambda _role: time.sleep(STUB_LATENCY_SECONDS), TRADER_ROLES))
    return time.perf_counter() - started


def _old_group_serial(symbol_count: int) -> float:
    """Before path: same Group x Trader calls, but one Group at a time."""
    group_count = (symbol_count + 6) // 7
    started = time.perf_counter()
    for group_index in range(group_count):
        _invoke_group(group_index)
    return time.perf_counter() - started


def _new_group_fanout(symbol_count: int) -> float:
    """After path: same Group x Trader calls through bounded fan-out."""
    group_count = (symbol_count + 6) // 7
    tasks = []
    for group_index in range(group_count):
        for role in TRADER_ROLES:
            tasks.append((
                f"group_{group_index}:{role}",
                (time.sleep, STUB_LATENCY_SECONDS),
                {},
            ))
    started = time.perf_counter()
    _bounded_parallel_calls(
        tasks,
        deadline=time.time() + 10.0,
        max_workers=MAX_CONCURRENCY,
        stagger_seconds=0.0,
    )
    return time.perf_counter() - started


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {
        "min_ms": round(min(samples) * 1000, 3),
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "p50_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(p95 * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
    }


def run(samples: int) -> dict:
    results = {
        "workload": "fixed-latency injected provider stub; not live-provider performance",
        "comparison": "equal Group x Trader work: serial Groups before vs bounded cross-Group fan-out after",
        "historical_reference": "single full-market call per Trader; excluded from before_after comparison",
        "stub_latency_seconds": STUB_LATENCY_SECONDS,
        "trader_roles": list(TRADER_ROLES),
        "max_concurrency": MAX_CONCURRENCY,
        "samples_per_case": samples,
        "cases": {},
    }
    for symbols in SYMBOL_COUNTS:
        before = [_old_group_serial(symbols) for _ in range(samples)]
        after = [_new_group_fanout(symbols) for _ in range(samples)]
        historical = [_old_single_market(symbols) for _ in range(samples)]
        results["cases"][str(symbols)] = {
            "group_count": (symbols + 6) // 7,
            "before_equal_work_group_serial_raw_ms": [round(x * 1000, 3) for x in before],
            "after_equal_work_group_fanout_raw_ms": [round(x * 1000, 3) for x in after],
            "historical_single_market_raw_ms": [round(x * 1000, 3) for x in historical],
            "before_equal_work_group_serial": _summary(before),
            "after_equal_work_group_fanout": _summary(after),
            "historical_single_market": _summary(historical),
            "median_speedup_ratio": round(
                statistics.median(before) / statistics.median(after), 3
            ),
            "median_wall_clock_delta_ms": round(
                (statistics.median(before) - statistics.median(after)) * 1000, 3
            ),
        }
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", default="data/council_group_benchmark.json")
    args = parser.parse_args()
    report = run(max(1, args.samples))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
