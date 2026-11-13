"""Phase 3 - empirical Roofline ceilings and progressive GEMM (Student ID 230103216).

All numbers are application-level throughput of pure Python code, NOT native CPU peaks.
"""
import multiprocessing
import time
from multiprocessing import shared_memory

from harness import (QUICK, BenchStats, detect_topology, emit_verify_tag, fmt_bytes, get_ctx,
                     md_table, save_results)

BUF_BYTES = 8 * 10**6 if QUICK else 64 * 10**6
FLOPS_PER_WORKER = 2 * 10**6 if QUICK else 60 * 10**6
N = 64 if QUICK else 256
BS_LIST = [8, 16, 32, 64, 96, 128]
WARMUP, TRIALS = (1, 3) if QUICK else (1, 5)
TRIALS_V1 = 2 if QUICK else 3


def split(n, p):
    return [((n * w) // p, (n * (w + 1)) // p) for w in range(p)]


# ---------------- Part 3A workers ----------------
def copy_task(args):
    src_name, dst_name, start, end = args
    s = shared_memory.SharedMemory(name=src_name)
    d = shared_memory.SharedMemory(name=dst_name)
    ss = dd = None
    try:
        ss = s.buf[start:end]
        dd = d.buf[start:end]
        dd[:] = ss                      # memoryview slice copy
    finally:
        if ss is not None:
            ss.release()
        if dd is not None:
            dd.release()
        s.close()
        d.close()
    return end - start


def fma_kernel(iters):
    """16 independent scalar accumulators, a_i = a_i*m + c : 16 mul + 16 add = 32 FLOP/iteration."""
    m, c = 0.999999, 1e-7
    a0 = a1 = a2 = a3 = a4 = a5 = a6 = a7 = 0.1
    a8 = a9 = a10 = a11 = a12 = a13 = a14 = a15 = 0.2
    for _ in range(iters):
        a0 = a0 * m + c
        a1 = a1 * m + c
        a2 = a2 * m + c
        a3 = a3 * m + c
        a4 = a4 * m + c
        a5 = a5 * m + c
        a6 = a6 * m + c
        a7 = a7 * m + c
        a8 = a8 * m + c
        a9 = a9 * m + c
        a10 = a10 * m + c
        a11 = a11 * m + c
        a12 = a12 * m + c
        a13 = a13 * m + c
        a14 = a14 * m + c
        a15 = a15 * m + c
    return a0 + a1 + a2 + a3 + a4 + a5 + a6 + a7 + a8 + a9 + a10 + a11 + a12 + a13 + a14 + a15


# ---------------- Part 3B workers ----------------
def gemm_task(args):
    variant, r0, r1, bs, n, na, nb, nc = args
    sa, sb, sc = (shared_memory.SharedMemory(name=x) for x in (na, nb, nc))
    A, B, C = sa.buf.cast("d"), sb.buf.cast("d"), sc.buf.cast("d")
    try:
        if variant == 1:                       # naive i-j-k, strided access to B
            for i in range(r0, r1):
                ib = i * n
                for j in range(n):
                    s = 0.0
                    for k in range(n):
                        s += A[ib + k] * B[k * n + j]
                    C[ib + j] = s
        elif variant == 2:                     # i-k-j, hoisted a_ik, unit-stride inner j
            for i in range(r0, r1):
                ib = i * n
                for j in range(n):
                    C[ib + j] = 0.0
                for k in range(n):
                    aik = A[ib + k]
                    kb = k * n
                    for j in range(n):
                        C[ib + j] += aik * B[kb + j]
        else:                                  # variant 3: six-loop tiled, i-k-j micro-kernel
            for i in range(r0, r1):
                ib = i * n
                for j in range(n):
                    C[ib + j] = 0.0
            for ii in range(r0, r1, bs):
                imax = min(ii + bs, r1)        # never cross this worker's row partition
                for kk in range(0, n, bs):
                    kmax = min(kk + bs, n)
                    for jj in range(0, n, bs):
                        jmax = min(jj + bs, n)
                        for i in range(ii, imax):
                            ib = i * n
                            for k in range(kk, kmax):
                                aik = A[ib + k]
                                kb = k * n
                                for j in range(jj, jmax):
                                    C[ib + j] += aik * B[kb + j]
    finally:
        A.release(); B.release(); C.release()   # release views BEFORE closing
        sa.close(); sb.close(); sc.close()
    return r1 - r0


def snapshot(shm):
    mv = shm.buf.cast("d")
    try:
        return list(mv)
    finally:
        mv.release()


def fill(shm, mul, mod):
    mv = shm.buf.cast("d")
    try:
        for idx in range(len(mv)):
            mv[idx] = ((idx * mul) % mod) / mod - 0.5
    finally:
        mv.release()


def timed(fn, warmup, trials):
    return BenchStats.from_ns(_collect(fn, warmup, trials))


def _collect(fn, warmup, trials):
    out = []
    for i in range(warmup + trials):
        t0 = time.perf_counter_ns()
        fn()
        t1 = time.perf_counter_ns()
        if i >= warmup:
            out.append(t1 - t0)
    return out


def main():
    topo = detect_topology()
    p = topo["Pphys"]
    ctx = get_ctx()
    body = ["# Phase 3 - empirical Roofline and GEMM", "",
            f"Pphys = {p} worker processes; warm-up = {WARMUP}, trials = {TRIALS}. "
            "Pure-Python throughput = application-level ceilings, not native CPU peaks.", ""]

    # ===== 3A: bandwidth =====
    print("3A: memory bandwidth ...")
    src = shared_memory.SharedMemory(create=True, size=BUF_BYTES)
    dst = shared_memory.SharedMemory(create=True, size=BUF_BYTES)
    pool = ctx.Pool(p)
    try:
        src.buf[:] = b"\xa5" * BUF_BYTES
        tasks = [(src.name, dst.name, a, b) for a, b in split(BUF_BYTES, p)]
        bw_stats = timed(lambda: pool.map(copy_task, tasks, chunksize=1), WARMUP + 1, TRIALS + 2)
        ok = bytes(dst.buf[:1024]) == b"\xa5" * 1024
        # compute ceiling
        print("3A: compute ceiling ...")
        iters = FLOPS_PER_WORKER // 32
        cp_stats = timed(lambda: pool.map(fma_kernel, [iters] * p, chunksize=1), WARMUP, TRIALS)
    finally:
        pool.close(); pool.join()
        src.close(); src.unlink()
        dst.close(); dst.unlink()
    bw = (2 * BUF_BYTES) / (bw_stats.median_ms / 1000 * 1e9)
    flops_total = iters * 32 * p
    psus = flops_total / (cp_stats.median_ms / 1000 * 1e9)
    ridge = psus / bw
    body += ["## Part 3A - Roofline ceilings", "",
             md_table(["Quantity", "Value"], [
                 ["Buffers", f"2 x {BUF_BYTES / 1e6:.0f} MB SharedMemory, {p} workers, slice copy"],
                 ["Copy median time (ms) / IQR", f"{bw_stats.median_ms:.3f} / {bw_stats.iqr_ms:.3f}"],
                 ["Sustained bandwidth BW = 2*size/(T*1e9)", f"{bw:.3f} GB/s"],
                 ["Copy sanity check (first KiB)", "OK" if ok else "FAILED"],
                 ["Compute kernel", f"16 accumulators, {iters:,} iterations/worker = {iters * 32:,} FLOP/worker"],
                 ["Compute median time (ms) / IQR", f"{cp_stats.median_ms:.3f} / {cp_stats.iqr_ms:.3f}"],
                 ["Sustained compute P", f"{psus:.4f} GFLOP/s ({p} workers)"],
                 ["Ridge point I* = P/BW", f"{ridge:.5f} FLOP/byte"]]), "",
             "Interpretation note: the bandwidth comes from memoryview (memcpy-level) copies, whereas the compute "
             "ceiling comes from interpreted bytecode, so the ridge point is extremely low compared with native "
             "hardware; this is an artefact of Python's interpreter overhead, not of the CPU.", ""]

    # ===== 3B: GEMM =====
    print("3B: GEMM ...")
    nbytes = N * N * 8
    shA, shB, shC = (shared_memory.SharedMemory(create=True, size=nbytes) for _ in range(3))
    pool = ctx.Pool(p)
    flop = 2 * N ** 3
    try:
        fill(shA, 37, 101)
        fill(shB, 53, 97)
        parts = split(N, p)

        def run(variant, bs=0):
            args = [(variant, a, b, bs, N, shA.name, shB.name, shC.name) for a, b in parts if b > a]
            pool.map(gemm_task, args, chunksize=1)

        print("  variant 2 (reference) ...")
        st2 = timed(lambda: run(2), WARMUP, TRIALS)
        ref = snapshot(shC)
        print("  variant 1 ...")
        st1 = timed(lambda: run(1), 1, TRIALS_V1)
        c1 = snapshot(shC)
        err1 = max(abs(x - y) for x, y in zip(c1, ref))
        sweep = {}
        for bs in BS_LIST:
            print(f"  variant 3, BS={bs} ...")
            st = timed(lambda: run(3, bs), WARMUP, TRIALS)
            err = max(abs(x - y) for x, y in zip(snapshot(shC), ref))
            sweep[bs] = (st, err)
    finally:
        pool.close(); pool.join()
        for s in (shA, shB, shC):
            s.close(); s.unlink()

    g = lambda st: flop / (st.median_ms / 1000 * 1e9)
    bs_star = max(sweep, key=lambda b: g(sweep[b][0]))
    g1, g2, g3 = g(st1), g(st2), g(sweep[bs_star][0])
    rows = []
    for bs in BS_LIST:
        st, err = sweep[bs]
        w = 3 * bs * bs * 8
        rows.append([bs, f"{st.median_ms:.2f}", f"{st.iqr_ms:.2f}", f"{g(st):.5f}", fmt_bytes(w),
                     f"{bs / 12:.2f}", f"{err:.2e}", "*" if bs == bs_star else ""])
    body += [f"## Part 3B - GEMM (N = {N}, double precision, flat buffers, {p} row-partitioned workers)", "",
             md_table(["Variant", "Median (ms)", "IQR (ms)", "GFLOP/s"], [
                 ["1 naive i-j-k", f"{st1.median_ms:.2f}", f"{st1.iqr_ms:.2f}", f"{g1:.5f}"],
                 ["2 reordered i-k-j", f"{st2.median_ms:.2f}", f"{st2.iqr_ms:.2f}", f"{g2:.5f}"],
                 [f"3 blocked, BS* = {bs_star}", f"{sweep[bs_star][0].median_ms:.2f}",
                  f"{sweep[bs_star][0].iqr_ms:.2f}", f"{g3:.5f}"]]), "",
             "### Tile-size sweep (Variant 3)", "",
             md_table(["BS", "Median (ms)", "IQR (ms)", "GFLOP/s", "Wtile = 3*BS^2*8", "Itiled ~ BS/12 (FLOP/B)",
                       "max abs(C3-C2)", "best"], rows), ""]
    v3ok = all(sweep[b][1] < 1e-9 for b in BS_LIST)
    body += ["### Numerical verification", "",
             f"- max abs(C_variant1 - C_variant2) = {err1:.3e} -> {'PASS' if err1 < 1e-9 else 'FAIL'} (< 1e-9)",
             f"- max abs(C_variant3 - C_variant2) over all BS: {max(sweep[b][1] for b in BS_LIST):.3e} -> "
             f"{'PASS' if v3ok else 'FAIL'} (< 1e-9)", ""]
    wstar = 3 * bs_star ** 2 * 8
    cache_lines = []
    for name, size in (("L1d", topo["l1d"]), ("L2", topo["l2"])):
        if size is None:
            cache_lines.append(f"- {name}: capacity unavailable, cannot compare.")
        else:
            cache_lines.append(f"- {name} = {fmt_bytes(size)}: Wtile(BS*) = {fmt_bytes(wstar)} "
                               f"({'fits' if wstar <= size else 'does NOT fit'}; {wstar / size:.1%} of capacity).")
    body += [f"### Optimal tile size: BS* = {bs_star} (highest measured GFLOP/s)", ""] + cache_lines + [""]

    # roofline mapping (assumed accounting)
    def roof(i):
        return min(psus, i * bw)

    iv = {"1 naive": 2 / 16, "2 reordered": 2 / 24, f"3 blocked BS*={bs_star}": bs_star / 12}
    meas = {"1 naive": g1, "2 reordered": g2, f"3 blocked BS*={bs_star}": g3}
    rows = [[k, f"{iv[k]:.3f}", f"{roof(iv[k]):.4f}", f"{meas[k]:.5f}", f"{meas[k] / psus:.2%}"] for k in iv]
    body += ["### Roofline mapping", "",
             md_table(["Variant", "Assumed I (FLOP/B)", "Roof = min(P, I*BW) GFLOP/s",
                       "Measured GFLOP/s", "% of P_sustained"], rows), "",
             "Assumed intensities: naive = 2 FLOP / 16 B (A and B streamed, no reuse); reordered = 2 FLOP / 24 B "
             "(B load + C load/store, a_ik in register); blocked = BS/12 as given. Replace if your handout defines others.",
             f"Ridge point I* = {ridge:.5f} FLOP/B. " + (
                 "All assumed intensities lie to the right of the ridge (compute-bound region): measured performance is "
                 "limited by the interpreter's compute rate, not by memory bandwidth."
                 if all(v >= ridge for v in iv.values()) else
                 "At least one variant lies to the left of the ridge (memory-bound region) under the assumed intensities."),
             ""]

    tag = emit_verify_tag(3, [g1, g3])
    print("\n".join(body))
    save_results(3, body, tag)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
