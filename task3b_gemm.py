"""
task3b_gemm.py - Phase 3B: progressive parallel GEMM optimisation and 10-point tile sweep.

Usage (PowerShell, inside parallel_lab/):
    python task3b_gemm.py            # official parameters (N = 384, asks for confirmation)
    python task3b_gemm.py --yes
    python task3b_gemm.py --quick    # N = 96, fewer runs (hash NOT valid)
    python task3b_gemm.py --force

C = A x B, double precision, flat 1-D buffers in ONE SharedMemory block (3 * N^2 * 8 B = 3.54 MB
for N = 384), rows partitioned over P_phys workers of a persistent "synced" pool.
  Variant 1: naive i-j-k, dot += A[i*N+k] * B[k*N+j]  (stride-N walk over B).
  Variant 2: i-k-j, a_ik hoisted, inner j loop over a contiguous row slice of B and C.
  Variant 3: 6-loop blocked (ii,kk,jj tiles of BS) with micro-kernel order i,k,j, a_ik and
             the C/B tile slices hoisted out of the innermost loop.
All variants add the products for each C[i][j] in the same k order, so results are bitwise
equal; max|C3 - C2| must be < 1e-9 (checked), plus an independent spot-check of sampled entries.

Pure-Python caveat (printed in the report too): Python lists/arrays hold boxed floats, so cache
behaviour is dominated by interpreter overhead. The optimum BS may therefore NOT match the
cache-capacity prediction - report what you measure.

Estimated runtime (full): ~2-8 minutes depending on P_phys. Memory: 3.6 MB shared + workers.
VERIFY_HASH[PHASE3]: metric1 = GFLOP/s of Variant 1, metric2 = GFLOP/s of Variant 3 at BS*.
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from array import array
from multiprocessing import shared_memory
from typing import Any, Dict, List, Optional, Tuple

import harness

N_FULL = 384
TILE_SIZES = [4, 8, 12, 16, 24, 32, 48, 64, 96, 128]
TOLERANCE = 1e-9
SEED_A, SEED_B = 20240001, 20240002


# ==========================================================================
# Kernels (module level for 'spawn')
# ==========================================================================
def gemm_v1(A: memoryview, B: memoryview, C: memoryview, n: int, r0: int, r1: int) -> None:
    """Naive i-j-k. Inner loop walks B with stride n and recomputes index arithmetic."""
    for i in range(r0, r1):
        for j in range(n):
            dot = 0.0
            for k in range(n):
                dot += A[i * n + k] * B[k * n + j]
            C[i * n + j] = dot


def gemm_v2(A: memoryview, B: memoryview, C: memoryview, n: int, r0: int, r1: int) -> None:
    """Loop-reordered i-k-j: a_ik hoisted, unit-stride row slices of B and C."""
    b_rows = [B[k * n:(k + 1) * n].tolist() for k in range(n)]
    for i in range(r0, r1):
        crow = [0.0] * n
        base = i * n
        for k in range(n):
            a_ik = A[base + k]
            crow = [c + a_ik * b for c, b in zip(crow, b_rows[k])]
        C[base:base + n] = array("d", crow)


def gemm_v3(A: memoryview, B: memoryview, C: memoryview, n: int, bs: int, r0: int, r1: int) -> None:
    """6-loop cache-blocked GEMM; micro-kernel order (i,k,j) with tile slices hoisted."""
    b_rows = [B[k * n:(k + 1) * n].tolist() for k in range(n)]
    c_rows = [[0.0] * n for _ in range(r1 - r0)]
    for ii in range(r0, r1, bs):
        i_hi = min(ii + bs, r1)
        for kk in range(0, n, bs):
            k_hi = min(kk + bs, n)
            for jj in range(0, n, bs):
                j_hi = min(jj + bs, n)
                b_tile = [b_rows[k][jj:j_hi] for k in range(kk, k_hi)]      # packed B tile
                for i in range(ii, i_hi):
                    crow = c_rows[i - r0]
                    c_tile = crow[jj:j_hi]                                    # hoisted C tile slice
                    base = i * n + kk
                    for kt in range(k_hi - kk):
                        a_ik = A[base + kt]                                   # hoisted scalar
                        c_tile = [c + a_ik * b for c, b in zip(c_tile, b_tile[kt])]
                    crow[jj:j_hi] = c_tile
    for i in range(r0, r1):
        C[i * n:(i + 1) * n] = array("d", c_rows[i - r0])


def gemm_task(task: Tuple[int, int, int, int, str, int]) -> int:
    """Pool task: attach to shared block, run one variant on rows [r0, r1), detach."""
    variant, bs, r0, r1, shm_name, n = task
    harness.sync_point()
    nn = n * n
    shm = shared_memory.SharedMemory(name=shm_name)
    buf = A = B = C = None
    try:
        buf = shm.buf.cast("d")
        A, B, C = buf[0:nn], buf[nn:2 * nn], buf[2 * nn:3 * nn]
        if variant == 1:
            gemm_v1(A, B, C, n, r0, r1)
        elif variant == 2:
            gemm_v2(A, B, C, n, r0, r1)
        else:
            gemm_v3(A, B, C, n, bs, r0, r1)
    finally:
        for view in (A, B, C, buf):            # release derived views before closing the mapping
            if view is not None:
                view.release()
        shm.close()
    return r1 - r0


# ==========================================================================
# Parent-side helpers
# ==========================================================================
def snapshot_c(shm: shared_memory.SharedMemory, n: int) -> List[float]:
    """Copy C out of shared memory."""
    nn = n * n
    view = shm.buf.cast("d")
    c_view = view[2 * nn:3 * nn]
    try:
        return c_view.tolist()
    finally:
        c_view.release()
        view.release()


def zero_c(shm: shared_memory.SharedMemory, n: int) -> None:
    """Clear C so a skipped computation would be detected."""
    nn = n * n
    view = shm.buf.cast("d")
    c_view = view[2 * nn:3 * nn]
    try:
        c_view[:] = array("d", bytes(8 * nn))
    finally:
        c_view.release()
        view.release()


def spot_points(n: int, count: int = 24) -> List[Tuple[int, int]]:
    """Deterministic sample of (i, j) positions for the independent dot-product check."""
    return [((37 * t + 11) % n, (53 * t + 5) % n) for t in range(count)]


def spot_error(c: List[float], a: List[float], b: List[float], n: int) -> float:
    """Max |C[i][j] - dot(A[i,:], B[:,j])| over the sampled positions."""
    worst = 0.0
    for i, j in spot_points(n):
        ref = 0.0
        for k in range(n):
            ref += a[i * n + k] * b[k * n + j]
        worst = max(worst, abs(c[i * n + j] - ref))
    return worst


def bench_variant(pool: Any, shm: shared_memory.SharedMemory, n: int, p: int, variant: int,
                  bs: int, warmup: int, timed: int) -> Tuple[Dict[str, Any], List[float]]:
    """Benchmark one variant; returns (record, snapshot of C)."""
    ranges = harness.balanced_ranges(n, p)
    tasks = [(variant, bs, r0, r1, shm.name, n) for r0, r1 in ranges]
    zero_c(shm, n)
    stats = harness.run_benchmark(lambda: pool.map(gemm_task, tasks, chunksize=1),
                                  warmup_runs=warmup, timed_runs=timed)
    c = snapshot_c(shm, n)
    rec = {"variant": variant, "bs": bs if variant == 3 else None,
           "median_ms": stats.median_ms, "iqr_ms": stats.iqr_ms,
           "min_ms": stats.min_ms, "max_ms": stats.max_ms,
           "gflops": 2.0 * n ** 3 / (stats.median_ms * 1e6)}
    return rec, c


def max_abs_diff(x: List[float], y: List[float]) -> float:
    return max(abs(a - b) for a, b in zip(x, y))


def estimate(n: int, p: int, warmup: int, timed: int) -> str:
    """Calibrate the inner-loop costs and extrapolate."""
    m = 60_000
    A = [0.5] * 64
    t0 = time.perf_counter_ns()
    dot = 0.0
    for k in range(m):
        dot += A[k & 63] * A[(k + 1) & 63]
    ns_v1 = (time.perf_counter_ns() - t0) / m
    row = [0.5] * 4096
    t0 = time.perf_counter_ns()
    for _ in range(m // 4096 + 1):
        row = [c + 0.3 * b for c, b in zip(row, row)]
    ns_v2 = (time.perf_counter_ns() - t0) / ((m // 4096 + 1) * 4096)
    work = n ** 3
    runs = warmup + timed
    total = (work * ns_v1 + work * ns_v2 * 1.0 + len(TILE_SIZES) * work * ns_v2 * 1.6) / 1e9 / p * runs
    return (f"[estimate] V1 ~{work * ns_v1 / 1e9 / p:.1f} s/trial, V2 ~{work * ns_v2 / 1e9 / p:.1f} s/trial "
            f"(ideal split over {p} workers); whole phase ~{total / 60:.1f} min.\n"
            f"[estimate] memory ~{3 * n * n * 8 / 1e6:.1f} MB shared + ~{p * 40} MB worker overhead.")


def roofline_mapping(rec_v1: Dict[str, Any], rec_v2: Dict[str, Any], rec_v3: Dict[str, Any],
                     bs_star: int, phase3a: Dict[str, Any]) -> Dict[str, Any]:
    """Place the variants on the measured Phase-3A roofline."""
    peak = phase3a["p_sustained_gflops"]
    bw = phase3a["bw_sustained_gbs"]
    ridge = phase3a["ridge_point_flop_per_byte"]
    rows = []
    for label, rec, oi in (("Variant 1 (naive)", rec_v1, 0.125),
                           ("Variant 2 (i-k-j)", rec_v2, 0.125),
                           (f"Variant 3 (BS*={bs_star})", rec_v3, bs_star / 12.0)):
        attainable = min(peak, oi * bw)
        rows.append({"label": label, "intensity": oi, "achieved_gflops": rec["gflops"],
                     "attainable_gflops": attainable, "percent_of_attainable": 100.0 * rec["gflops"] / attainable,
                     "region": "compute-bound" if oi >= ridge else "memory-bound"})
    return {"peak_gflops": peak, "bw_gbs": bw, "ridge": ridge, "rows": rows}


def main(argv: Optional[List[str]] = None) -> int:
    parser = harness.common_parser("Phase 3B: GEMM optimisation and tile sweep")
    args = parser.parse_args(argv)
    try:
        hw = harness.init_phase(args, "phase3b")
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    mode = harness.get_mode()
    p = int(hw["physical_cores"])
    n = harness.pick(N_FULL, 96)
    warmup, timed = harness.pick(1, 1), harness.pick(5, 2)
    harness.log(f"=== PHASE 3B: GEMM (mode={mode}, N={n}, workers={p}) ===")
    harness.mode_banner()
    harness.confirm_run(estimate(n, p, warmup, timed), args)

    a_data = harness.deterministic_doubles(n * n, SEED_A)
    b_data = harness.deterministic_doubles(n * n, SEED_B)
    a_list, b_list = a_data.tolist(), b_data.tolist()
    nn = n * n
    records: List[Dict[str, Any]] = []
    try:
        with harness.managed_shared_memory(3 * nn * 8) as shm:
            view = shm.buf.cast("d")
            try:
                view[0:nn] = a_data
                view[nn:2 * nn] = b_data
            finally:
                view.release()
            pool = harness.create_synced_pool(p)
            try:
                harness.log("\n[1/3] Variant 1 (naive i-j-k)")
                rec1, c1 = bench_variant(pool, shm, n, p, 1, 0, warmup, timed)
                harness.log(f"  {rec1['median_ms']:.1f} ms, {rec1['gflops']:.5f} GFLOP/s")
                harness.log("[2/3] Variant 2 (i-k-j)")
                rec2, c2 = bench_variant(pool, shm, n, p, 2, 0, warmup, timed)
                harness.log(f"  {rec2['median_ms']:.1f} ms, {rec2['gflops']:.5f} GFLOP/s")
                rec1["err_vs_v2"] = max_abs_diff(c1, c2)
                rec2["err_vs_v2"] = 0.0
                rec1["spot_error"] = spot_error(c1, a_list, b_list, n)
                rec2["spot_error"] = spot_error(c2, a_list, b_list, n)
                records += [rec1, rec2]
                harness.log(f"[3/3] Variant 3 tile sweep over BS = {TILE_SIZES}")
                for bs in TILE_SIZES:
                    rec3, c3 = bench_variant(pool, shm, n, p, 3, bs, warmup, timed)
                    rec3["err_vs_v2"] = max_abs_diff(c3, c2)
                    rec3["spot_error"] = spot_error(c3, a_list, b_list, n)
                    records.append(rec3)
                    harness.log(f"  BS={bs:>3}: {rec3['median_ms']:.1f} ms, {rec3['gflops']:.5f} GFLOP/s, "
                                f"err={rec3['err_vs_v2']:.1e}")
                pool.close()
                pool.join()
            except BaseException:
                pool.terminate()
                pool.join()
                raise
    except KeyboardInterrupt:
        print("\n[abort] interrupted; shared memory cleaned up. Re-run to start again.", file=sys.stderr)
        return 130

    tiles = [r for r in records if r["variant"] == 3]
    best = max(tiles, key=lambda r: r["gflops"])
    bs_star = best["bs"]
    l1_bytes = hw["l1d_kb"] * 1024 if hw.get("l1d_kb") else None
    l2_bytes = hw["l2_kb"] * 1024 if hw.get("l2_kb") else None
    footprints = []
    for r in tiles:
        w = 3 * r["bs"] ** 2 * 8
        r["w_tile_bytes"] = w
        r["intensity_tiled"] = r["bs"] / 12.0
        r["fits_l1"] = None if l1_bytes is None else w <= l1_bytes
        r["fits_l2"] = None if l2_bytes is None else w <= l2_bytes
        footprints.append({"bs": r["bs"], "w_tile_bytes": w, "fits_l1": r["fits_l1"], "fits_l2": r["fits_l2"]})
    worst_err = max(max(r["err_vs_v2"] for r in records), max(r["spot_error"] for r in records))
    validated = worst_err < TOLERANCE

    p3a = harness.load_json("phase3a")
    roofline = None
    if p3a and p3a.get("mode") == mode:
        roofline = roofline_mapping(records[0], records[1], best, bs_star, p3a)
    verify = harness.make_verify_record("PHASE3", hw["student_seed"], records[0]["gflops"], best["gflops"])

    observations = [
        f"Variant 2 is {records[0]['median_ms'] / records[1]['median_ms']:.2f}x faster than Variant 1; "
        f"best Variant 3 (BS*={bs_star}) is {records[0]['median_ms'] / best['median_ms']:.2f}x faster than Variant 1 "
        f"and {records[1]['median_ms'] / best['median_ms']:.2f}x relative to Variant 2.",
        f"Empirical optimum BS* = {bs_star} (tile footprint {3 * bs_star ** 2 * 8} B; "
        f"L1d = {hw.get('l1d_kb')} KB, L2 = {hw.get('l2_kb')} KB).",
        f"Max numerical deviation observed = {worst_err:.2e} ({'< 1e-9: OK' if validated else 'EXCEEDS 1e-9'}).",
    ]
    if roofline is None:
        observations.append("Roofline mapping skipped: run task3a_roofline.py (same mode) first.")
    result = {
        "phase": "3B", "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parameters": {"N": n, "workers": p, "warmup": warmup, "timed": timed, "tile_sizes": TILE_SIZES,
                       "l1d_kb": hw.get("l1d_kb"), "l2_kb": hw.get("l2_kb")},
        "records": records, "bs_star": bs_star, "footprints": footprints,
        "numerically_validated": validated, "worst_error": worst_err,
        "roofline": roofline, "observations": observations, "verify": verify,
    }
    path = harness.save_json("phase3b", result)

    harness.log("\n--- GEMM table ---")
    harness.log(harness.render_table(
        ["Variant", "BS", "Median ms", "GFLOP/s", "max|C-C2|", "spot err"],
        [(r["variant"], r["bs"] if r["bs"] else "-", f"{r['median_ms']:.1f}", f"{r['gflops']:.5f}",
          f"{r['err_vs_v2']:.1e}", f"{r['spot_error']:.1e}") for r in records]))
    for line in observations:
        harness.log(f"[obs] {line}")
    harness.log("\n" + verify["tag"])
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
