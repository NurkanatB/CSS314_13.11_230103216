"""
task4_scheduling.py - Phase 4: skewed workload, static vs cyclic vs dynamic scheduling.

Usage (PowerShell, inside parallel_lab/):
    python task4_scheduling.py            # official parameters (N = 12,000, asks for confirmation)
    python task4_scheduling.py --yes
    python task4_scheduling.py --quick    # N = 3,000, fewer runs (hash NOT valid)
    python task4_scheduling.py --force

Workload: item i in [0, N) performs K(i) = 1 + floor((i/60)^2) iterations of
sin/cos/sqrt (K computed exactly as 1 + i*i // 3600). Item 11,999 does ~40,000x the work of item 0.
Executed by P_phys worker processes (one mp.Process per worker per trial):
  Strategy 1: contiguous static blocks      Strategy 2: cyclic (round-robin) static
  Strategy 3: dynamic chunk queue, chunk sizes C in {1, 4, 16, 64, 256, 1024}, claimed from a
              shared mp.Value('i') counter protected by its lock.
Each worker records its own active time t_worker[t] (ms, loop start -> loop end, including
lock waits) in a shared mp.Array('d', p). Load imbalance: lambda = max(t_worker) / mean(t_worker).
Twall includes process start/join (it is the real wall-clock of a trial); lambda uses worker times only.
All strategies must produce the same checksum (verified within fp tolerance).

Estimated runtime (full): ~3-12 minutes (static block is the slowest by design).
Memory: ~35 MB per worker process.
VERIFY_HASH[PHASE4]: metric1 = Twall(Strategy 1) ms, metric2 = Twall(Strategy 3, C*) ms (medians).
"""
from __future__ import annotations

import math
import multiprocessing as mp
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import harness

N_FULL = 12_000
CHUNK_SIZES = [1, 4, 16, 64, 256, 1024]


# ==========================================================================
# Work (module level for 'spawn')
# ==========================================================================
def item_work(i: int) -> float:
    """K(i) = 1 + floor((i/60)^2) heavy trigonometric iterations."""
    k = 1 + (i * i) // 3600
    sin, cos, sqrt = math.sin, math.cos, math.sqrt
    x = 0.5 + 0.001 * (i % 100)
    acc = 0.0
    for _ in range(k):
        x = sin(x) + cos(x)
        acc += sqrt(abs(x) + 1.0)
    return acc


def total_iterations(n: int) -> int:
    """Total number of heavy iterations Sum K(i)."""
    return sum(1 + (i * i) // 3600 for i in range(n))


def sched_worker(strategy: str, t: int, p: int, n: int, chunk: int,
                 counter: Any, times: Any, sums: Any) -> None:
    """One worker. Records active time (ms) and a checksum in the shared arrays."""
    total = 0.0
    t0 = time.perf_counter_ns()
    if strategy == "block":
        for i in range(t * n // p, (t + 1) * n // p):
            total += item_work(i)
    elif strategy == "cyclic":
        for i in range(t, n, p):
            total += item_work(i)
    else:  # dynamic chunk queue
        while True:
            with counter.get_lock():                    # claim [start, start + chunk)
                start = counter.value
                counter.value = start + chunk
            if start >= n:
                break
            for i in range(start, min(start + chunk, n)):
                total += item_work(i)
    times[t] = (time.perf_counter_ns() - t0) / 1e6
    sums[t] = total


def run_once(strategy: str, chunk: int, p: int, n: int) -> Tuple[float, List[float], float]:
    """One trial. Returns (Twall ms, worker active times ms, checksum)."""
    counter = mp.Value("i", 0)
    times = mp.RawArray("d", p)
    sums = mp.RawArray("d", p)
    procs = [mp.Process(target=sched_worker, args=(strategy, t, p, n, chunk, counter, times, sums))
             for t in range(p)]
    t0 = time.perf_counter_ns()
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    wall = (time.perf_counter_ns() - t0) / 1e6
    bad = [pr.exitcode for pr in procs if pr.exitcode != 0]
    if bad:
        raise RuntimeError(f"worker failure, exit codes: {bad}")
    return wall, list(times), sum(sums)


def imbalance(times: List[float]) -> float:
    """lambda = max / mean of worker active times (1.00 = perfect balance)."""
    mean = sum(times) / len(times)
    return max(times) / mean if mean > 0 else 1.0


def measure(strategy: str, chunk: int, p: int, n: int, warmup: int, timed: int) -> Dict[str, Any]:
    """Benchmark one scheduling configuration."""
    runs: List[Tuple[float, List[float], float]] = []
    stats = harness.run_benchmark(lambda: runs.append(run_once(strategy, chunk, p, n)),
                                  warmup_runs=warmup, timed_runs=timed)
    kept = runs[warmup:]
    lambdas = [imbalance(r[1]) for r in kept]
    last_times = kept[-1][1]
    return {"strategy": strategy, "chunk": chunk if strategy == "dynamic" else None,
            "twall_median_ms": stats.median_ms, "twall_iqr_ms": stats.iqr_ms,
            "twall_min_ms": stats.min_ms, "twall_max_ms": stats.max_ms,
            "lambda_median": harness.median(lambdas), "lambda_max": max(lambdas),
            "worker_ms_last_run": last_times,
            "mean_idle_ms": max(last_times) - sum(last_times) / len(last_times),
            "checksum": kept[-1][2]}


def estimate(n: int, p: int, warmup: int, timed: int) -> str:
    """Calibrate trig-iteration cost and extrapolate all 8 configurations."""
    probe = 300_000
    t0 = time.perf_counter_ns()
    x, acc = 0.5, 0.0
    sin, cos, sqrt = math.sin, math.cos, math.sqrt
    for _ in range(probe):
        x = sin(x) + cos(x)
        acc += sqrt(abs(x) + 1.0)
    ns = (time.perf_counter_ns() - t0) / probe
    work = total_iterations(n)
    seq = work * ns / 1e9
    last_block = (1 - ((n - n // p) / n) ** 3) if p > 1 else 1.0
    runs = warmup + timed
    total = (seq * last_block + seq / p * (1 + len(CHUNK_SIZES))) * runs
    return (f"[estimate] total work {work:,} iterations ~{ns:.0f} ns each -> sequential ~{seq:.1f} s; "
            f"block strategy ~{seq * last_block:.1f} s/trial (imbalanced), others ~{seq / p:.1f} s/trial; "
            f"whole phase ~{total / 60:.1f} min.\n[estimate] memory ~{p * 35} MB.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = harness.common_parser("Phase 4: skewed workload scheduling")
    args = parser.parse_args(argv)
    try:
        hw = harness.init_phase(args, "phase4")
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    mode = harness.get_mode()
    p = int(hw["physical_cores"])
    n = harness.pick(N_FULL, 3000)
    warmup, timed = harness.pick(1, 1), harness.pick(3, 2)
    harness.log(f"=== PHASE 4: Scheduling (mode={mode}, N={n:,}, workers={p}) ===")
    harness.mode_banner()
    if p == 1:
        harness.log("[warn] P_phys = 1: all strategies are trivially balanced (lambda = 1).")
    harness.confirm_run(estimate(n, p, warmup, timed), args)

    configs: List[Tuple[str, int, str]] = [("block", 0, "Strategy 1: contiguous block"),
                                           ("cyclic", 0, "Strategy 2: cyclic")]
    configs += [("dynamic", c, f"Strategy 3: dynamic C={c}") for c in CHUNK_SIZES]
    rows: List[Dict[str, Any]] = []
    try:
        for strategy, chunk, label in configs:
            harness.log(f"  running {label} ...")
            row = measure(strategy, chunk, p, n, warmup, timed)
            row["label"] = label
            rows.append(row)
            harness.log(f"    Twall median {row['twall_median_ms']:.0f} ms, lambda {row['lambda_median']:.3f}")
    except KeyboardInterrupt:
        print("\n[abort] interrupted. Re-run to start again.", file=sys.stderr)
        return 130

    ref = rows[0]["checksum"]
    checksum_ok = all(abs(r["checksum"] - ref) <= 1e-9 * abs(ref) for r in rows)
    dyn = [r for r in rows if r["strategy"] == "dynamic"]
    best = min(dyn, key=lambda r: r["twall_median_ms"])
    c_star = best["chunk"]
    block, cyclic = rows[0], rows[1]
    observations = [
        f"Static block: Twall {block['twall_median_ms']:.0f} ms, lambda {block['lambda_median']:.3f}; "
        f"cyclic: Twall {cyclic['twall_median_ms']:.0f} ms, lambda {cyclic['lambda_median']:.3f}.",
        f"Dynamic sweep: best chunk C* = {c_star} (Twall {best['twall_median_ms']:.0f} ms, "
        f"lambda {best['lambda_median']:.3f}); C=1 lambda {dyn[0]['lambda_median']:.3f}, "
        f"C=1024 lambda {dyn[-1]['lambda_median']:.3f}.",
        f"Speedup of Strategy 3 (C*) over Strategy 1: {block['twall_median_ms'] / best['twall_median_ms']:.2f}x.",
    ]
    if not checksum_ok:
        observations.append("WARNING: checksums differ between strategies beyond fp tolerance.")
    verify = harness.make_verify_record("PHASE4", hw["student_seed"], block["twall_median_ms"],
                                        best["twall_median_ms"])
    result = {
        "phase": 4, "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameters": {"N": n, "workers": p, "warmup": warmup, "timed": timed, "chunk_sizes": CHUNK_SIZES,
                       "total_iterations": total_iterations(n)},
        "rows": rows, "c_star": c_star, "checksum_consistent": checksum_ok,
        "observations": observations, "verify": verify,
    }
    path = harness.save_json("phase4", result)

    harness.log("\n--- Scheduling table ---")
    harness.log(harness.render_table(
        ["Strategy", "Twall median ms", "IQR ms", "lambda (median)", "mean idle ms"],
        [(r["label"], f"{r['twall_median_ms']:.0f}", f"{r['twall_iqr_ms']:.0f}", f"{r['lambda_median']:.3f}",
          f"{r['mean_idle_ms']:.0f}") for r in rows]))
    for line in observations:
        harness.log(f"[obs] {line}")
    harness.log("\n" + verify["tag"])
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
