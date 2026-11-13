"""build_report.py - assembles lab_report_230103216.md from the REAL measured results in results/.

Run it only after all five phase scripts have been run in FULL mode:
    python build_report.py
(--allow-quick builds a draft from quick-mode results for debugging only.)
Nothing is invented here: tables, numbers and hashes are copied from results/phaseN_results.txt.
"""
import os
import sys

from harness import BASE_DIR, RESULTS_DIR, STUDENT_ID, detect_topology, topology_table

ALLOW_QUICK = "--allow-quick" in sys.argv
SCRIPTS = ["harness.py", "task0_cal.py", "task1_falsesharing.py", "task2_scaling.py",
           "task3_roofline_gemm.py", "task4_scheduling.py", "build_report.py"]


def load(phase):
    path = os.path.join(RESULTS_DIR, f"phase{phase}_results.txt")
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} is missing. Run task{phase}_*.py first.")
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    mode = lines[0] if lines else ""
    if "MODE: FULL" not in mode and not ALLOW_QUICK:
        sys.exit(f"ERROR: phase{phase}_results.txt is not from a FULL run ({mode}). Re-run without --quick.")
    tag = next((l for l in lines if l.startswith("VERIFY_HASH[")), None)
    if tag is None:
        sys.exit(f"ERROR: no VERIFY_HASH line in phase{phase}_results.txt")
    # body = everything after the blank line that ends the banner, minus the final tag line
    start = lines.index("") + 1
    body = [l for l in lines[start:] if not l.startswith("VERIFY_HASH[")]
    # demote the '# Phase N ...' title (the report has its own headings)
    body = [l for l in body if not l.startswith("# Phase")]
    return "\n".join(body).strip(), tag, "\n".join(lines[:4])


def main():
    topo = detect_topology()
    parts = {i: load(i) for i in range(5)}
    tags = [parts[i][1] for i in range(5)]
    env_banner = parts[0][2]

    R = []
    R += [f"# Laboratory Report - Performance Evaluation and Optimization in Parallel Computing",
          f"**Student ID: {STUDENT_ID}**", "",
          "All numbers below were measured on the author's laptop and copied automatically from `results/`.", ""]
    R += ["## 1. Introduction", "",
          "The purpose of this laboratory is to evaluate and optimise parallel Python programs empirically: to "
          "measure hardware limits (timer latency, memory bandwidth, compute throughput), to expose cache-coherence "
          "effects (false sharing), to quantify strong and weak scaling with Amdahl, Gustafson and Karp-Flatt "
          "metrics, to relate matrix-multiplication performance to a Roofline model, and to compare static and "
          "dynamic loop-scheduling strategies. All experiments use pure Python 3 (standard library only), "
          "`multiprocessing` with the `spawn` start method, `multiprocessing.shared_memory`, and "
          "`time.perf_counter_ns()`. Each measurement uses warm-up runs, repeated trials, and the median with the "
          "inter-quartile range (IQR) as robust statistics; process/pool start-up is kept outside the timed regions.",
          "", "Experimental environment:", "", "```", env_banner, "```", "",
          "Limitations: pure Python measures interpreter-level throughput (not native FMA peak); Windows process "
          "creation is expensive; results are affected by thermal throttling, turbo/power policy, background "
          "processes and OS scheduling, which is why medians and IQRs are reported.", ""]
    R += ["## 2. Hardware Topology", "", topology_table(topo), ""]
    if topo["notes"]:
        R += ["Detection notes:"] + [f"- {n}" for n in topo["notes"]] + [""]

    R += ["## 3. Phase 0 Results", "", parts[0][0], "", "**Verification:** `" + tags[0] + "`", ""]

    R += ["## 4. Phase 1 Results", "", parts[1][0], "",
          "### False sharing analysis", "",
          "**Why it occurs.** Cores exchange data in cache lines (typically 64 B). If different processes write to "
          "different 8-byte slots that lie in the same line, the coherence protocol (MESI) must move the line's "
          "ownership back and forth on every write, although no data is logically shared. Each write invalidates "
          "the other cores' copies and forces a cache-to-cache transfer.", "",
          "**Effect of line size / stride.** With 8-byte slots and a 64-byte line, a stride of 8 slots separates "
          "workers by one full line; strides below 8 place several workers inside one line. Stride 16 (128 B) "
          "additionally avoids sharing a line with the adjacent-line prefetcher's paired line. The stride where "
          "the measured time drops is reported in the key findings above; in pure Python the interpreter overhead "
          "per operation is large, so the effect can be smaller than in native code, and the report must be read "
          "against the measured numbers, not the textbook expectation.", "",
          "**Why more processes do not always help.** Total work is fixed, so Amdahl-type limits, process "
          "start/synchronisation costs, shared-resource contention (coherence traffic, memory) and SMT sharing "
          "of one physical core limit speedup; efficiency E_p = S_p/p therefore falls as p grows.", "",
          "**Karp-Flatt.** e(p) = (1/S_p - 1/p)/(1 - 1/p) estimates the experimentally determined serial fraction. "
          "A roughly constant e(p) indicates an inherently serial part; an e(p) that grows with p indicates "
          "parallel overhead (contention, synchronisation) - compare the stride-1 and stride-16 tables above.", "",
          "**Verification:** `" + tags[1] + "`", ""]

    R += ["## 5. Phase 2 Results", "", parts[2][0], "",
          "### Amdahl's Law", "",
          "Strong scaling fixes N, so the serial Stage A bounds the speedup at S_inf = 1/s (value given above). "
          "The gap between the 'Amdahl predicted' column and the measured S_strong is due to overheads that Amdahl's "
          "simple model ignores (pool dispatch and result transfer, memory/cache sharing, SMT, frequency scaling).", "",
          "### Gustafson's Law", "",
          "Weak scaling grows N with p; the serial fraction alpha(p) = TStageA(p)/Tweak(p) is measured and the scaled "
          "speedup S = p - alpha(p-1) is computed from it. E_weak = Tweak(1)/Tweak(p) shows how well the "
          "per-worker time stays constant.", "",
          "### Architectural inflection", "",
          "Beyond Pphys, extra logical threads are SMT siblings that share the execution units and caches of an "
          "already busy physical core, so the marginal gain per added thread is expected to be well below the "
          "ideal; scheduling overhead and shared memory bandwidth add to the diminishing returns. A hybrid "
          "(P-core/E-core) design is only claimed if hardware inspection says so (see the statement in the "
          "Phase 2 section above).", "",
          "**Verification:** `" + tags[2] + "`", ""]

    R += ["## 6. Phase 3 Results", "", parts[3][0], "",
          "### Roofline analysis", "",
          "Attainable performance = min(P_sustained, I x BW_sustained). The ridge point I* separates "
          "memory-bound (I < I*) from compute-bound (I > I*) kernels. Cache blocking raises the operational "
          "intensity by reusing a BS x BS tile (Itiled ~ BS/12) while its working set Wtile = 3 BS^2 x 8 B fits "
          "in a cache level; the measured optimum BS* and its footprint relative to the real L1d/L2 sizes are "
          "stated above. Because every access is interpreted Python bytecode, the measured GFLOP/s of all "
          "variants is far below native performance and the benefit of blocking is limited by interpreter "
          "overhead - the measured numbers decide how large the benefit is on this machine.", "",
          "**Verification:** `" + tags[3] + "`", ""]

    R += ["## 7. Phase 4 Results", "", parts[4][0], "",
          "### Explanation", "",
          "- **Contiguous block static** gives each worker an equal number of iterations, but K(i) grows "
          "quadratically with i, so the last block contains far more work -> large max/avg (lambda).",
          "- **Cyclic round-robin** interleaves cheap and expensive iterations across workers, so the load is "
          "much more even without any run-time coordination.",
          "- **Dynamic, C = 1** balances almost perfectly but pays one lock acquisition per iteration; whether "
          "this matters depends on the work per item relative to the lock cost (see measured table).",
          "- **Dynamic, C = 512** claims huge chunks; the last chunks still contain expensive iterations, "
          "causing tail imbalance (some workers finish early and idle).",
          "- **Intermediate C** trades synchronisation overhead against tail imbalance; the experimentally "
          "optimal C* is given in the chunk-size sweep above.", "",
          "**Verification:** `" + tags[4] + "`", ""]

    R += ["## 8. Overall Conclusions", "",
          "- **Bottlenecks:** interpreter overhead dominates all pure-Python kernels; beyond that, serial stages, "
          "process synchronisation, shared-cache/memory traffic and SMT sharing limit scaling (see Phases 1-2).",
          "- **Cache contention:** Phase 1 quantifies the cost of workers sharing a cache line via the stride "
          "sweep and Karp-Flatt tables.",
          "- **Strong vs weak scaling:** strong scaling is bounded by Amdahl's serial fraction, whereas weak "
          "scaling (Gustafson) lets the problem grow with p; compare E_strong and E_weak in Phase 2.",
          "- **Memory vs compute:** the Roofline ceilings of Phase 3 show which resource limits each GEMM variant.",
          "- **Cache blocking:** its effect is judged from the Variant 1/2/3 and tile-sweep tables; the optimum "
          "BS* is compared with the real L1d/L2 sizes.",
          "- **Dynamic scheduling:** removes load imbalance at the cost of synchronisation; the chunk size C "
          "balances the two (Phase 4).", ""]

    R += ["## 9. Complete Source Code", ""]
    for fn in SCRIPTS:
        R += [f"### {fn}", "", "```python", open(os.path.join(BASE_DIR, fn), encoding="utf-8").read().rstrip(),
              "```", ""]

    R += ["## 10. All Verification Lines", "", "```"] + tags + ["```", ""]
    out = os.path.join(BASE_DIR, f"lab_report_{STUDENT_ID}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(R))
    print(f"Report written: {out}")
    print("\n".join(tags))


if __name__ == "__main__":
    main()
