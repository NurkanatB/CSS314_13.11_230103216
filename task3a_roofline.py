"""
task3a_roofline.py - Phase 3A: empirical machine roofline in pure Python.

Usage (PowerShell, inside parallel_lab/):
    python task3a_roofline.py            # official parameters (asks for confirmation)
    python task3a_roofline.py --yes
    python task3a_roofline.py --quick    # 16 MB buffers, fewer FLOPs (hash/values NOT valid)
    python task3a_roofline.py --force
    python task3a_roofline.py --flops-per-worker 2.56e9   # see "spec note" below

(1) Sustained DRAM bandwidth: two 96e6-byte SharedMemory buffers (192 MB total, > L3).
    A persistent pool of P_phys workers (attached once, outside the timed region) starts
    together on a barrier; each copies its slice with  mv_a[s:e] = mv_b[s:e]  (native memcpy).
    T = max over workers of the in-worker copy time (barrier release -> copy done), median of
    15 trials.  BW = 2 * 96e6 B / (T * 1e9)  GB/s  (1 read + 1 write byte per byte copied;
    write-allocate traffic is NOT counted, so this is the handout's definition).
(2) Sustained Python compute ceiling: P_phys workers, each executes 8.0e7 FLOPs as
    16 unrolled scalar updates  x_k = x_k * a + b  (mul + add = 2 FLOP per statement,
    32 FLOP per loop pass) -> 2.5e6 passes.  GFLOP/s = P_phys * FLOP_per_worker / (T * 1e9).
(3) Ridge point I* = P_sustained / BW_sustained  (FLOP/Byte).

SPEC NOTE: the handout says "8.0e7 fused multiply-add FLOPs per worker"; the master prompt
says "80,000,000 iterations per worker" (= 2.56e9 FLOP with 16 unrolled FMAs, ~1 min per trial).
These conflict. The handout is authoritative -> default 8.0e7 FLOP; use --flops-per-worker
to change it (the value used is recorded in the JSON).

Estimated runtime (full): ~20-60 s.  Memory: 192 MB shared + small per-worker overhead.
"""
from __future__ import annotations

import math
import multiprocessing as mp
import sys
import time
from multiprocessing import shared_memory, util
from typing import Any, Dict, List, Optional, Tuple

import harness

BUFFER_BYTES_FULL = 96_000_000
FLOPS_PER_PASS = 32                      # 16 statements x (mul + add)

_STATE: Dict[str, Any] = {}


# ==========================================================================
# Bandwidth worker side
# ==========================================================================
def _bw_cleanup() -> None:
    """Release worker-side mappings (registered with multiprocessing.util.Finalize)."""
    for key in ("mv_a", "mv_b"):
        _STATE.pop(key, None)
    for shm in _STATE.pop("shms", []):
        try:
            shm.close()
        except BufferError:
            pass


def _bw_init(name_a: str, name_b: str) -> None:
    """Pool initializer: attach to both buffers once (steady-state page tables)."""
    shm_a = shared_memory.SharedMemory(name=name_a)
    shm_b = shared_memory.SharedMemory(name=name_b)
    _STATE["shms"] = [shm_a, shm_b]
    _STATE["mv_a"] = shm_a.buf
    _STATE["mv_b"] = shm_b.buf
    util.Finalize(None, _bw_cleanup, exitpriority=10)


def bw_task(bounds: Tuple[int, int]) -> int:
    """Copy B[start:end] -> A[start:end]; returns elapsed ns measured inside the worker."""
    harness.sync_point()
    start, end = bounds
    mv_a, mv_b = _STATE["mv_a"], _STATE["mv_b"]
    t0 = time.perf_counter_ns()
    mv_a[start:end] = mv_b[start:end]
    return time.perf_counter_ns() - t0


# ==========================================================================
# Compute worker side
# ==========================================================================
def fma_task(passes: int) -> Tuple[int, float]:
    """16 independent scalar chains x_k = x_k*a + b, `passes` times. Returns (ns, checksum)."""
    harness.sync_point()
    a = 0.999999
    b = 1e-7
    x0, x1, x2, x3 = 0.10, 0.11, 0.12, 0.13
    x4, x5, x6, x7 = 0.14, 0.15, 0.16, 0.17
    x8, x9, x10, x11 = 0.18, 0.19, 0.20, 0.21
    x12, x13, x14, x15 = 0.22, 0.23, 0.24, 0.25
    t0 = time.perf_counter_ns()
    for _ in range(passes):
        x0 = x0 * a + b
        x1 = x1 * a + b
        x2 = x2 * a + b
        x3 = x3 * a + b
        x4 = x4 * a + b
        x5 = x5 * a + b
        x6 = x6 * a + b
        x7 = x7 * a + b
        x8 = x8 * a + b
        x9 = x9 * a + b
        x10 = x10 * a + b
        x11 = x11 * a + b
        x12 = x12 * a + b
        x13 = x13 * a + b
        x14 = x14 * a + b
        x15 = x15 * a + b
    elapsed = time.perf_counter_ns() - t0
    return elapsed, x0 + x1 + x2 + x3 + x4 + x5 + x6 + x7 + x8 + x9 + x10 + x11 + x12 + x13 + x14 + x15


# ==========================================================================
# Parent-side drivers
# ==========================================================================
def fill(buf: memoryview, size: int, value: int) -> None:
    """Write a constant byte pattern in 8 MB blocks (touches every page)."""
    block = bytes([value]) * min(size, 8 * 1024 * 1024)
    pos = 0
    while pos < size:
        n = min(len(block), size - pos)
        buf[pos:pos + n] = block[:n]
        pos += n


def measure_bandwidth(p: int, size: int, warmup: int, timed: int) -> Dict[str, Any]:
    """Run the parallel memcpy benchmark; returns statistics and BW (GB/s)."""
    with harness.managed_shared_memory(size) as shm_a, harness.managed_shared_memory(size) as shm_b:
        fill(shm_a.buf, size, 0x11)
        fill(shm_b.buf, size, 0x5A)
        pool = harness.create_synced_pool(p, _bw_init, (shm_a.name, shm_b.name))
        ranges = harness.balanced_ranges(size, p)
        kernel_ms: List[float] = []
        median_worker_ms: List[float] = []
        try:
            for run in range(warmup + timed):
                ns = pool.map(bw_task, ranges, chunksize=1)
                if run >= warmup:
                    kernel_ms.append(max(ns) / 1e6)
                    median_worker_ms.append(harness.median([x / 1e6 for x in ns]))
            pool.close()
            pool.join()
        except BaseException:
            pool.terminate()
            pool.join()
            raise
        copy_ok = shm_a.buf[:size] == shm_b.buf[:size]       # verify the copy really happened
    stats = harness.bench_stats_from_samples(kernel_ms)
    bw = 2.0 * size / (stats.median_ms / 1e3) / 1e9
    return {"buffer_bytes": size, "workers": p, "stats": stats.to_dict(),
            "median_of_worker_median_ms": harness.median(median_worker_ms),
            "bandwidth_gbs": bw, "copy_verified": bool(copy_ok), "trials_ms": kernel_ms}


def measure_compute(p: int, flops_per_worker: float, warmup: int, timed: int) -> Dict[str, Any]:
    """Run the 16-way unrolled FMA benchmark; returns statistics and GFLOP/s."""
    passes = max(1, math.ceil(flops_per_worker / FLOPS_PER_PASS))
    actual_flops = passes * FLOPS_PER_PASS
    pool = harness.create_synced_pool(p)
    kernel_ms: List[float] = []
    checks: List[float] = []
    try:
        for run in range(warmup + timed):
            res = pool.map(fma_task, [passes] * p, chunksize=1)
            if run >= warmup:
                kernel_ms.append(max(r[0] for r in res) / 1e6)
                checks = [r[1] for r in res]
        pool.close()
        pool.join()
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    stats = harness.bench_stats_from_samples(kernel_ms)
    gflops = p * actual_flops / (stats.median_ms / 1e3) / 1e9
    return {"workers": p, "passes_per_worker": passes, "flop_per_worker": actual_flops,
            "stats": stats.to_dict(), "gflops": gflops, "checksum_workers": checks, "trials_ms": kernel_ms}


def main(argv: Optional[List[str]] = None) -> int:
    parser = harness.common_parser("Phase 3A: empirical roofline")
    parser.add_argument("--flops-per-worker", type=float, default=None,
                        help="override FLOPs per worker (default 8.0e7 as in the handout)")
    args = parser.parse_args(argv)
    try:
        hw = harness.init_phase(args, "phase3a")
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    mode = harness.get_mode()
    p_phys = int(hw["physical_cores"])
    size = harness.pick(BUFFER_BYTES_FULL, 16_000_000)
    flops = args.flops_per_worker or harness.pick(8.0e7, 8.0e6)
    bw_warm, bw_timed = harness.pick(3, 1), harness.pick(15, 5)
    fl_warm, fl_timed = harness.pick(1, 1), harness.pick(7, 3)
    harness.log(f"=== PHASE 3A: Roofline (mode={mode}, P_phys={p_phys}, buffers 2 x {size / 1e6:.0f} MB) ===")
    harness.mode_banner()
    est_flop_s = flops / FLOPS_PER_PASS * 16 * 40e-9 * (fl_warm + fl_timed)
    harness.confirm_run(f"[estimate] ~{est_flop_s + 10:.0f} s total (compute ~{est_flop_s:.0f} s at an assumed 40 ns per "
                        f"statement); memory ~{2 * size / 1e6:.0f} MB shared + {p_phys * 35} MB workers.", args)
    l3_mb = hw.get("l3_mb")
    if l3_mb and 2 * size / 1e6 < l3_mb:
        harness.log(f"[warn] 192 MB footprint is NOT larger than reported L3 ({l3_mb:g} MB): bandwidth may be cache-resident.")

    try:
        harness.log("\n[1/2] Sustained DRAM bandwidth ...")
        bw = measure_bandwidth(p_phys, size, bw_warm, bw_timed)
        harness.log(f"  median copy time {bw['stats']['median_ms']:.3f} ms -> BW = {bw['bandwidth_gbs']:.2f} GB/s "
                    f"(copy verified: {bw['copy_verified']})")
        harness.log("\n[2/2] Sustained Python compute ceiling ...")
        comp = measure_compute(p_phys, flops, fl_warm, fl_timed)
        harness.log(f"  median kernel time {comp['stats']['median_ms']:.1f} ms -> P = {comp['gflops']:.4f} GFLOP/s")
    except KeyboardInterrupt:
        print("\n[abort] interrupted; shared memory cleaned up. Re-run to start again.", file=sys.stderr)
        return 130

    ridge = comp["gflops"] / bw["bandwidth_gbs"]
    limitations = [
        "FLOPs are counted by statement (mul+add = 2 FLOP), not measured by hardware counters.",
        "CPython executes ~tens of ns per bytecode-level operation, so P_sustained measures INTERPRETER dispatch "
        "throughput, orders of magnitude below the silicon's FMA peak; the 'roofline' is an interpreter roofline.",
        "Bandwidth uses native memcpy (memoryview slice copy) and is therefore a true memory-system measurement, "
        "while the compute ceiling is not - the two ceilings come from different 'machines'.",
        "Write-allocate traffic is not counted in BW (handout definition: 2 bytes per byte copied).",
        "Time per trial is max over workers of in-worker time after a start barrier; dispatch overhead excluded.",
    ]
    result = {
        "phase": "3A", "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameters": {"P_phys": p_phys, "buffer_bytes": size, "flop_per_worker_requested": flops,
                       "bw_warmup": bw_warm, "bw_timed": bw_timed, "compute_warmup": fl_warm,
                       "compute_timed": fl_timed,
                       "spec_note": "handout 8.0e7 FLOP/worker vs master prompt 80,000,000 iterations: handout used"},
        "bandwidth": bw, "compute": comp,
        "bw_sustained_gbs": bw["bandwidth_gbs"], "p_sustained_gflops": comp["gflops"],
        "ridge_point_flop_per_byte": ridge, "limitations": limitations,
    }
    path = harness.save_json("phase3a", result)

    harness.log("\n--- Results ---")
    harness.log(harness.render_table(["Quantity", "Value"], [
        ("BW_sustained", f"{bw['bandwidth_gbs']:.3f} GB/s"),
        ("P_sustained", f"{comp['gflops']:.5f} GFLOP/s"),
        ("Ridge point I*", f"{ridge:.6f} FLOP/Byte")]))
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
