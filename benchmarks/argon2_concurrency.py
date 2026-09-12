from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from cueflow.auth_crypto import PRODUCTION_ARGON2_CONFIG, PasswordHashService

PASSWORD = "CueFlow benchmark password value 2026"


def _operation(hasher: PasswordHashService, encoded: str, index: int) -> float:
    started = time.perf_counter()
    if index % 2:
        if not hasher.verify_password(encoded, PASSWORD).valid:
            raise RuntimeError("Argon2 verification failed during benchmark")
    else:
        hasher.hash_password(PASSWORD + str(index))
    return (time.perf_counter() - started) * 1000


def run_benchmark(*, concurrency: int, operations: int) -> dict[str, Any]:
    if concurrency < 1 or operations < concurrency:
        raise ValueError("operations must be at least the positive concurrency value")
    hasher = PasswordHashService(config=PRODUCTION_ARGON2_CONFIG)
    encoded = hasher.hash_password(PASSWORD)
    wall_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        durations = list(
            executor.map(lambda index: _operation(hasher, encoded, index), range(operations))
        )
    wall_ms = (time.perf_counter() - wall_started) * 1000
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))
    return {
        "argon2": {
            "memory_cost_kib": PRODUCTION_ARGON2_CONFIG.memory_cost_kib,
            "time_cost": PRODUCTION_ARGON2_CONFIG.time_cost,
            "parallelism": PRODUCTION_ARGON2_CONFIG.parallelism,
        },
        "concurrency": concurrency,
        "operations": operations,
        "estimated_argon2_working_set_mib": concurrency
        * PRODUCTION_ARGON2_CONFIG.memory_cost_kib
        / 1024,
        "wall_ms": round(wall_ms, 3),
        "throughput_per_second": round(operations / (wall_ms / 1000), 3),
        "latency_ms": {
            "median": round(statistics.median(ordered), 3),
            "p95": round(ordered[p95_index], 3),
            "max": round(max(ordered), 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark CueFlow production Argon2id parameters")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--operations", type=int, default=8)
    parser.add_argument("--max-p95-ms", type=float)
    args = parser.parse_args()
    result = run_benchmark(concurrency=args.concurrency, operations=args.operations)
    if args.max_p95_ms is None:
        result["release_gate"] = "measurement_only"
        exit_code = 0
    elif result["latency_ms"]["p95"] <= args.max_p95_ms:
        result["release_gate"] = "passed"
        exit_code = 0
    else:
        result["release_gate"] = "failed"
        exit_code = 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
