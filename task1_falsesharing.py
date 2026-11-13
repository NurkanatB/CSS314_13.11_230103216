"""
task1_falsesharing.py - Phase 1: false sharing & cache-line contention.

Usage (PowerShell, inside parallel_lab/):
    python task1_falsesharing.py            # official parameters (asks for confirmation)
    python task1_falsesharing.py --yes      # no confirmation prompt
    python task1_falsesharing.py --quick    # reduced debug run (hash NOT valid)
    python task1_falsesharing.py --force    # re-run although results/phase1.json exists

Workload: M = 24,000,000 total read-modify-write steps split over p worker processes
(M // p each), every worker updating its own 64-bit slot `t * stride` in ONE 4096-byte
SharedMemory block through ctypes.c_uint64.

Measurement note (documented deviation / refinement of the handout):
  Workers attach, then wait on a barrier, then time ONLY their loop with perf_counter_ns().
  The primary time of a trial is max(worker loop times) ("kernel time"), which excludes
  process start-up (50-300 ms per process on Windows 'spawn' - larger than the signal we
  want to see). The wall time including spawn/join is recorded too (`wall_*`).
  Tables use kernel time; harness.run_benchmark(warmup_runs=2, timed_runs=7) drives the trials.

Estimated runtime (full): roughly 3-10 minutes; memory: ~30-40 MB per worker process.
VERIFY_HASH[PHASE1]: metric1 = median kernel ms (stride=1, p=P_phys),
                     metric2 = median kernel ms (stride=16, p=P_phys).
"""
from __future__ import annotations

import ctypes
import multiprocessing as mp
import struct
import sys
import time
from multiprocessing import shared_memory
from typing import Any, Dict, List, Optional, Tuple

import harness

SHM_BYTES = 4096                      # required buffer size
SLOTS = SHM_BYTES // 8                # 512 x uint64
STRIDES = [1, 2, 4, 8, 16, 32]        # 8 .. 256 bytes between slots
MASK = 0xFFFFFFFFFFFFFFFF
MULT = 6364136223846793005
BARRIER_TIMEOUT = 120.0


# ==========================================================================
# Worker (module level => picklable under Windows 'spawn')
# ==========================================================================
def fs_worker(shm_name: str, slot_idx: int, iters: int,
              barrier: Any = None, times: Any = None, worker_id: int = 0) -> None:
    """Deterministic LCG accumulation, one store to shared slot per iteration.

    barrier/times are optional: if given, the loop start is synchronised and its
    duration (ns) is stored in times[worker_id].
    """
    shm = shared_memory.SharedMemory(name=shm_name)
    arr = None
    try:
        arr = (ctypes.c_uint64 * SLOTS).from_buffer(shm.buf)
        acc = arr[slot_idx]
        mask = MASK
        mult = MULT
        if barrier is not None:
            barrier.wait(BARRIER_TIMEOUT)
        t0 = time.perf_counter_ns()
        for i in range(iters):
            acc = (acc * mult + (i | 1)) & mask
            arr[slot_idx] = acc            # direct store to the (possibly shared) cache line
        t1 = time.perf_counter_ns()
        if times is not None:
            times[worker_id] = t1 - t0
    except BaseException:
        if barrier is not None:
            try:
                barrier.abort()
            except Exception:
                pass
        raise
    finally:
        arr = None                         # drop ctypes export before closing the mapping
        try:
            shm.close()
        except BufferError:
            pass


# ==========================================================================
# One trial / one configuration
# ==========================================================================
def run_once(shm: shared_memory.SharedMemory, p: int, stride: int,
             iters: int) -> Tuple[float, float, bool]:
    """Run p workers once. Returns (kernel_ms, wall_ms, values_consistent)."""
    shm.buf[:SHM_BYTES] = bytes(SHM_BYTES)                 # reset all slots to 0
    barrier = mp.Barrier(p)
    times = mp.RawArray("q", p)
    procs = [mp.Process(target=fs_worker,
                        args=(shm.name, t * stride, iters, barrier, times, t))
             for t in range(p)]
    t0 = time.perf_counter_ns()
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    wall_ms = (time.perf_counter_ns() - t0) / 1e6
    bad = [pr.exitcode for pr in procs if pr.exitcode != 0]
    if bad:
        raise RuntimeError(f"worker failure, exit codes: {bad}")
    kernel_ms = max(times[:]) / 1e6
    values = [struct.unpack_from("<Q", shm.buf, t * stride * 8)[0] for t in range(p)]
    consistent = len(set(values)) == 1                     # identical recurrence in every worker
    return kernel_ms, wall_ms, consistent


def measure(shm: shared_memory.SharedMemory, p: int, stride: int, total_iters: int,
            warmup: int, timed: int) -> Dict[str, Any]:
    """Benchmark one (p, stride) configuration."""
    iters = total_iters // p
    kernel: List[float] = []
    consistent_flags: List[bool] = []

    def trial() -> None:
        k_ms, _w, ok = run_once(shm, p, stride, iters)
        kernel.append(k_ms)
        consistent_flags.append(ok)

    wall_stats = harness.run_benchmark(trial, warmup_runs=warmup, timed_runs=timed)
    k_stats = harness.bench_stats_from_samples(kernel[warmup:])
    total_ops = iters * p
    return {
        "p": p, "stride": stride, "byte_separation": stride * 8,
        "iters_per_worker": iters, "total_ops": total_ops,
        "median_ms": k_stats.median_ms, "iqr_ms": k_stats.iqr_ms,
        "min_ms": k_stats.min_ms, "max_ms": k_stats.max_ms,
        "throughput_mops": total_ops / (k_stats.median_ms * 1e3),
        "wall_median_ms": wall_stats.median_ms,
        "values_consistent": all(consistent_flags),
    }


def karp_flatt(speedup: float, p: int) -> Optional[float]:
    """Experimentally determined serial fraction e(p) = (1/S - 1/p) / (1 - 1/p), p >= 2."""
    if p < 2:
        return None
    return (1.0 / speedup - 1.0 / p) / (1.0 - 1.0 / p)


def fits(stride: int, p: int) -> bool:
    """True if the slots 0, stride, ..., (p-1)*stride fit into the 512-slot buffer."""
    return stride * (p - 1) < SLOTS


def process_counts(p_phys: int, p_log: int) -> List[int]:
    """1, 2, 4, ... <= P_log, plus P_phys and P_log themselves (needed for the hash / ceiling)."""
    counts = set()
    p = 1
    while p <= p_log:
        counts.add(p)
        p *= 2
    counts.update({p_phys, p_log})
    return sorted(counts)


# ==========================================================================
# Estimation
# ==========================================================================
def calibrate_ns_per_iter(n: int = 400_000) -> float:
    """Single-process cost of the worker loop (local list instead of shared memory)."""
    buf = [0] * 8
    acc = 0
    t0 = time.perf_counter_ns()
    for i in range(n):
        acc = (acc * MULT + (i | 1)) & MASK
        buf[0] = acc
    return (time.perf_counter_ns() - t0) / n


def estimate(m: int, counts: List[int], p_phys: int, warmup: int, timed: int) -> str:
    """Rough runtime / memory estimate for the whole phase."""
    ns = calibrate_ns_per_iter()
    runs = warmup + timed
    spawn_s = 0.15
    total = sum(m / p * ns / 1e9 + spawn_s * p for p in counts) * runs * 2           # two scaling sweeps
    total += sum(m / p_phys * ns / 1e9 + spawn_s * p_phys for _ in STRIDES) * runs   # stride sweep
    return (f"[estimate] loop cost ~{ns:.0f} ns/iter -> p=1 trial ~{m * ns / 1e9:.1f} s; "
            f"whole phase ~{total / 60:.1f} min (uncontended lower bound; false sharing makes it slower).\n"
            f"[estimate] memory: 4 KB shared + ~{max(counts) * 35} MB for the largest worker set.")


# ==========================================================================
# Analysis helpers
# ==========================================================================
def find_inflection(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Largest relative time drop between consecutive strides in Table 1A."""
    best = None
    for prev, cur in zip(rows, rows[1:]):
        ratio = cur["median_ms"] / prev["median_ms"]
        if best is None or ratio < best["ratio"]:
            best = {"from_stride": prev["stride"], "to_stride": cur["stride"],
                    "from_bytes": prev["byte_separation"], "to_bytes": cur["byte_separation"],
                    "ratio": ratio}
    if best is None:
        return {"found": False}
    best["drop_percent"] = (1.0 - best["ratio"]) * 100.0
    best["found"] = best["drop_percent"] >= 15.0
    return best


def print_table_1a(rows: List[Dict[str, Any]]) -> None:
    harness.log(harness.render_table(
        ["Stride", "Byte sep.", "Median ms", "IQR ms", "Mops/s", "Wall median ms"],
        [(r["stride"], r["byte_separation"], f"{r['median_ms']:.2f}", f"{r['iqr_ms']:.2f}",
          f"{r['throughput_mops']:.2f}", f"{r['wall_median_ms']:.2f}") for r in rows]))


def print_table_1b(title: str, rows: List[Dict[str, Any]]) -> None:
    harness.log(title)
    harness.log(harness.render_table(
        ["p", "Median ms", "Speedup Sp", "Efficiency Ep", "Karp-Flatt e(p)"],
        [(r["p"], f"{r['median_ms']:.2f}", f"{r['speedup']:.3f}", f"{r['efficiency']:.3f}",
          "n/a" if r["karp_flatt"] is None else f"{r['karp_flatt']:.4f}") for r in rows]))


# ==========================================================================
# Main
# ==========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = harness.common_parser("Phase 1: false sharing & Karp-Flatt")
    args = parser.parse_args(argv)
    try:
        hw = harness.init_phase(args, "phase1")
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    mode = harness.get_mode()
    p_phys = int(hw["physical_cores"])
    p_log = mp.cpu_count()
    if p_phys < 2:
        print("[error] Phase 1 needs at least 2 physical cores to expose false sharing.", file=sys.stderr)
        return 2

    total_iters = harness.pick(24_000_000, 2_400_000)
    warmup = harness.pick(2, 1)
    timed = harness.pick(7, 3)
    counts = process_counts(p_phys, p_log)
    harness.log(f"=== PHASE 1: False sharing (mode={mode}, M={total_iters:,}, P_phys={p_phys}, P_log={p_log}) ===")
    harness.mode_banner()
    harness.confirm_run(estimate(total_iters, counts, p_phys, warmup, timed), args)

    table_1a: List[Dict[str, Any]] = []
    scaling: Dict[int, List[Dict[str, Any]]] = {1: [], 16: []}
    skipped: List[str] = []
    try:
        with harness.managed_shared_memory(SHM_BYTES) as shm:
            harness.log(f"\n[1/2] Stride sweep at p = P_phys = {p_phys}")
            for stride in STRIDES:
                if not fits(stride, p_phys):
                    msg = f"stride={stride} at p={p_phys} needs slot {stride * (p_phys - 1)} >= {SLOTS}: skipped"
                    skipped.append(msg)
                    harness.log(f"  [skip] {msg}")
                    continue
                row = measure(shm, p_phys, stride, total_iters, warmup, timed)
                table_1a.append(row)
                harness.log(f"  stride={stride:>2} ({stride * 8:>3} B): median {row['median_ms']:.2f} ms, "
                            f"{row['throughput_mops']:.2f} Mops/s, consistent={row['values_consistent']}")

            harness.log("\n[2/2] Process-count sweep (stride=1 and stride=16)")
            for stride in (1, 16):
                for p in counts:
                    if not fits(stride, p):
                        msg = f"stride={stride} at p={p} does not fit the 512-slot buffer: skipped"
                        skipped.append(msg)
                        harness.log(f"  [skip] {msg}")
                        continue
                    row = measure(shm, p, stride, total_iters, warmup, timed)
                    scaling[stride].append(row)
                    harness.log(f"  stride={stride:>2} p={p:>2}: median {row['median_ms']:.2f} ms")
    except KeyboardInterrupt:
        print("\n[abort] interrupted; shared memory was cleaned up. Re-run to start again.", file=sys.stderr)
        return 130

    # ---- Table 1B -------------------------------------------------------
    table_1b: Dict[str, List[Dict[str, Any]]] = {}
    for stride, rows in scaling.items():
        base = next((r for r in rows if r["p"] == 1), None)
        out_rows = []
        for r in rows:
            speedup = base["median_ms"] / r["median_ms"] if base else float("nan")
            out_rows.append({"p": r["p"], "median_ms": r["median_ms"], "iqr_ms": r["iqr_ms"],
                             "speedup": speedup, "efficiency": speedup / r["p"],
                             "karp_flatt": karp_flatt(speedup, r["p"]) if base else None,
                             "values_consistent": r["values_consistent"]})
        table_1b[f"stride_{stride}"] = out_rows

    # ---- observations ---------------------------------------------------
    observations: List[str] = []
    infl = find_inflection(table_1a)
    if infl.get("found"):
        observations.append(
            f"Largest execution-time drop in the stride sweep: stride {infl['from_stride']} -> {infl['to_stride']} "
            f"({infl['from_bytes']} B -> {infl['to_bytes']} B separation), time x{infl['ratio']:.2f} "
            f"({infl['drop_percent']:.0f}% faster). Reported cache-line size: {hw.get('cache_line_bytes')} B.")
    elif infl.get("from_stride") is not None:
        observations.append(
            f"No precipitous drop observed: the largest step is only {infl['drop_percent']:.0f}% "
            f"(stride {infl['from_stride']} -> {infl['to_stride']}). In pure Python the interpreter overhead per "
            f"iteration can hide coherence traffic; report this honestly instead of forcing a conclusion.")
    s1 = next((r for r in table_1a if r["stride"] == 1), None)
    s16 = next((r for r in table_1a if r["stride"] == 16), None)
    if s1 and s16:
        observations.append(f"At p={p_phys}: stride=1 median {s1['median_ms']:.2f} ms vs stride=16 median "
                            f"{s16['median_ms']:.2f} ms (ratio {s1['median_ms'] / s16['median_ms']:.2f}x).")
    for key, rows in table_1b.items():
        kf = [r for r in rows if r["karp_flatt"] is not None]
        if len(kf) >= 2:
            observations.append(f"{key}: e(p) goes from {kf[0]['karp_flatt']:.4f} at p={kf[0]['p']} "
                                f"to {kf[-1]['karp_flatt']:.4f} at p={kf[-1]['p']}.")
    if not all(r["values_consistent"] for r in table_1a):
        observations.append("WARNING: some trials produced inconsistent slot values (possible race/bug).")

    # ---- hash -----------------------------------------------------------
    verify = None
    if s1 and s16:
        verify = harness.make_verify_record("PHASE1", hw["student_seed"], s1["median_ms"], s16["median_ms"])
    result = {
        "phase": 1, "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameters": {"M": total_iters, "warmup": warmup, "timed": timed, "P_phys": p_phys, "P_log": p_log,
                       "p_values": counts, "strides": STRIDES, "shm_bytes": SHM_BYTES,
                       "time_basis": "max worker loop time after a start barrier (kernel time)"},
        "table_1a": table_1a, "table_1b": table_1b, "inflection": infl,
        "observations": observations, "skipped": skipped, "verify": verify,
    }
    path = harness.save_json("phase1", result)

    harness.log("\n--- Table 1A: stride sensitivity (p = P_phys) ---")
    print_table_1a(table_1a)
    harness.log("")
    print_table_1b("--- Table 1B (stride = 1, severe false sharing) ---", table_1b["stride_1"])
    harness.log("")
    print_table_1b("--- Table 1B (stride = 16, 128-byte isolation) ---", table_1b["stride_16"])
    for line in observations:
        harness.log(f"[obs] {line}")
    harness.log("\n" + (verify["tag"] if verify else "VERIFY_HASH[PHASE1]: not computed (stride 1/16 missing)"))
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
