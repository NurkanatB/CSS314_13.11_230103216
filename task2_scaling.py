"""Phase 2 - strong and weak scaling with a two-stage pipeline (Student ID 230103216).

Workload translation (pure Python, NOT hardware FMA):
  Stage A (serial, parent): first 5% of N elements, 10 elementary float operations per element
        (mul, add, mul, sub, sqrt, mul, add, sqrt, mul, sub). One extra accumulation per element
        keeps the result alive.
  Stage B (parallel, Pool): all N elements split into p contiguous ranges; per element 10 rounds of
        { multiply-add (FMA-style a*k+c), sin, cos } = 30 counted operations. No shared writes:
        each worker returns a partial sum.
"""
import math
import multiprocessing
import time

from harness import (QUICK, BenchStats, detect_topology, emit_verify_tag, get_ctx, md_table,
                     save_results)

N_STRONG = 60_000 if QUICK else 1_200_000
N0_WEAK = 10_000 if QUICK else 200_000
WARMUP, TRIALS = (1, 3) if QUICK else (1, 5)
STAGE_A_FRACTION = 0.05


def stage_a(n_a):
    sqrt = math.sqrt
    acc = 0.0
    for i in range(n_a):
        x = (i % 1000) * 1e-3 + 0.1
        a = x * 1.5
        a = a + 0.25
        a = a * a
        a = a - 0.125
        a = sqrt(a)
        a = a * 0.75
        a = a + 1.0
        a = sqrt(a)
        a = a * 2.0
        a = a - 0.5
        acc += a
    return acc


def stage_b_chunk(rng):
    start, end = rng
    sin, cos = math.sin, math.cos
    total = 0.0
    for i in range(start, end):
        a = (i % 1000) * 1e-3 + 0.1
        for _ in range(10):
            a = a * 1.000001 + 0.000001   # FMA-style multiply-add
            a = sin(a) + 1.0
            a = cos(a) + 1.0
        total += a
    return total


def split(n, p):
    return [((n * w) // p, (n * (w + 1)) // p) for w in range(p)]


def run_total(pool, p, n):
    t0 = time.perf_counter_ns()
    stage_a(int(STAGE_A_FRACTION * n))
    t1 = time.perf_counter_ns()
    pool.map(stage_b_chunk, split(n, p), chunksize=1)
    t2 = time.perf_counter_ns()
    return t2 - t0, t1 - t0


def bench_p(ctx, p, n):
    pool = ctx.Pool(p)   # persistent pool: created OUTSIDE the timed region
    try:
        tot, sa = [], []
        for i in range(WARMUP + TRIALS):
            t, a = run_total(pool, p, n)
            if i >= WARMUP:
                tot.append(t)
                sa.append(a)
    finally:
        pool.close()
        pool.join()
    return BenchStats.from_ns(tot), BenchStats.from_ns(sa)


def main():
    topo = detect_topology()
    pphys, plog = topo["Pphys"], topo["P_log"]
    ctx = get_ctx()
    body = ["# Phase 2 - strong and weak scaling", "",
            f"Pphys = {pphys}, P_log = {plog}; warm-up = {WARMUP}, trials = {TRIALS}; persistent Pool per p "
            f"(creation excluded from timing); Stage A serial in parent = {STAGE_A_FRACTION:.0%} of N.", ""]

    # ---------- strong ----------
    print("Strong scaling ...")
    strong = {}
    for p in range(1, plog + 1):
        strong[p] = bench_p(ctx, p, N_STRONG)
        print(f"  p={p:>2}: {strong[p][0].median_ms:.2f} ms")
    t1 = strong[1][0].median_ms
    s_frac = strong[1][1].median_ms / t1
    s_inf = 1 / s_frac
    rows = []
    for p in range(1, plog + 1):
        tp = strong[p][0].median_ms
        sp = t1 / tp
        amd = 1 / (s_frac + (1 - s_frac) / p)
        rows.append([p, f"{tp:.2f}", f"{strong[p][0].iqr_ms:.2f}", f"{sp:.3f}", f"{sp / p:.3f}", f"{amd:.3f}"])
    body += [f"## Strong scaling (N = {N_STRONG:,})", "",
             md_table(["p", "T (ms)", "IQR (ms)", "S_strong", "E_strong", "Amdahl S(p) predicted"], rows), "",
             f"Serial fraction s = TStageA(1)/Tstrong(1) = {strong[1][1].median_ms:.2f} / {t1:.2f} = **{s_frac:.4f}**; "
             f"Amdahl limit S_inf = 1/s = **{s_inf:.3f}**.",
             f"Measured S_strong({plog}) = {t1 / strong[plog][0].median_ms:.3f} "
             f"({t1 / strong[plog][0].median_ms / s_inf:.1%} of the Amdahl limit).", ""]

    # ---------- weak ----------
    print("Weak scaling ...")
    weak = {}
    for p in range(1, plog + 1):
        weak[p] = bench_p(ctx, p, p * N0_WEAK)
        print(f"  p={p:>2}: {weak[p][0].median_ms:.2f} ms")
    w1 = weak[1][0].median_ms
    rows = []
    sg = {}
    for p in range(1, plog + 1):
        tw = weak[p][0].median_ms
        alpha = weak[p][1].median_ms / tw
        sg[p] = p - alpha * (p - 1)
        rows.append([p, f"{p * N0_WEAK:,}", f"{tw:.2f}", f"{weak[p][0].iqr_ms:.2f}", f"{w1 / tw:.3f}",
                     f"{alpha:.4f}", f"{sg[p]:.3f}"])
    body += [f"## Weak scaling (N0 = {N0_WEAK:,}, N(p) = p*N0)", "",
             md_table(["p", "N(p)", "T (ms)", "IQR (ms)", "E_weak", "alpha(p)", "S_Gustafson"], rows), ""]

    # ---------- inflection ----------
    body.append("## Architectural inflection (Pphys vs Pphys+1)")
    body.append("")
    if pphys + 1 <= plog and pphys in strong:
        a, b = pphys, pphys + 1
        sa_, sb_ = t1 / strong[a][0].median_ms, t1 / strong[b][0].median_ms
        ea_, eb_ = w1 / weak[a][0].median_ms, w1 / weak[b][0].median_ms
        body.append(md_table(["Metric", f"p = {a}", f"p = {b}", "Change"], [
            ["Strong T (ms)", f"{strong[a][0].median_ms:.2f}", f"{strong[b][0].median_ms:.2f}",
             f"{(strong[b][0].median_ms / strong[a][0].median_ms - 1):+.1%}"],
            ["S_strong", f"{sa_:.3f}", f"{sb_:.3f}", f"{(sb_ / sa_ - 1):+.1%}"],
            ["Weak T (ms)", f"{weak[a][0].median_ms:.2f}", f"{weak[b][0].median_ms:.2f}",
             f"{(weak[b][0].median_ms / weak[a][0].median_ms - 1):+.1%}"],
            ["E_weak", f"{ea_:.3f}", f"{eb_:.3f}", f"{(eb_ / ea_ - 1):+.1%}"]]))
        body += ["", f"Marginal strong-scaling gain from the extra logical thread: {(sb_ - sa_):+.3f} speedup units "
                     f"(ideal would be about {sa_ / a:+.3f})."]
    else:
        body.append("Pphys + 1 is not available (P_log <= Pphys): no SMT/extra logical threads, comparison not possible.")
    if topo["hybrid"] is True:
        body.append("Hardware inspection reports multiple efficiency classes: the CPU is hybrid (P/E cores).")
    elif topo["hybrid"] is False:
        body.append("Hardware inspection reports a single efficiency class: no P/E-core split was detected.")
    else:
        body.append("Hybrid P/E-core status could not be determined from the OS; no assumption is made.")
    body.append("")

    tag = emit_verify_tag(2, [t1 / strong[plog][0].median_ms, sg[plog]])
    print("\n".join(body))
    save_results(2, body, tag)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
