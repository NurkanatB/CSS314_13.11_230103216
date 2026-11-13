"""
task2_scaling.py - Phase 2: strong scaling (Amdahl), weak scaling (Gustafson), SMT inflection.

Usage (PowerShell, inside parallel_lab/):
    python task2_scaling.py            # official parameters (asks for confirmation)
    python task2_scaling.py --yes      # no prompt
    python task2_scaling.py --quick    # reduced debug run (hash NOT valid)
    python task2_scaling.py --force    # re-run although results/phase2.json exists

Pipeline per trial over N elements:
  Stage A (serial, parent process): first 5% of elements, 12 transcendental calls each
          (6 x [math.sin, math.sqrt]).
  Stage B (parallel): all N elements split evenly over a PERSISTENT mp.Pool(p) created
          OUTSIDE the timed region; 35 iterations/element of multiply-add + math.sin + math.sqrt.
          (The pool is a "synced" pool: one task per distinct worker, all starting together.)

Experiment 2A strong: N = 1.6e6, p = 1..P_log.   Experiment 2B weak: N(p) = p * 250,000.
NOTE (from the handout itself): in weak scaling Stage A covers 5% of N(p), so the SERIAL
work grows with p; alpha(p) = T_StageA(p) / T_weak(p) captures exactly this.

Estimated runtime (full): ~4-12 minutes; memory: ~30-40 MB per pool worker.
VERIFY_HASH[PHASE2]: metric1 = S_strong(P_log), metric2 = S_Gustafson(P_log).
"""
from __future__ import annotations

import math
import multiprocessing as mp
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import harness

STAGE_A_FRACTION = 0.05
STAGE_A_OPS_PAIRS = 6          # 6 x (sin, sqrt) = 12 transcendental operations / element
STAGE_B_ITERS = 35


# ==========================================================================
# Kernels (module level for 'spawn')
# ==========================================================================
def stage_a(n_elems: int) -> float:
    """Strictly serial prefilter: 12 transcendental calls per element."""
    sin, sqrt = math.sin, math.sqrt
    acc = 0.0
    for i in range(n_elems):
        x = 0.1 + 0.001 * (i % 1000)
        for _ in range(STAGE_A_OPS_PAIRS):
            s = sin(x)
            x = sqrt(1.0 + s * s)
        acc += x
    return acc


def stage_b_range(bounds: Tuple[int, int]) -> float:
    """Embarrassingly parallel kernel over elements [start, end); no shared writes."""
    harness.sync_point()                       # one task per distinct worker, common start
    start, end = bounds
    sin, sqrt = math.sin, math.sqrt
    total = 0.0
    for i in range(start, end):
        x = 0.1 + 0.001 * (i % 1000)
        for _ in range(STAGE_B_ITERS):
            x = sqrt(1.5 + sin(x * 1.0000001 + 0.25))   # multiply-add + sin + sqrt
        total += x
    return total


def pipeline(pool: Any, p: int, n: int, a_log: List[float]) -> float:
    """One full trial (Stage A then Stage B). Appends Stage-A time (ms) to a_log."""
    t0 = time.perf_counter_ns()
    acc = stage_a(int(STAGE_A_FRACTION * n))
    a_log.append((time.perf_counter_ns() - t0) / 1e6)
    parts = pool.map(stage_b_range, harness.balanced_ranges(n, p), chunksize=1)
    return acc + sum(parts)


def run_config(p: int, n: int, warmup: int, timed: int) -> Dict[str, Any]:
    """Create a persistent pool (untimed), benchmark the pipeline, tear the pool down."""
    pool = harness.create_synced_pool(p)
    a_log: List[float] = []
    checks: List[float] = []
    try:
        stats = harness.run_benchmark(lambda: checks.append(pipeline(pool, p, n, a_log)),
                                      warmup_runs=warmup, timed_runs=timed)
        pool.close()
        pool.join()
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    stage_a_ms = harness.median(a_log[warmup:])
    return {"p": p, "n": n, "median_ms": stats.median_ms, "iqr_ms": stats.iqr_ms,
            "min_ms": stats.min_ms, "max_ms": stats.max_ms,
            "stage_a_ms": stage_a_ms, "checksum": checks[-1]}


# ==========================================================================
# Estimation
# ==========================================================================
def estimate(n_strong: int, n0: int, p_log: int, warmup: int, timed: int) -> str:
    """Calibrate the Stage-B element cost and extrapolate the phase runtime."""
    probe = 4000
    t0 = time.perf_counter_ns()
    # calibrate with the same kernel body (sync_point skipped by inlining)
    sin, sqrt = math.sin, math.sqrt
    for i in range(probe):
        x = 0.1 + 0.001 * (i % 1000)
        for _ in range(STAGE_B_ITERS):
            x = sqrt(1.5 + sin(x * 1.0000001 + 0.25))
    per_elem_s = (time.perf_counter_ns() - t0) / 1e9 / probe
    runs = warmup + timed
    strong = sum(n_strong * per_elem_s / p for p in range(1, p_log + 1))
    weak = n0 * per_elem_s * p_log
    total = (strong + weak) * runs * 1.15 + p_log * 2 * 0.3
    return (f"[estimate] Stage-B cost ~{per_elem_s * 1e6:.1f} us/element -> strong p=1 trial ~"
            f"{n_strong * per_elem_s:.1f} s; whole phase ~{total / 60:.1f} min "
            f"(assumes ideal scaling, real time is longer).\n"
            f"[estimate] memory: ~{p_log * 35} MB for the largest pool.")


# ==========================================================================
# Analysis
# ==========================================================================
def marginal_analysis(rows: List[Dict[str, Any]], p_phys: int, p_log: int) -> Dict[str, Any]:
    """Marginal speedup per added worker and the P_phys -> P_phys+1 inflection."""
    speed = {r["p"]: r["speedup"] for r in rows}
    marginal = {p: speed[p] - speed[p - 1] for p in speed if p >= 2 and (p - 1) in speed}
    out: Dict[str, Any] = {"marginal_speedup": {str(k): v for k, v in marginal.items()}}
    if p_log <= p_phys:
        out["note"] = "P_log == P_phys: no SMT/Hyper-Threading step to analyse."
        return out
    below = [marginal[p] for p in range(2, p_phys + 1) if p in marginal]
    above = [marginal[p] for p in range(p_phys + 1, p_log + 1) if p in marginal]
    if below and (p_phys + 1) in marginal:
        mean_below = sum(below) / len(below)
        step = marginal[p_phys + 1]
        out["mean_marginal_p2_to_pphys"] = mean_below
        out["marginal_at_pphys_plus_1"] = step
        out["drop_vs_mean_below_percent"] = (1.0 - step / mean_below) * 100.0 if mean_below else None
        if p_phys in marginal and marginal[p_phys]:
            out["drop_vs_pphys_step_percent"] = (1.0 - step / marginal[p_phys]) * 100.0
    if above:
        out["mean_marginal_above_pphys"] = sum(above) / len(above)
    return out


def print_strong(rows: List[Dict[str, Any]]) -> None:
    harness.log(harness.render_table(
        ["p", "T ms", "Speedup", "Efficiency", "Amdahl S(p)", "Stage A ms"],
        [(r["p"], f"{r['median_ms']:.1f}", f"{r['speedup']:.3f}", f"{r['efficiency']:.3f}",
          f"{r['amdahl_speedup']:.3f}", f"{r['stage_a_ms']:.1f}") for r in rows]))


def print_weak(rows: List[Dict[str, Any]]) -> None:
    harness.log(harness.render_table(
        ["p", "N(p)", "T ms", "E_weak", "alpha", "S_Gustafson", "Stage A ms"],
        [(r["p"], r["n"], f"{r['median_ms']:.1f}", f"{r['weak_efficiency']:.3f}", f"{r['alpha']:.4f}",
          f"{r['gustafson_speedup']:.3f}", f"{r['stage_a_ms']:.1f}") for r in rows]))


# ==========================================================================
# Main
# ==========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = harness.common_parser("Phase 2: strong vs weak scaling")
    args = parser.parse_args(argv)
    try:
        hw = harness.init_phase(args, "phase2")
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    mode = harness.get_mode()
    p_phys = int(hw["physical_cores"])
    p_log = mp.cpu_count()
    n_strong = harness.pick(1_600_000, 160_000)
    n0 = harness.pick(250_000, 25_000)
    warmup = harness.pick(1, 1)
    timed = harness.pick(5, 3)
    harness.log(f"=== PHASE 2: Scaling (mode={mode}, N={n_strong:,}, N0={n0:,}, P_phys={p_phys}, P_log={p_log}) ===")
    harness.mode_banner()
    harness.confirm_run(estimate(n_strong, n0, p_log, warmup, timed), args)

    strong: List[Dict[str, Any]] = []
    weak: List[Dict[str, Any]] = []
    try:
        harness.log("\n[1/2] Experiment 2A: strong scaling")
        for p in range(1, p_log + 1):
            row = run_config(p, n_strong, warmup, timed)
            strong.append(row)
            harness.log(f"  p={p:>2}: {row['median_ms']:.1f} ms (stage A {row['stage_a_ms']:.1f} ms)")
        harness.log("\n[2/2] Experiment 2B: weak scaling")
        for p in range(1, p_log + 1):
            row = run_config(p, p * n0, warmup, timed)
            weak.append(row)
            harness.log(f"  p={p:>2}: N={p * n0:,} -> {row['median_ms']:.1f} ms (stage A {row['stage_a_ms']:.1f} ms)")
    except KeyboardInterrupt:
        print("\n[abort] interrupted; pools were terminated. Re-run to start again.", file=sys.stderr)
        return 130

    # ---- derived quantities --------------------------------------------
    t1 = strong[0]["median_ms"]
    s_serial = strong[0]["stage_a_ms"] / t1
    s_inf = 1.0 / s_serial
    for r in strong:
        r["speedup"] = t1 / r["median_ms"]
        r["efficiency"] = r["speedup"] / r["p"]
        r["amdahl_speedup"] = 1.0 / (s_serial + (1.0 - s_serial) / r["p"])
    chk0 = strong[0]["checksum"]
    checksum_ok = all(abs(r["checksum"] - chk0) <= 1e-9 * abs(chk0) for r in strong)
    tw1 = weak[0]["median_ms"]
    for r in weak:
        r["weak_efficiency"] = tw1 / r["median_ms"]
        r["alpha"] = r["stage_a_ms"] / r["median_ms"]
        r["gustafson_speedup"] = r["p"] - r["alpha"] * (r["p"] - 1)

    s_obs = strong[-1]["speedup"]
    s_amdahl_plog = strong[-1]["amdahl_speedup"]
    s_gust_plog = weak[-1]["gustafson_speedup"]
    marg = marginal_analysis(strong, p_phys, p_log)

    observations = [
        f"Measured serial fraction s = T_StageA(1)/T_strong(1) = {s_serial:.4f}; Amdahl limit S_inf = {s_inf:.2f}.",
        f"At p = P_log = {p_log}: observed strong speedup {s_obs:.3f} vs Amdahl prediction {s_amdahl_plog:.3f} "
        f"(observed/predicted = {s_obs / s_amdahl_plog:.2f}).",
        f"Weak scaling at p = {p_log}: efficiency {weak[-1]['weak_efficiency']:.3f}, "
        f"alpha = {weak[-1]['alpha']:.4f}, Gustafson speedup {s_gust_plog:.3f}.",
    ]
    if "drop_vs_mean_below_percent" in marg and marg["drop_vs_mean_below_percent"] is not None:
        observations.append(
            f"Marginal speedup per added worker: mean {marg['mean_marginal_p2_to_pphys']:.3f} for p=2..{p_phys}, "
            f"{marg['marginal_at_pphys_plus_1']:.3f} at p={p_phys + 1} "
            f"({marg['drop_vs_mean_below_percent']:.1f}% lower than the pre-P_phys mean).")
    elif "note" in marg:
        observations.append(marg["note"])
    if not checksum_ok:
        observations.append("WARNING: checksums differ between p values beyond fp tolerance.")

    verify = harness.make_verify_record("PHASE2", hw["student_seed"], s_obs, s_gust_plog)
    result = {
        "phase": 2, "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameters": {"N_strong": n_strong, "N0_weak": n0, "warmup": warmup, "timed": timed,
                       "P_phys": p_phys, "P_log": p_log, "stage_a_fraction": STAGE_A_FRACTION,
                       "stage_b_iterations": STAGE_B_ITERS},
        "strong": strong, "weak": weak,
        "serial_fraction": s_serial, "amdahl_s_inf": s_inf,
        "amdahl_speedup_at_plog": s_amdahl_plog, "observed_strong_speedup_at_plog": s_obs,
        "gustafson_speedup_at_plog": s_gust_plog, "marginal": marg,
        "checksum_consistent": checksum_ok, "observations": observations, "verify": verify,
    }
    path = harness.save_json("phase2", result)

    harness.log("\n--- Experiment 2A: strong scaling ---")
    print_strong(strong)
    harness.log("\n--- Experiment 2B: weak scaling ---")
    print_weak(weak)
    for line in observations:
        harness.log(f"[obs] {line}")
    harness.log("\n" + verify["tag"])
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
