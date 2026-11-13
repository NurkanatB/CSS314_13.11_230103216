"""Phase 4 - dynamic load balancing (Student ID 230103216).

K(i) = 1 + floor((i/40)^2); each of the K steps does sin, cos and sqrt.
Strategies: contiguous block static, cyclic static, shared-memory dynamic queue (chunk C).
"""
import math
import multiprocessing
import time

from harness import (BARRIER_TIMEOUT, QUICK, BenchStats, detect_topology, emit_verify_tag, get_ctx,
                     md_table, save_results)

N = 1000 if QUICK else 8000
CHUNKS = [1, 8, 64, 512]
WARMUP, TRIALS = (1, 2) if QUICK else (1, 3)


def work_item(i):
    k = 1 + int((i / 40.0) ** 2)
    sin, cos, sqrt = math.sin, math.cos, math.sqrt
    x = i * 0.001 + 0.5
    for _ in range(k):
        x = sqrt(abs(sin(x) + cos(x))) + 0.1
    return x


def worker(kind, w, p, n, chunk, counter, times, counts, sums, sbar, ebar):
    sbar.wait(BARRIER_TIMEOUT)
    t0 = time.perf_counter_ns()
    chk, cnt = 0.0, 0
    if kind == "block":
        for i in range((n * w) // p, (n * (w + 1)) // p):
            chk += work_item(i)
            cnt += 1
    elif kind == "cyclic":
        for i in range(w, n, p):
            chk += work_item(i)
            cnt += 1
    else:  # dynamic queue: atomically claim [start, start+chunk)
        while True:
            with counter.get_lock():
                start = counter.value
                counter.value = start + chunk
            if start >= n:
                break
            for i in range(start, min(start + chunk, n)):
                chk += work_item(i)
                cnt += 1
    t1 = time.perf_counter_ns()
    times[w] = (t1 - t0) / 1e6
    counts[w] = cnt
    sums[w] = chk
    ebar.wait(BARRIER_TIMEOUT)


def run_once(ctx, kind, chunk, p):
    counter = ctx.Value("i", 0)
    times = ctx.Array("d", p, lock=False)
    counts = ctx.Array("q", p, lock=False)
    sums = ctx.Array("d", p, lock=False)
    sbar, ebar = ctx.Barrier(p + 1), ctx.Barrier(p + 1)
    procs = [ctx.Process(target=worker, args=(kind, w, p, N, chunk, counter, times, counts, sums, sbar, ebar))
             for w in range(p)]
    for pr in procs:
        pr.start()
    try:
        sbar.wait(BARRIER_TIMEOUT)
        t0 = time.perf_counter_ns()
        ebar.wait(BARRIER_TIMEOUT)
        t1 = time.perf_counter_ns()
    finally:
        for pr in procs:
            pr.join(60)
            if pr.is_alive():
                pr.terminate()
    if sum(counts) != N:
        raise RuntimeError(f"exactly-once violated: processed {sum(counts)} of {N}")
    return (t1 - t0) / 1e6, list(times), sum(sums)


def measure(ctx, kind, chunk, p, ref_chk):
    runs = []
    for i in range(WARMUP + TRIALS):
        r = run_once(ctx, kind, chunk, p)
        if abs(r[2] - ref_chk[0]) > 1e-6 * abs(ref_chk[0]) if ref_chk[0] else False:
            raise RuntimeError("checksum mismatch between strategies")
        ref_chk[0] = ref_chk[0] or r[2]
        if i >= WARMUP:
            runs.append(r)
    stats = BenchStats.from_ns([int(r[0] * 1e6) for r in runs])
    lam = lambda r: max(r[1]) / (sum(r[1]) / len(r[1]))
    lams = sorted(lam(r) for r in runs)
    mid = sorted(runs, key=lambda r: r[0])[len(runs) // 2]
    return {"stats": stats, "lambda": lams[len(lams) // 2], "workers": mid[1]}


def main():
    topo = detect_topology()
    p = topo["Pphys"]
    ctx = get_ctx()
    total_k = sum(1 + int((i / 40.0) ** 2) for i in range(N))
    body = ["# Phase 4 - dynamic load balancing", "",
            f"N = {N}, p = Pphys = {p}, total inner steps = {total_k:,}; warm-up = {WARMUP}, trials = {TRIALS}. "
            "Worker runtime = time inside the worker from start-barrier release to the end of its loop "
            "(process start-up excluded); wall time measured by the parent between the two barriers.", ""]
    configs = [("block", 0, "Block static"), ("cyclic", 0, "Cyclic static")] + \
              [("dynamic", c, f"Dynamic C={c}") for c in CHUNKS]
    res, ref = {}, [0.0]
    for kind, c, label in configs:
        print(f"{label} ...")
        res[label] = measure(ctx, kind, c, p, ref)
        r = res[label]
        print(f"  wall {r['stats'].median_ms:.1f} ms, lambda {r['lambda']:.3f}")
    tb = res["Block static"]["stats"].median_ms
    rows = [[lab, f"{r['stats'].median_ms:.1f}", f"{r['stats'].iqr_ms:.1f}", f"{r['lambda']:.3f}",
             f"{tb / r['stats'].median_ms:.2f}x"] for lab, r in res.items()]
    body += ["## Scheduling comparison", "",
             md_table(["Strategy", "Wall median (ms)", "IQR (ms)", "lambda = max/avg (median)", "Speedup vs block"],
                      rows), "",
             "Every run verified: each item processed exactly once (sum of per-worker counts == N) and the "
             "checksum agrees across all strategies (relative tolerance 1e-6).", "",
             "## Individual worker active runtimes (ms, from the median-wall trial)", "",
             md_table(["Strategy"] + [f"W{w}" for w in range(p)] + ["max/avg"],
                      [[lab] + [f"{t:.1f}" for t in r["workers"]] +
                       [f"{max(r['workers']) / (sum(r['workers']) / p):.3f}"] for lab, r in res.items()]), ""]
    dyn = {c: res[f"Dynamic C={c}"] for c in CHUNKS}
    c_star = min(dyn, key=lambda c: dyn[c]["stats"].median_ms)
    best_static = min(("Block static", "Cyclic static"), key=lambda k: res[k]["stats"].median_ms)
    body += ["## Chunk-size sweep (dynamic queue)", "",
             md_table(["C", "Wall median (ms)", "IQR (ms)", "lambda", "best"],
                      [[c, f"{dyn[c]['stats'].median_ms:.1f}", f"{dyn[c]['stats'].iqr_ms:.1f}",
                        f"{dyn[c]['lambda']:.3f}", "*" if c == c_star else ""] for c in CHUNKS]), "",
             f"Experimentally optimal chunk size C* = **{c_star}** "
             f"(Twall = {dyn[c_star]['stats'].median_ms:.1f} ms, lambda = {dyn[c_star]['lambda']:.3f}).",
             f"Best static strategy: {best_static} ({res[best_static]['stats'].median_ms:.1f} ms); "
             f"dynamic C* is {res[best_static]['stats'].median_ms / dyn[c_star]['stats'].median_ms:.2f}x "
             f"{'faster' if dyn[c_star]['stats'].median_ms < res[best_static]['stats'].median_ms else 'relative speed'} "
             "than it.", ""]
    tag = emit_verify_tag(4, [tb, dyn[c_star]["stats"].median_ms])
    print("\n".join(body))
    save_results(4, body, tag)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
