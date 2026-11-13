"""Phase 1 - false sharing (stride sweep), scaling and Karp-Flatt (Student ID 230103216)."""
import multiprocessing
import time
from multiprocessing import shared_memory

from harness import (BARRIER_TIMEOUT, MASK64, QUICK, BenchStats, detect_topology,
                     emit_verify_tag, get_ctx, md_table, save_results)

SHM_BYTES = 4096
SLOTS = SHM_BYTES // 8  # 512 uint64 slots
LCG_A = 6364136223846793005
LCG_C = 1442695040888963407

M_TOTAL = 400_000 if QUICK else 16_000_000
STRIDES = [1, 2, 4, 8, 16, 32]
WARMUP, TRIALS = (1, 3) if QUICK else (1, 5)


def worker(shm_name, slot, ops, start_bar, end_bar):
    """LCG read-modify-write on one uint64 slot of the shared block."""
    shm = shared_memory.SharedMemory(name=shm_name)
    mv = shm.buf.cast("Q")
    try:
        a, c, mask = LCG_A, LCG_C, MASK64
        start_bar.wait(BARRIER_TIMEOUT)
        for _ in range(ops):
            mv[slot] = (mv[slot] * a + c) & mask
        end_bar.wait(BARRIER_TIMEOUT)
    finally:
        mv.release()   # release the cast view BEFORE closing -> no BufferError
        shm.close()


def run_once(ctx, shm, p, stride, ops_each):
    shm.buf[:] = bytes(SHM_BYTES)
    start_bar, end_bar = ctx.Barrier(p + 1), ctx.Barrier(p + 1)
    procs = [ctx.Process(target=worker, args=(shm.name, w * stride, ops_each, start_bar, end_bar))
             for w in range(p)]
    for pr in procs:
        pr.start()
    try:
        start_bar.wait(BARRIER_TIMEOUT)
        t0 = time.perf_counter_ns()
        end_bar.wait(BARRIER_TIMEOUT)
        t1 = time.perf_counter_ns()
    finally:
        for pr in procs:
            pr.join(60)
            if pr.is_alive():
                pr.terminate()
    return t1 - t0


def bench(ctx, shm, p, stride):
    ops_each = M_TOTAL // p
    samples = []
    for i in range(WARMUP + TRIALS):
        ns = run_once(ctx, shm, p, stride, ops_each)
        if i >= WARMUP:
            samples.append(ns)
    return BenchStats.from_ns(samples)


def fits(p, stride):
    return (p - 1) * stride < SLOTS


def main():
    topo = detect_topology()
    pphys, plog = topo["Pphys"], topo["P_log"]
    ctx = get_ctx()
    shm = shared_memory.SharedMemory(create=True, size=SHM_BYTES)
    body = [f"# Phase 1 - false sharing and Karp-Flatt", "",
            f"Pphys = {pphys}, P_log = {plog}, total operations M = {M_TOTAL:,} (split evenly across p workers), "
            f"warm-up = {WARMUP}, trials = {TRIALS}. Shared block: {SHM_BYTES} bytes = {SLOTS} uint64 slots; "
            f"worker w uses slot w*stride.", ""]
    try:
        # ---------------- stride sweep ----------------
        print("Stride sweep ...")
        sweep = {}
        rows = []
        for s in STRIDES:
            if not fits(pphys, s):
                rows.append([s, "skipped", "-", "-", f"p*stride exceeds {SLOTS} slots"])
                continue
            st = bench(ctx, shm, pphys, s)
            sweep[s] = st
            mops = M_TOTAL / (st.median_ms / 1000) / 1e6
            rows.append([s, f"{st.median_ms:.2f}", f"{st.iqr_ms:.2f}", f"{mops:.2f}", f"{s*8} B"])
            print(f"  stride {s:>2}: median {st.median_ms:.2f} ms, IQR {st.iqr_ms:.2f}, {mops:.2f} Mops/s")
        body += [f"## Stride sweep (p = Pphys = {pphys})", "",
                 md_table(["Stride (slots)", "Median (ms)", "IQR (ms)", "Throughput (Mops/s)",
                           "Distance between workers"], rows), ""]
        # significant drop detection
        findings = []
        if 1 in sweep:
            base = sweep[1].median_ms
            first = next((s for s in STRIDES if s in sweep and s > 1 and sweep[s].median_ms <= 0.85 * base), None)
            if first:
                findings.append(f"First stride with >=15% lower median time than stride 1: stride {first} "
                                f"({sweep[first].median_ms:.2f} ms vs {base:.2f} ms).")
            else:
                findings.append("No stride reduced the median time by >=15% relative to stride 1 on this machine "
                                "(false-sharing effect not clearly visible in pure Python here).")
            best = min(sweep, key=lambda k: sweep[k].median_ms)
            findings.append(f"Fastest stride overall: {best} ({sweep[best].median_ms:.2f} ms).")
        body += ["Key findings:"] + [f"- {f}" for f in findings] + [""]

        # ---------------- process scaling ----------------
        ps, q = [], 1
        while q <= plog:
            ps.append(q)
            q *= 2
        if plog not in ps:
            ps.append(plog)
        body.append("## Process scaling (fixed total M)")
        body.append("")
        for stride in (1, 16):
            print(f"Scaling, stride {stride} ...")
            res = {}
            for p in ps:
                if not fits(p, stride):
                    continue
                res[p] = bench(ctx, shm, p, stride)
                print(f"  p={p:>2}: median {res[p].median_ms:.2f} ms")
            t1 = res[1].median_ms
            rows = []
            for p in ps:
                if p not in res:
                    rows.append([p, "skipped", "-", "-", "-", "-"])
                    continue
                tp = res[p].median_ms
                sp = t1 / tp
                ep = sp / p
                kf = f"{(1 / sp - 1 / p) / (1 - 1 / p):.4f}" if p >= 2 else "n/a"
                rows.append([p, f"{tp:.2f}", f"{res[p].iqr_ms:.2f}", f"{sp:.3f}", f"{ep:.3f}", kf])
            body += [f"### stride = {stride}", "",
                     md_table(["p", "Median (ms)", "IQR (ms)", "Speedup Sp", "Efficiency Ep",
                               "Karp-Flatt e(p)"], rows), ""]
    finally:
        shm.close()
        shm.unlink()

    tag = emit_verify_tag(1, [sweep[1].median_ms, sweep[16].median_ms]) if (1 in sweep and 16 in sweep) else \
        "VERIFY_HASH[PHASE1]=ERROR_stride1_or_16_missing"
    print("\n".join(body))
    save_results(1, body, tag)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
